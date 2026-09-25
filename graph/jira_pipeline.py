"""Jira deterministic pass (plan.md §3 Pass A, Block 6). Builds Project/
WorkItem/Person nodes and structural edges straight from Jira API fields —
zero LLM. Free-text chunks are queued for the shared semantic pass after the
deterministic write succeeds.

`project_record`/`issue_record` are ported near-verbatim from the source
project's `graph/jira_ingest.py` — they only build `SourceRecord`s and never
touched Graphiti, so the change here is dropping the Graphiti-era
`jira_group_id()` concept (plan.md §1: one unified graph, no per-source group).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from falkordb import Graph
from openai import OpenAI

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger, RecordEdgeRef, SemanticStatus
from connectors.core.models import SourceAccess, SourceBreadcrumb, SourceRecord
from connectors.core.runner import prepare_record
from connectors.jira.api import JiraIssue, JiraPerson, JiraProject, JiraSite, field_intervals
from graph.derived import materialize_around
from graph import vector_store
from graph.embed_batch import active_batch
from graph import writer as w
from graph.resolver import (
    anchor_properties, link_verified_person_identity, resolve_backlinks_for_target,
    resolve_exact_anchors, resolved_anchor_values,
)
from graph.selective_ingestion import has_pending, selective_chunk_writes

logger = logging.getLogger("neuron.jira_pipeline")

_embed_client: OpenAI | None = None


def _embed_now(
    uid: str, label: str, text: str, collection: str = vector_store.COLLECTION,
    name: str | None = None,
) -> None:
    """Embed and upsert a single node immediately, at write time.

    Used for Pass A records that never reach the semantic pass
    (`NOT_APPLICABLE`). Without this, a WorkItem/Commit is fulltext-searchable
    but has no vector at all --
    verified on real data: an issue with an empty description ranked #1 on
    a fulltext-only query, yet never appeared in `hybrid_search`'s top
    results, because RRF (`graph/search.py`) sums a rank contribution from
    *each* leg a uid appears in. A uid absent from the vector leg gets only
    one leg's worth of score, so anything present in *both* legs -- even
    weakly -- outranks it. This is not an LLM call (no extraction, no
    Decision/Term/System reasoning), just the same embedding model call the
    semantic pass already makes for every other node -- Pass A stays
    deterministic in the sense that matters (no LLM judgment involved).

    Both named channels are written in ONE request (two inputs, not two
    calls): `name` embeds the node's own name/path, `content` the full text.
    See `vector_store.ensure_collection` for why a single combined embedding
    loses short-query matches.
    """
    global _embed_client
    if _embed_client is None:
        # An explicit timeout, because the SDK default is a 600s read with two
        # retries -- up to 30 MINUTES stalled on one record inside a loop of
        # hundreds. Observed live: an 11-minute silence mid-sync with the
        # process at 0% CPU. Failing this call fast and letting
        # `scripts/rebuild_vectors.py` repair the gap is strictly better than
        # holding an entire ingestion hostage to one hung connection.
        _embed_client = OpenAI(timeout=30.0, max_retries=2)
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    content = vector_store.truncate_for_embedding(text)
    name_text = vector_store.truncate_for_embedding((name or "").strip() or content)

    # Inside a sync, hand the record to the open batch instead of issuing a
    # request per record -- see graph/embed_batch.py for the measurement.
    batch = active_batch()
    if batch is not None and batch.collection == collection:
        batch.add(uid, label, content, name_text)
        return

    response = _embed_client.embeddings.create(
        model=model, input=[content, name_text],
    )
    vector_store.upsert_vectors(vector_store.client(), [{
        "uid": uid, "label": label,
        "embedding": response.data[0].embedding,
        "name_embedding": response.data[1].embedding,
        "embedded_text": content[:400],
        "embedded_model": model,
    }], collection=collection)


def _time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------- SourceRecord


def project_record(project: JiraProject, site: JiraSite, connection_id: str) -> SourceRecord:
    lines = [f"Jira project {project.key}: {project.name}"]
    if project.description:
        lines.extend(["", project.description])
    return SourceRecord(
        provider="jira", connection_id=connection_id, entity_type="project",
        external_id=f"{site.cloud_id}:{project.project_id}", name=project.name,
        content="\n".join(lines).strip(),
        url=f"{site.url}/browse/{project.key}" if site.url else None,
        metadata={"cloud_id": site.cloud_id, "project_key": project.key, "pipeline_version": 3},
        access=SourceAccess(policy_version="jira-delegated-v1"),
    )


def issue_record(issue: JiraIssue, project: JiraProject, site: JiraSite, connection_id: str) -> SourceRecord:
    return SourceRecord(
        provider="jira", connection_id=connection_id, entity_type="work_item",
        external_id=f"{site.cloud_id}:{issue.issue_id}", name=f"{issue.key} — {issue.summary}",
        content=issue.content, url=f"{site.url}/browse/{issue.key}" if site.url else None,
        breadcrumbs=(SourceBreadcrumb(project.project_id, project.name, "project"),),
        created_at=_time(issue.created_at), updated_at=_time(issue.updated_at),
        mime_type="text/markdown",
        metadata={
            "cloud_id": site.cloud_id,
            "project_key": project.key,
            "issue_key": issue.key,
            "pipeline_version": 3,
        },
        access=SourceAccess(policy_version="jira-delegated-v1"),
    )


# --------------------------------------------------------------- uid helpers


def project_uid(connection_id: str, external_id: str) -> str:
    return w.make_uid("Project", "jira", connection_id, external_id)


def work_item_uid(connection_id: str, external_id: str) -> str:
    return w.make_uid("WorkItem", "jira", connection_id, external_id)


def person_uid(account_id: str) -> str:
    """Person identity is deliberately namespaced to `jira-account` for now.
    True cross-provider Person merging (same human, seen via Jira email and a
    GitHub commit author email) is Tier-3 similarity/adjudication work
    (plan.md §5), not this deterministic pass — a Jira account id alone isn't
    a verified-enough identifier to assume it's the same person elsewhere."""
    return w.make_uid("Person", "jira-account", account_id)


