"""Deterministic GitHub -> unified FalkorDB writer.

Repository, SourceFile, Commit and Person are provider facts and therefore do
not need an LLM. File bodies and commit messages are queued for the shared
semantic pass, which extracts only Decision/Term/System knowledge.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from falkordb import Graph

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger, RecordEdgeRef, SemanticStatus
from connectors.core.models import SourceAccess, SourceBreadcrumb, SourceRecord
from connectors.core.runner import prepare_record
from connectors.github_app.api import GitHubCommit, GitHubFile, GitHubRepository
from graph import writer as w
from graph.jira_pipeline import delete_orphaned_shared_entities, delete_record
from graph.resolver import (
    anchor_properties, link_verified_person_identity, resolve_backlinks_for_target,
    resolve_exact_anchors,
)

logger = logging.getLogger("neuron.github_pipeline")


def _time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def repository_record(repository: GitHubRepository, installation_id: int) -> SourceRecord:
    return SourceRecord(
        provider="github", connection_id=str(installation_id), entity_type="repository",
        external_id=str(repository.repository_id), name=repository.full_name,
        content=(f"GitHub repository {repository.full_name}\n"
                 f"Default branch: {repository.default_branch}\n"
                 f"Visibility: {'private' if repository.private else 'public'}"),
        url=repository.html_url,
        metadata={"default_branch": repository.default_branch, "pipeline_version": 2},
        access=SourceAccess(public=not repository.private, policy_version="github-app-v1"),
    )


def file_record(
    repository: GitHubRepository, file: GitHubFile, content: str, installation_id: int
) -> SourceRecord:
    extension = PurePosixPath(file.path).suffix.lower()
    language = "python" if extension == ".py" else None
    source_text = (
        f"[SOURCE]\nKind: SourceFile\nName: {file.path}\n"
        f"Repository: {repository.full_name}\n\n{content}"
    )
    return SourceRecord(
        provider="github", connection_id=str(installation_id), entity_type="source_file",
        external_id=f"{repository.repository_id}:{file.path}", name=file.path,
        content=source_text,
        url=(f"{repository.html_url}/blob/{quote(repository.default_branch, safe='')}/"
             f"{quote(file.path, safe='/')}") if repository.html_url else None,
        parent_external_id=str(repository.repository_id),
        breadcrumbs=(SourceBreadcrumb(str(repository.repository_id), repository.full_name, "repository"),),
        mime_type="text/x-python" if extension == ".py" else "text/markdown",
        language=language,
        metadata={"repository_id": repository.repository_id, "path": file.path,
                  "blob_sha": file.sha, "pipeline_version": 2},
        access=SourceAccess(public=not repository.private, policy_version="github-app-v1"),
    )


def commit_record(
    repository: GitHubRepository, commit: GitHubCommit, installation_id: int
) -> SourceRecord:
    source_text = (
        f"[SOURCE]\nKind: Commit\nName: {commit.sha[:12]}\n"
        f"Repository: {repository.full_name}\nAuthor: {commit.author_name}\n\n{commit.message}"
    )
    return SourceRecord(
        provider="github", connection_id=str(installation_id), entity_type="commit",
        external_id=f"{repository.repository_id}:{commit.sha}",
        name=f"{commit.sha[:7]} — {commit.message.splitlines()[0][:160]}",
        content=source_text, url=commit.html_url,
        parent_external_id=str(repository.repository_id),
        breadcrumbs=(SourceBreadcrumb(str(repository.repository_id), repository.full_name, "repository"),),
        created_at=_time(commit.authored_at), updated_at=_time(commit.authored_at),
        mime_type="text/plain",
        metadata={"repository_id": repository.repository_id, "sha": commit.sha,
                  "pipeline_version": 2},
        access=SourceAccess(public=not repository.private, policy_version="github-app-v1"),
    )


def repository_uid(installation_id: int, repository_id: int) -> str:
    return w.make_uid("Repository", "github", str(installation_id), str(repository_id))


def file_uid(installation_id: int, repository_id: int, path: str) -> str:
    return w.make_uid("SourceFile", "github", str(installation_id), str(repository_id), path)


def commit_uid(installation_id: int, repository_id: int, sha: str) -> str:
    return w.make_uid("Commit", "github", str(installation_id), str(repository_id), sha)


def person_uid(commit: GitHubCommit) -> str:
    identity = commit.author_email.strip().lower() or commit.author_name.strip().lower()
    return w.make_uid("Person", "github-author", identity)


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
) -> RecordEdgeRef:
    w.upsert_fact_edges(graph, rel, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": [record_key],
        "evidence": None, "extraction_method": "deterministic", "confidence": 1.0,
        "valid_at": valid_at.isoformat() if valid_at else None,
    }])
    return RecordEdgeRef(rel, from_uid, to_uid)


def write_repository(
    graph: Graph, ledger: ConnectorLedger, repository: GitHubRepository, installation_id: int
) -> RecordAction:
    record = repository_record(repository, installation_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = repository_uid(installation_id, repository.repository_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Repository", [{"uid": uid, "props": {
        "name": repository.full_name, "search_text": record.content, "url": repository.html_url,
        "default_branch": repository.default_branch, "private": repository.private,
    }}])
    w.link_mentioned_in(graph, "Repository", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="Repository",
        target_provider="github", repository_name=repository.full_name,
        url=repository.html_url,
    )
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=SemanticStatus.NOT_APPLICABLE)
    return prepared.action


def write_file(
    graph: Graph, ledger: ConnectorLedger, repository: GitHubRepository,
    file: GitHubFile, content: str, installation_id: int,
) -> RecordAction:
    record = file_record(repository, file, content, installation_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = file_uid(installation_id, repository.repository_id, file.path)
    root_uid = repository_uid(installation_id, repository.repository_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "SourceFile", [{"uid": uid, "props": {
        "name": file.path, "search_text": record.content, "url": record.url,
        "path": file.path, "language": record.language or "markdown", "blob_sha": file.sha,
    }}])
    w.link_mentioned_in(graph, "SourceFile", [{"uid": uid, "record_key": record.record_key}])
    edge = _write_edge(graph, "CONTAINS", "Repository", "SourceFile", root_uid, uid,
                       record.record_key, record.reference_time)
    ledger.record_edge(record.record_key, edge.rel_type, edge.from_uid, edge.to_uid)
    resolve_exact_anchors(graph, ledger, record, uid, "SourceFile")
    ledger.save_chunks(record.record_key, [(c.chunk_id, c.chunk_index, c.text) for c in prepared.chunks])
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=SemanticStatus.PENDING if prepared.chunks else SemanticStatus.NOT_APPLICABLE)
    return prepared.action


def write_commit(
    graph: Graph, ledger: ConnectorLedger, repository: GitHubRepository,
    commit: GitHubCommit, installation_id: int,
) -> RecordAction:
    record = commit_record(repository, commit, installation_id)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = commit_uid(installation_id, repository.repository_id, commit.sha)
    root_uid = repository_uid(installation_id, repository.repository_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Commit", [{"uid": uid, "props": {
        "name": record.name, "search_text": record.content, "url": record.url,
        "sha": commit.sha, "authored_at": commit.authored_at,
    }}])
    w.link_mentioned_in(graph, "Commit", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="Commit",
        target_provider="github", commit_sha=commit.sha, url=commit.html_url,
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
    ledger.record_edges_batch(record.record_key, edges)
    link_verified_person_identity(graph, ledger, p_uid, commit.author_email, record.record_key)
    resolve_exact_anchors(graph, ledger, record, uid, "Commit")
    ledger.save_chunks(record.record_key, [(c.chunk_id, c.chunk_index, c.text) for c in prepared.chunks])
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=SemanticStatus.PENDING if prepared.chunks else SemanticStatus.NOT_APPLICABLE)
    return prepared.action


def reconcile_repository_records(
    graph: Graph, ledger: ConnectorLedger, installation_id: int, repository_id: int,
    present_file_keys: set[str], present_commit_keys: set[str],
) -> int:
    removed = 0
    for entity_type, present in (("source_file", present_file_keys), ("commit", present_commit_keys)):
        prefix = f"github:{installation_id}:{entity_type}:{repository_id}:"
        for record_key in ledger.record_keys_with_prefix(prefix):
            if record_key not in present:
                delete_record(graph, ledger, record_key)
                removed += 1
    return removed
