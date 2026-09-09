"""Jira deterministic pass (plan.md §3 Pass A, Block 6). Builds Project/
WorkItem/Person nodes and structural edges straight from Jira API fields —
zero LLM. The semantic pass (Decision/Term/System extraction from free text)
is Block 7; this module's only connection to it is flagging each record's
`semantic_status` so that pass knows what's waiting.

`project_record`/`issue_record` are ported near-verbatim from the source
project's `graph/jira_ingest.py` — they only build `SourceRecord`s and never
touched Graphiti, so the change here is dropping the Graphiti-era
`jira_group_id()` concept (plan.md §1: one unified graph, no per-source group).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from falkordb import Graph

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger, RecordEdgeRef, SemanticStatus
from connectors.core.models import SourceAccess, SourceBreadcrumb, SourceRecord
from connectors.core.runner import prepare_record
from connectors.jira.api import JiraIssue, JiraPerson, JiraProject, JiraSite
from graph import writer as w

logger = logging.getLogger("neuron.jira_pipeline")


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
        metadata={"cloud_id": site.cloud_id, "project_key": project.key, "pipeline_version": 2},
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
            "pipeline_version": 2,
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
        _reset_record_support(graph, ledger, record.record_key, uid)
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
    if project.description.strip():
        ledger.save_chunks(record.record_key, [(c.chunk_id, c.chunk_index, c.text) for c in prepared.chunks])
        semantic_status = SemanticStatus.PENDING
    else:
        semantic_status = SemanticStatus.NOT_APPLICABLE
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid, semantic_status=semantic_status)
    return prepared.action


def write_issue(
    graph: Graph,
    ledger: ConnectorLedger,
    issue: JiraIssue,
    project: JiraProject,
    site: JiraSite,
    connection_id: str,
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
        _reset_record_support(graph, ledger, record.record_key, wi_uid)

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
        },
    }])
    w.link_mentioned_in(graph, "WorkItem", [{"uid": wi_uid, "record_key": record.record_key}])

    edges_supported: list[RecordEdgeRef] = []

    # single-valued structural facts: supersede-then-upsert (writer.py pattern,
    # verified against reassignment/unchanged-reassignment/no-prior-value)
    edges_supported.append(
        _write_single_valued(graph, "BELONGS_TO", "WorkItem", "Project", wi_uid, proj_uid, record.record_key)
    )
    for rel, person in (("ASSIGNED_TO", issue.assignee), ("REPORTED_BY", issue.reporter)):
        if person is None:
            continue
        p_uid = person_uid(person.account_id)
        w.upsert_entities(graph, "Person", [{"uid": p_uid, "props": {"name": person.display_name}}])
        # Without this, a Person is never discoverable via fetch_graph's
        # MENTIONED_IN-anchored node query, and every edge touching them
        # (ASSIGNED_TO/REPORTED_BY) silently disappears from the canvas too
        # (found while testing Block 9's /api/graph against real data).
        w.link_mentioned_in(graph, "Person", [{"uid": p_uid, "record_key": record.record_key}])
        edges_supported.append(_write_single_valued(graph, rel, "WorkItem", "Person", wi_uid, p_uid, record.record_key))

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
            })
        w.upsert_fact_edges(graph, "BLOCKS", "WorkItem", "WorkItem", rows)
        edges_supported.extend(RecordEdgeRef("BLOCKS", r["from_uid"], r["to_uid"]) for r in rows)

    ledger.record_edges_batch(record.record_key, edges_supported)
    logger.info(
        "wrote %s: WorkItem node + %d structural edge(s) -> %s",
        issue.key, len(edges_supported), [e.rel_type for e in edges_supported],
    )

    if issue.description.strip():
        ledger.save_chunks(record.record_key, [(c.chunk_id, c.chunk_index, c.text) for c in prepared.chunks])
        semantic_status = SemanticStatus.PENDING
    else:
        semantic_status = SemanticStatus.NOT_APPLICABLE
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=wi_uid, semantic_status=semantic_status)
    return prepared.action


def _write_single_valued(
    graph: Graph, rel: str, from_label: str, to_label: str, from_uid: str, to_uid: str, record_key: str
) -> RecordEdgeRef:
    w.supersede_fact_edges(graph, rel, from_label, to_label, [from_uid])
    w.upsert_fact_edges(graph, rel, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": [record_key],
        "evidence": None, "extraction_method": "deterministic", "confidence": 1.0,
    }])
    return RecordEdgeRef(rel, from_uid, to_uid)


def _source_record_row(record: SourceRecord, content_hash: str) -> dict:
    return {
        "record_key": record.record_key, "provider": record.provider,
        "entity_type": record.entity_type, "external_id": record.external_id,
        "name": record.name, "url": record.url, "content_hash": content_hash,
    }


def _reset_record_support(
    graph: Graph, ledger: ConnectorLedger, record_key: str, primary_uid: str
) -> None:
    old_edges = ledger.edges_for_record(record_key)
    w.remove_record_support(graph, record_key, [
        {"rel_type": edge.rel_type, "from_uid": edge.from_uid, "to_uid": edge.to_uid}
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
    old_edges = ledger.edges_for_record(record_key)
    w.remove_record_support(graph, record_key, [
        {"rel_type": edge.rel_type, "from_uid": edge.from_uid, "to_uid": edge.to_uid}
        for edge in old_edges
    ])
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