# --------------------------------------------------------------- write


def write_project(
    graph: Graph, ledger: ConnectorLedger, project: JiraProject, site: JiraSite, connection_id: str
) -> RecordAction:
    record = project_record(project, site, connection_id)
    prepared = prepare_record(record, ledger)
    logger.info("project %s: %s", project.key, prepared.action.value)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    if prepared.action == RecordAction.DELETE:
        delete_record(graph, ledger, record.record_key)
        return prepared.action

    uid = project_uid(connection_id, record.external_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_record_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_record_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Project", [{
        "uid": uid,
        "props": {"name": project.name, "search_text": record.content, "url": record.url},
    }])
    w.link_mentioned_in(graph, "Project", [{"uid": uid, "record_key": record.record_key}])

    # A project record always has *some* content (the "Jira project X: Y"
    # boilerplate line), so `prepared.chunks` is never empty -- gate on the
    # actual description instead, or every project would queue a pointless
    # LLM call that can only ever extract nothing (plan.md §4: don't spend
    # budget where there's no real free text).
    if project.description:
        chunk_writes = selective_chunk_writes(prepared.chunks)
        ledger.save_chunks(
            record.record_key, chunk_writes,
        )
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=(SemanticStatus.PENDING if project.description and has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def write_issue(
    graph: Graph,
    ledger: ConnectorLedger,
    issue: JiraIssue,
    project: JiraProject,
    site: JiraSite,
    connection_id: str,
    collection: str = vector_store.COLLECTION,
) -> RecordAction:
    record = issue_record(issue, project, site, connection_id)
    prepared = prepare_record(record, ledger)
    logger.info(
        "fetched %s: %r  status=%s type=%s assignee=%s reporter=%s labels=%s blocks=%s",
        issue.key, issue.summary, issue.status, issue.issue_type,
        issue.assignee.display_name if issue.assignee else None,
        issue.reporter.display_name if issue.reporter else None,
        list(issue.labels), list(issue.blocks),
    )
    logger.info("issue %s: %s", issue.key, prepared.action.value)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    if prepared.action == RecordAction.DELETE:
        delete_record(graph, ledger, record.record_key)
        return prepared.action

    wi_uid = work_item_uid(connection_id, record.external_id)
    proj_external_id = f"{site.cloud_id}:{project.project_id}"
    proj_uid = project_uid(connection_id, proj_external_id)

    if prepared.action == RecordAction.UPDATE:
        _reset_record_support(graph, ledger, record.record_key, wi_uid, record.reference_time)

    w.upsert_source_records(graph, [_source_record_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "WorkItem", [{
        "uid": wi_uid,
        "props": {
            "name": record.name,
            "search_text": record.content,
            "url": record.url,
            "status": issue.status,
            "issue_type": issue.issue_type,
            "labels": list(issue.labels),
            "issue_key": issue.key,
        },
    }])
    w.link_mentioned_in(graph, "WorkItem", [{"uid": wi_uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=wi_uid, target_label="WorkItem",
        target_provider="jira", jira_key=issue.key, url=record.url,
    )

    edges_supported: list[RecordEdgeRef] = []

    # single-valued structural facts: supersede-then-upsert (writer.py pattern,
    # verified against reassignment/unchanged-reassignment/no-prior-value)
    edges_supported.append(
        _write_single_valued(graph, "BELONGS_TO", "WorkItem", "Project", wi_uid, proj_uid,
                             record.record_key, record.reference_time)
    )
    for rel, person in (("ASSIGNED_TO", issue.assignee), ("REPORTED_BY", issue.reporter)):
        if person is None:
            continue
        p_uid = person_uid(person.account_id)
        w.upsert_entities(graph, "Person", [{"uid": p_uid, "props": {
            "name": person.display_name, "email": person.email or None,
        }}])
        # Without this, a Person is never discoverable via fetch_graph's
        # MENTIONED_IN-anchored node query, and every edge touching them
        # (ASSIGNED_TO/REPORTED_BY) silently disappears from the canvas too
        # (found while testing Block 9's /api/graph against real data).
        w.link_mentioned_in(graph, "Person", [{"uid": p_uid, "record_key": record.record_key}])
        edges_supported.append(_write_single_valued(
            graph, rel, "WorkItem", "Person", wi_uid, p_uid,
            record.record_key, record.reference_time,
        ))
        edges_supported.extend(
            link_verified_person_identity(
                graph, ledger, p_uid, person.email, record.record_key,
            )
        )

    if issue.parent_id:
        parent_uid = work_item_uid(connection_id, f"{site.cloud_id}:{issue.parent_id}")
        w.ensure_node_stub(graph, "WorkItem", parent_uid)
        if issue.parent_key:
            w.upsert_entities(graph, "WorkItem", [{
                "uid": parent_uid,
                "props": {"issue_key": issue.parent_key, "name": issue.parent_key},
            }])
        edges_supported.append(_write_single_valued(
            graph, "PARENT_OF", "WorkItem", "WorkItem", wi_uid, parent_uid,
            record.record_key, record.reference_time,
        ))

    _write_changelog_history(graph, issue, wi_uid, record.record_key)

    # BLOCKS is multi-valued (an issue can block several) -- plain upsert, no
    # supersede. Jira returns issues ordered by `updated ASC`, so the target
    # of a BLOCKS edge is often not synced yet within the same run --
    # `ensure_node_stub` reserves its identity now so the edge always
    # resolves; the target's real properties fill in whenever its own record
    # is processed (this run or a later one), via `upsert_entities`'s
    # `ON MATCH`.
    if issue.blocks:
        rows = []
        for blocked_id in issue.blocks:
            target_uid = work_item_uid(connection_id, f"{site.cloud_id}:{blocked_id}")
            w.ensure_node_stub(graph, "WorkItem", target_uid)  # target may not be synced yet this run
            rows.append({
                "from_uid": wi_uid, "to_uid": target_uid, "source_record_keys": [record.record_key],
                "evidence": None, "extraction_method": "deterministic", "confidence": 1.0,
                "valid_at": record.reference_time.isoformat() if record.reference_time else None,
            })
        w.upsert_fact_edges(graph, "BLOCKS", "WorkItem", "WorkItem", rows)
        edges_supported.extend(RecordEdgeRef("BLOCKS", r["from_uid"], r["to_uid"]) for r in rows)

    edges_supported.extend(
        resolve_exact_anchors(graph, ledger, record, wi_uid, "WorkItem")
    )
    materialize_around(graph, wi_uid, record.record_key)
    ledger.record_edges_batch(record.record_key, edges_supported)
    logger.info(
        "wrote %s: WorkItem node + %d structural edge(s) -> %s",
        issue.key, len(edges_supported), [e.rel_type for e in edges_supported],
    )

    _embed_now(wi_uid, "WorkItem", record.content, collection=collection, name=record.name)
    chunk_writes = selective_chunk_writes(
        prepared.chunks,
        resolved_anchors=resolved_anchor_values(graph, record, wi_uid),
    )
    ledger.save_chunks(record.record_key, chunk_writes)
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=wi_uid,
                  semantic_status=(SemanticStatus.PENDING if has_pending(chunk_writes)
                                   else SemanticStatus.NOT_APPLICABLE))
    return prepared.action


def _write_changelog_history(
    graph: Graph, issue: JiraIssue, wi_uid: str, record_key: str,
) -> None:
    """Closed changelog windows become FactHistory so the world axis is complete
    on the first sync, not only after we watch a field change live."""
    observed = w.now_iso()
    assignee_rows = []
    for start, end, account_id, display_name in field_intervals(
        issue.created_at,
        issue.assignee.account_id if issue.assignee else None,
        issue.assignee.display_name if issue.assignee else None,
        issue.changes, "assignee",
    ):
        if end is None or not account_id:
            continue
        p_uid = person_uid(account_id)
        w.upsert_entities(graph, "Person", [{"uid": p_uid, "props": {
            "name": display_name or account_id,
        }}])
        w.link_mentioned_in(graph, "Person", [{"uid": p_uid, "record_key": record_key}])
        assignee_rows.append({
            "from_uid": wi_uid, "to_uid": p_uid,
            "from_name": issue.key, "to_name": display_name or account_id,
            "valid_from": _time(start).isoformat() if _time(start) else start,
            "valid_to": _time(end).isoformat() if _time(end) else end,
            "source_record_keys": [record_key],
            "evidence": f"{issue.key} assignee {display_name or account_id}",
            "observed_from": observed, "observed_to": observed,
            "attested_from": _time(start).isoformat() if _time(start) else start,
        })
    w.upsert_history_intervals(graph, "ASSIGNED_TO", assignee_rows)

    status_rows = []
    for start, end, _status_id, status_name in field_intervals(
        issue.created_at, issue.status, issue.status, issue.changes, "status",
    ):
        if end is None or not status_name:
            continue
        status_uid = w.make_uid("Status", "jira-status", status_name.lower())
        status_rows.append({
            "from_uid": wi_uid, "to_uid": status_uid,
            "from_name": issue.key, "to_name": status_name,
            "valid_from": _time(start).isoformat() if _time(start) else start,
            "valid_to": _time(end).isoformat() if _time(end) else end,
            "source_record_keys": [record_key],
            "evidence": f"{issue.key} status {status_name}",
            "observed_from": observed, "observed_to": observed,
            "attested_from": _time(start).isoformat() if _time(start) else start,
        })
    w.upsert_history_intervals(graph, "HAS_STATUS", status_rows)


def _write_single_valued(
    graph: Graph, rel: str, from_label: str, to_label: str, from_uid: str, to_uid: str,
    record_key: str, valid_at: datetime | None = None,
) -> RecordEdgeRef:
    w.supersede_fact_edges(graph, rel, from_label, to_label, [from_uid])
    w.upsert_fact_edges(graph, rel, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": [record_key],
        "evidence": None, "extraction_method": "deterministic", "confidence": 1.0,
        "valid_at": valid_at.isoformat() if valid_at else None,
    }])
    return RecordEdgeRef(rel, from_uid, to_uid)


def _source_record_row(record: SourceRecord, content_hash: str) -> dict:
    return {
        "record_key": record.record_key, "provider": record.provider,
        "connection_id": record.connection_id,
        "entity_type": record.entity_type, "external_id": record.external_id,
        "name": record.name, "url": record.url, "content_hash": content_hash,
        "public": record.access.public, "principals": list(record.access.principals),
        "policy_version": record.access.policy_version,
        "source_created_at": record.created_at.isoformat() if record.created_at else None,
        "source_updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "source_time": record.reference_time.isoformat() if record.reference_time else None,
        **anchor_properties(record),
    }


def _reset_record_support(
    graph: Graph, ledger: ConnectorLedger, record_key: str, primary_uid: str,
    valid_to: datetime | None = None,
) -> None:
    old_edges = ledger.edges_for_record(record_key)
    w.remove_record_support(graph, record_key, [
        {"rel_type": edge.rel_type, "from_uid": edge.from_uid, "to_uid": edge.to_uid,
         "valid_to": valid_to.isoformat() if valid_to else None}
        for edge in old_edges
    ])
    w.unlink_record_mentions_except(graph, record_key, primary_uid)
    ledger.clear_edges(record_key)


def delete_record(graph: Graph, ledger: ConnectorLedger, record_key: str) -> None:
    """Hard delete: removes the record's own WorkItem/Project node (and every
    edge on it, via DETACH DELETE) and its :SourceRecord node.

    This does NOT by itself clean up shared entities (Person/Decision/Term/
    System) that this record contributed edges to but that other surviving
    records may still reference — call `delete_orphaned_shared_entities`
    once after deleting every record in a batch (e.g. a whole connection),
    not per-record, since scanning for orphans per-record is redundant work
    and would be wrong mid-batch (an entity that looks orphaned after
    deleting record 1 of 3 may not be once records 2-3 are still pending)."""
    entry = ledger.get(record_key)
    old_edges = ledger.edges_for_record(record_key)
    w.remove_record_support(graph, record_key, [
        {"rel_type": edge.rel_type, "from_uid": edge.from_uid, "to_uid": edge.to_uid}
        for edge in old_edges
    ])
    if entry and entry.primary_node_uid:
        w.delete_node(graph, entry.primary_node_uid)
    w.delete_source_record(graph, record_key)
    ledger.commit_delete(record_key)


def delete_orphaned_shared_entities(graph: Graph) -> int:
    """Call once after a batch of `delete_record` calls (plan.md: Person/
    Decision/Term/System can be shared across many records via MENTIONED_IN,
    so they're only safe to remove once no live record references them)."""
    return w.delete_orphaned_entities(graph, ["Person", "Decision", "Term", "System"])
