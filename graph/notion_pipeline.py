"""Deterministic Notion -> unified FalkorDB writer."""

from __future__ import annotations

from datetime import UTC, datetime

from falkordb import Graph

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger, RecordEdgeRef, SemanticStatus
from connectors.core.models import SourceAccess, SourceBreadcrumb, SourceRecord
from connectors.core.runner import prepare_record
from connectors.notion.api import NotionPage
from graph import writer as w
from graph.resolver import anchor_properties, resolve_backlinks_for_target, resolve_exact_anchors


def _time(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def workspace_record(workspace_id: str, workspace_name: str) -> SourceRecord:
    return SourceRecord(
        provider="notion", connection_id=workspace_id, entity_type="workspace",
        external_id=workspace_id, name=workspace_name,
        content=f"Notion workspace: {workspace_name}",
        metadata={"pipeline_version": 2},
        access=SourceAccess(policy_version="notion-oauth-v1"),
    )


def page_record(page: NotionPage, workspace_id: str, workspace_name: str) -> SourceRecord:
    content = (
        f"[SOURCE]\nKind: Document\nName: {page.title}\n"
        f"Workspace: {workspace_name}\n\n{page.content}"
    )
    return SourceRecord(
        provider="notion", connection_id=workspace_id, entity_type="page",
        external_id=page.page_id, name=page.title, content=content, url=page.url,
        parent_external_id=page.parent_page_id,
        breadcrumbs=(SourceBreadcrumb(workspace_id, workspace_name, "workspace"),),
        updated_at=_time(page.last_edited_time), mime_type="text/markdown",
        metadata={"parent_page_id": page.parent_page_id, "pipeline_version": 2},
        access=SourceAccess(policy_version="notion-oauth-v1"),
    )


def workspace_uid(workspace_id: str) -> str:
    return w.make_uid("Workspace", "notion", workspace_id)


def document_uid(workspace_id: str, page_id: str) -> str:
    return w.make_uid("Document", "notion", workspace_id, page_id)


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


def _edge(
    graph: Graph, rel: str, from_label: str, to_label: str,
    from_uid: str, to_uid: str, record_key: str, valid_at: datetime | None = None,
) -> RecordEdgeRef:
    w.upsert_fact_edges(graph, rel, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": [record_key],
        "evidence": None, "extraction_method": "deterministic", "confidence": 1.0,
        "valid_at": valid_at.isoformat() if valid_at else None,
    }])
    return RecordEdgeRef(rel, from_uid, to_uid)


def write_workspace(
    graph: Graph, ledger: ConnectorLedger, workspace_id: str, workspace_name: str
) -> RecordAction:
    record = workspace_record(workspace_id, workspace_name)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = workspace_uid(workspace_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Workspace", [{"uid": uid, "props": {
        "name": workspace_name, "search_text": record.content,
    }}])
    w.link_mentioned_in(graph, "Workspace", [{"uid": uid, "record_key": record.record_key}])
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=SemanticStatus.NOT_APPLICABLE)
    return prepared.action


def write_page(
    graph: Graph, ledger: ConnectorLedger, page: NotionPage,
    workspace_id: str, workspace_name: str,
) -> RecordAction:
    record = page_record(page, workspace_id, workspace_name)
    prepared = prepare_record(record, ledger)
    if prepared.action == RecordAction.KEEP:
        return prepared.action
    uid = document_uid(workspace_id, page.page_id)
    root_uid = workspace_uid(workspace_id)
    if prepared.action == RecordAction.UPDATE:
        _reset_support(graph, ledger, record.record_key, uid, record.reference_time)
    w.upsert_source_records(graph, [_source_row(record, prepared.content_hash)])
    w.upsert_entities(graph, "Document", [{"uid": uid, "props": {
        "name": page.title, "search_text": record.content, "url": page.url,
        "last_edited_time": page.last_edited_time,
    }}])
    w.link_mentioned_in(graph, "Document", [{"uid": uid, "record_key": record.record_key}])
    resolve_backlinks_for_target(
        graph, ledger, target_uid=uid, target_label="Document",
        target_provider="notion", url=page.url,
    )

    # Every page hangs from the workspace root. Nested pages additionally get
    # their exact page hierarchy so the canvas remains one connected graph.
    edges = [_edge(graph, "CONTAINS", "Workspace", "Document", root_uid, uid,
                   record.record_key, record.reference_time)]
    if page.parent_page_id:
        parent_uid = document_uid(workspace_id, page.parent_page_id)
        w.ensure_node_stub(graph, "Document", parent_uid)
        edges.append(_edge(graph, "PARENT_OF", "Document", "Document",
                           parent_uid, uid, record.record_key, record.reference_time))
    ledger.record_edges_batch(record.record_key, edges)
    resolve_exact_anchors(graph, ledger, record, uid, "Document")
    # A page that moved keeps its text but not its record_key, and chunk ids
    # are namespaced by record_key -- without this the whole page would be
    # re-extracted for a rename alone.
    moved_from = (
        ledger.find_moved_from(record.record_key, prepared.content_hash)
        if prepared.action == RecordAction.INSERT else None
    )
    diff = ledger.save_chunks(
        record.record_key, [(c.chunk_id, c.chunk_index, c.text) for c in prepared.chunks],
        adopt_from=moved_from.record_key if moved_from else None,
    )
    if moved_from:
        logger.info("page %s looks moved from %s", page.title, moved_from.record_key)
    if diff.kept or diff.superseded or diff.reused_done:
        logger.info(
            "page %s chunks: %d new, %d unchanged (%d already extracted -- no LLM call), %d superseded",
            page.title, diff.added, diff.kept, diff.reused_done, diff.superseded,
        )
    ledger.commit(record.record_key, prepared.content_hash, primary_node_uid=uid,
                  semantic_status=SemanticStatus.PENDING if prepared.chunks else SemanticStatus.NOT_APPLICABLE)
    return prepared.action
