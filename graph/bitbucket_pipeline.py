"""Deterministic Bitbucket -> unified FalkorDB writer.

Mirrors `graph/github_pipeline.py`: Repository, SourceFile, Commit and Person
are provider facts and need no LLM. File/commit/PR text stays on the node
for search and exact ticket-key anchors; it is not queued for extraction.

Unlike GitHub's App-installation model, Bitbucket uses classic per-user OAuth
(like Jira) — `connection_id` is the OAuth connection's opaque id, not a
numeric installation id, and a repository's stable identity is its Bitbucket
`uuid`, not an integer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from falkordb import Graph

from connectors.bitbucket.api import (
    BitbucketCommit, BitbucketFile, BitbucketFileChange, BitbucketPullRequest,
    BitbucketRepository,
)
from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger, RecordEdgeRef, SemanticStatus
from connectors.core.models import SourceAccess, SourceBreadcrumb, SourceRecord
from connectors.core.runner import prepare_record
from graph import vector_store
from graph import writer as w
from graph.jira_pipeline import _embed_now, delete_orphaned_shared_entities, delete_record
from graph.resolver import (
    anchor_properties, link_verified_person_identity, resolve_backlinks_for_target,
    resolve_exact_anchors, resolved_anchor_values,
)
from graph.selective_ingestion import has_pending, selective_chunk_writes


def _time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def repository_record(repository: BitbucketRepository, connection_id: str) -> SourceRecord:
    return SourceRecord(
        provider="bitbucket", connection_id=connection_id, entity_type="repository",
        external_id=repository.uuid, name=repository.full_name,
        content=(f"Bitbucket repository {repository.full_name}\n"
                 f"Main branch: {repository.main_branch}\n"
                 f"Visibility: {'private' if repository.private else 'public'}\n\n"
                 f"{repository.description}"),
        url=repository.html_url,
        metadata={"main_branch": repository.main_branch, "pipeline_version": 1},
        access=SourceAccess(public=not repository.private, policy_version="bitbucket-oauth-v1"),
    )


def file_record(
    repository: BitbucketRepository, file: BitbucketFile, connection_id: str,
) -> SourceRecord:
    extension = PurePosixPath(file.path).suffix.lower()
    source_text = (
        f"[SOURCE]\nKind: SourceFile\nName: {file.path}\n"
        f"Repository: {repository.full_name}\n\n{file.content}"
    )
    return SourceRecord(
        provider="bitbucket", connection_id=connection_id, entity_type="source_file",
        external_id=f"{repository.uuid}:{file.path}", name=file.path,
        content=source_text,
        # Link at the file's own commit when known: a permalink, and it avoids
        # the branch-name-with-'/' addressing problem (see api.resolve_ref).
        url=(f"{repository.html_url}/src/"
             f"{quote(file.commit_hash or repository.main_branch, safe='/')}/"
             f"{quote(file.path, safe='/')}") if repository.html_url else None,
        parent_external_id=repository.uuid,
        breadcrumbs=(SourceBreadcrumb(repository.uuid, repository.full_name, "repository"),),
        mime_type="text/x-python" if extension == ".py" else "text/markdown",
        language=file.language,
        # `branch` is deliberately NOT part of external_id: a repository has one
        # ingested state, and the branch selects which state that is. Recording
        # it here (and in the URL) keeps provenance honest about where the
        # content came from.
        metadata={"repository_uuid": repository.uuid, "path": file.path,
                  "commit_hash": file.commit_hash, "branch": repository.main_branch,
                  "pipeline_version": 1},
        access=SourceAccess(public=not repository.private, policy_version="bitbucket-oauth-v1"),
    )


def _file_change_line(change: BitbucketFileChange) -> str:
    return f"  {change.status}  {change.path}  +{change.lines_added}/-{change.lines_removed}"


def commit_record(
    repository: BitbucketRepository, commit: BitbucketCommit, connection_id: str,
) -> SourceRecord:
    files_block = ""
    if commit.files:
        files_block = "\n\nFiles:\n" + "\n".join(_file_change_line(c) for c in commit.files)
    source_text = (
        f"[SOURCE]\nKind: Commit\nName: {commit.commit_hash[:12]}\n"
        f"Repository: {repository.full_name}\nAuthor: {commit.author_name}\n\n"
        f"{commit.message}{files_block}"
    )
    return SourceRecord(
        provider="bitbucket", connection_id=connection_id, entity_type="commit",
        external_id=f"{repository.uuid}:{commit.commit_hash}",
        name=f"{commit.commit_hash[:7]} — {commit.message.splitlines()[0][:160]}",
        content=source_text, url=commit.html_url,
        parent_external_id=repository.uuid,
        breadcrumbs=(SourceBreadcrumb(repository.uuid, repository.full_name, "repository"),),
        created_at=_time(commit.date), updated_at=_time(commit.date),
        mime_type="text/plain",
        metadata={"repository_uuid": repository.uuid, "commit_hash": commit.commit_hash,
                  "branch": repository.main_branch, "pipeline_version": 2},
        access=SourceAccess(public=not repository.private, policy_version="bitbucket-oauth-v1"),
    )


def modifies_paths(
    commit: BitbucketCommit, present_paths: set[str],
) -> list[tuple[str, str]]:
    """(path, evidence) for HEAD SourceFiles this commit still shares a path with.

    Deleted / non-ingested extensions have no SourceFile — listed on the
    commit record, not linked. Renames prefer `path` (new), then `old_path`.
    """
    linked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for change in commit.files:
        evidence = f"{change.status} +{change.lines_added}/-{change.lines_removed}"
        for candidate in (change.path, change.old_path):
            if candidate and candidate in present_paths and candidate not in seen:
                seen.add(candidate)
                linked.append((candidate, evidence))
                break
    return linked


def pull_request_record(
    repository: BitbucketRepository, pr: BitbucketPullRequest, connection_id: str,
) -> SourceRecord:
    source_text = (
        f"[SOURCE]\nKind: PullRequest\nName: PR #{pr.id} — {pr.title}\n"
        f"Repository: {repository.full_name}\nAuthor: {pr.author_name}\n"
        f"Branch: {pr.source_branch} -> {pr.destination_branch}\n\n{pr.description}"
    )
    return SourceRecord(
        provider="bitbucket", connection_id=connection_id, entity_type="pull_request",
        external_id=f"{repository.uuid}:{pr.id}", name=f"PR #{pr.id} — {pr.title}",
        content=source_text, url=pr.html_url,
        parent_external_id=repository.uuid,
        breadcrumbs=(SourceBreadcrumb(repository.uuid, repository.full_name, "repository"),),
        created_at=_time(pr.created_on), updated_at=_time(pr.updated_on),
        mime_type="text/plain",
        metadata={"repository_uuid": repository.uuid, "pull_request_id": pr.id,
                  "state": pr.state, "pipeline_version": 1},
        access=SourceAccess(public=not repository.private, policy_version="bitbucket-oauth-v1"),
    )


def repository_uid(connection_id: str, repository_uuid: str) -> str:
    return w.make_uid("Repository", "bitbucket", connection_id, repository_uuid)


def file_uid(connection_id: str, repository_uuid: str, path: str) -> str:
    return w.make_uid("SourceFile", "bitbucket", connection_id, repository_uuid, path)


def commit_uid(connection_id: str, repository_uuid: str, commit_hash: str) -> str:
    return w.make_uid("Commit", "bitbucket", connection_id, repository_uuid, commit_hash)


def pull_request_uid(connection_id: str, repository_uuid: str, pr_id: int) -> str:
    return w.make_uid("PullRequest", "bitbucket", connection_id, repository_uuid, str(pr_id))


def pull_request_ref(repository: BitbucketRepository, pr_id: int) -> str:
    return f"{repository.full_name.lower()}#{pr_id}"


def person_uid(commit: BitbucketCommit) -> str:
    identity = commit.author_email.strip().lower() or commit.author_name.strip().lower()
    return w.make_uid("Person", "bitbucket-author", identity)


def pr_person_uid(pr: BitbucketPullRequest) -> str:
    # KNOWN GAP: Bitbucket's pull-request API never exposes an author email
    # (only display_name + an internal uuid), while commit authors almost
    # always have one (from local git config) -- so this keys on name, in the
    # SAME "bitbucket-author" namespace commits use, giving the two a chance
    # to merge when a commit author also happens to have no email. When the
    # matching commit author DOES have an email, the two will NOT merge (the
    # commit's Person node is keyed by that email, not by name) -- there is
    # no way to resolve this without an email on the PR side, which Bitbucket
    # does not provide. Verified-email SAME_AS linking is unaffected: it only
    # ever merges nodes when both sides propose a real email.
    identity = pr.author_name.strip().lower() or pr.author_uuid.strip().lower()
    return w.make_uid("Person", "bitbucket-author", identity)


def _source_row(record: SourceRecord, content_hash: str) -> dict:
    return {
        "record_key": record.record_key, "provider": record.provider,
        "connection_id": record.connection_id, "entity_type": record.entity_type,
        "external_id": record.external_id, "name": record.name, "url": record.url,
        "content_hash": content_hash,
        "public": record.access.public, "principals": list(record.access.principals),
        "policy_version": record.access.policy_version,
        "source_created_at": record.created_at.isoformat() if record.created_at else None,
        "source_updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "source_time": record.reference_time.isoformat() if record.reference_time else None,
        **anchor_properties(record),
    }


def _reset_support(
    graph: Graph, ledger: ConnectorLedger, record_key: str, primary_uid: str,
    valid_to: datetime | None = None,
) -> None:
    old = ledger.edges_for_record(record_key)
    w.remove_record_support(graph, record_key, [
        {"rel_type": edge.rel_type, "from_uid": edge.from_uid, "to_uid": edge.to_uid,
         "valid_to": valid_to.isoformat() if valid_to else None}
        for edge in old
    ])
    w.unlink_record_mentions_except(graph, record_key, primary_uid)
    ledger.clear_edges(record_key)


def _write_edge(
    graph: Graph, rel: str, from_label: str, to_label: str,
    from_uid: str, to_uid: str, record_key: str, valid_at: datetime | None = None,
    evidence: str | None = None,
) -> RecordEdgeRef:
    w.upsert_fact_edges(graph, rel, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": [record_key],
        "evidence": evidence, "extraction_method": "deterministic", "confidence": 1.0,
        "valid_at": valid_at.isoformat() if valid_at else None,
    }])
    return RecordEdgeRef(rel, from_uid, to_uid)


def write_repository(
    graph: Graph, ledger: ConnectorLedger, repository: BitbucketRepository, connection_id: str,
) -> RecordAction:
    record = repository_record(repository, connection_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = repository_uid(connection_id, repository.uuid)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Repository", [{"uid": uid, "props": {
        "name": repository.full_name, "search_text": record.content, "url": repository.html_url,
        "default_branch": repository.main_branch, "private": repository.private,
    }}])
    w.link_mentioned_in(graph, "Repository", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="Repository",
        target_provider="bitbucket", repository_name=repository.full_name,
        url=repository.html_url,
    )
    if repository.description:
        chunk_writes = selective_chunk_writes(prepared.chunks)
        ledger.save_chunks(
            record.record_key, chunk_writes,
        )
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=(SemanticStatus.PENDING if repository.description and has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def write_file(
    graph: Graph, ledger: ConnectorLedger, repository: BitbucketRepository,
    file: BitbucketFile, connection_id: str,
    collection: str = vector_store.COLLECTION,
) -> RecordAction:
    record = file_record(repository, file, connection_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = file_uid(connection_id, repository.uuid, file.path)
    root_uid = repository_uid(connection_id, repository.uuid)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "SourceFile", [{"uid": uid, "props": {
        "name": file.path, "search_text": record.content, "url": record.url,
        "path": file.path, "language": record.language or "markdown", "blob_sha": file.commit_hash,
    }}])
    w.link_mentioned_in(graph, "SourceFile", [{"uid": uid, "record_key": record.record_key}])
    edge = _write_edge(graph, "CONTAINS", "Repository", "SourceFile", root_uid, uid,
                       record.record_key, record.reference_time)
    ledger.record_edge(record.record_key, edge.rel_type, edge.from_uid, edge.to_uid)
    resolve_exact_anchors(graph, ledger, record, uid, "SourceFile")
    if record.content.strip():
        _embed_now(uid, "SourceFile", record.content, collection=collection, name=record.name)
    chunk_writes = selective_chunk_writes(
        prepared.chunks, resolved_anchors=resolved_anchor_values(graph, record, uid),
    )
    ledger.save_chunks(record.record_key, chunk_writes)
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=(SemanticStatus.PENDING if has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def write_commit(
    graph: Graph, ledger: ConnectorLedger, repository: BitbucketRepository,
    commit: BitbucketCommit, connection_id: str,
    collection: str = vector_store.COLLECTION,
    present_paths: set[str] | None = None,
) -> RecordAction:
    record = commit_record(repository, commit, connection_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = commit_uid(connection_id, repository.uuid, commit.commit_hash)
    root_uid = repository_uid(connection_id, repository.uuid)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Commit", [{"uid": uid, "props": {
        "name": record.name, "search_text": record.content, "url": record.url,
        "sha": commit.commit_hash, "authored_at": commit.date,
    }}])
    w.link_mentioned_in(graph, "Commit", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="Commit",
        target_provider="bitbucket", commit_sha=commit.commit_hash, url=commit.html_url,
    )
    p_uid = person_uid(commit)
    w.upsert_entities(graph, "Person", [{"uid": p_uid, "props": {
        "name": commit.author_name, "email": commit.author_email or None,
    }}])
    w.link_mentioned_in(graph, "Person", [{"uid": p_uid, "record_key": record.record_key}])
    edges = [
        _write_edge(graph, "CONTAINS", "Repository", "Commit", root_uid, uid,
                    record.record_key, record.reference_time),
        _write_edge(graph, "AUTHORED_BY", "Commit", "Person", uid, p_uid,
                    record.record_key, record.reference_time),
    ]
    for path, evidence in modifies_paths(commit, present_paths or set()):
        edges.append(_write_edge(
            graph, "MODIFIES", "Commit", "SourceFile", uid,
            file_uid(connection_id, repository.uuid, path),
            record.record_key, record.reference_time, evidence=evidence,
        ))
    ledger.record_edges_batch(record.record_key, edges)
    link_verified_person_identity(graph, ledger, p_uid, commit.author_email, record.record_key)
    resolve_exact_anchors(graph, ledger, record, uid, "Commit")
    if record.content.strip():
        _embed_now(uid, "Commit", record.content, collection=collection, name=record.name)
    chunk_writes = selective_chunk_writes(
        prepared.chunks, resolved_anchors=resolved_anchor_values(graph, record, uid),
    )
    ledger.save_chunks(record.record_key, chunk_writes)
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=(SemanticStatus.PENDING if has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def write_pull_request(
    graph: Graph, ledger: ConnectorLedger, repository: BitbucketRepository,
    pr: BitbucketPullRequest, connection_id: str,
    collection: str = vector_store.COLLECTION,
) -> RecordAction:
    record = pull_request_record(repository, pr, connection_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = pull_request_uid(connection_id, repository.uuid, pr.id)
    root_uid = repository_uid(connection_id, repository.uuid)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "PullRequest", [{"uid": uid, "props": {
        "name": record.name, "search_text": record.content, "url": record.url,
        "state": pr.state, "source_branch": pr.source_branch,
        "destination_branch": pr.destination_branch,
        "pr_id": pr.id, "pr_ref": pull_request_ref(repository, pr.id),
    }}])
    w.link_mentioned_in(graph, "PullRequest", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="PullRequest",
        target_provider="bitbucket", url=pr.html_url,
        pull_request_ref=pull_request_ref(repository, pr.id),
    )
    p_uid = pr_person_uid(pr)
    w.upsert_entities(graph, "Person", [{"uid": p_uid, "props": {"name": pr.author_name}}])
    w.link_mentioned_in(graph, "Person", [{"uid": p_uid, "record_key": record.record_key}])
    edges = [
        _write_edge(graph, "CONTAINS", "Repository", "PullRequest", root_uid, uid,
                    record.record_key, record.reference_time),
        _write_edge(graph, "AUTHORED_BY", "PullRequest", "Person", uid, p_uid,
                    record.record_key, record.reference_time),
    ]
    ledger.record_edges_batch(record.record_key, edges)
    resolve_exact_anchors(graph, ledger, record, uid, "PullRequest")
    if record.content.strip():
        _embed_now(uid, "PullRequest", record.content, collection=collection, name=record.name)
    chunk_writes = selective_chunk_writes(
        prepared.chunks, resolved_anchors=resolved_anchor_values(graph, record, uid),
    )
    ledger.save_chunks(record.record_key, chunk_writes)
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=(SemanticStatus.PENDING if has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def reconcile_repository_records(
    graph: Graph, ledger: ConnectorLedger, connection_id: str, repository_uuid: str,
    present_file_keys: set[str], present_commit_keys: set[str],
    present_pr_keys: set[str] = frozenset(),
) -> int:
    removed = 0
    for entity_type, present in (
        ("source_file", present_file_keys), ("commit", present_commit_keys),
        ("pull_request", present_pr_keys),
    ):
        prefix = f"bitbucket:{connection_id}:{entity_type}:{repository_uuid}:"
        for record_key in ledger.record_keys_with_prefix(prefix):
            if record_key not in present:
                delete_record(graph, ledger, record_key)
                removed += 1
    return removed
