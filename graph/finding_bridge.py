"""Materializes ledger-tracked ingestion findings as FalkorDB `Finding` nodes.

`connectors.core.ledger.ConnectorLedger.record_ingestion_assessments` persists
a validated LLM ingestion judgement (`graph.profiles.IngestionAssessment`,
`should_flag=True`) durably in the ledger's own SQL tables. Nothing about
that write touches FalkorDB, so it alone does not make a finding show up
anywhere the UI actually looks:

  - the graph canvas's "Findings" filter (`type === "Finding"` in the
    frontend) reads `graph.graph_view.fetch_graph`, which only returns nodes
    reached by a `MENTIONED_IN` edge from a live `SourceRecord`;
  - `graph.structured_query`'s finding-lane matches `(:Finding)-[:MENTIONED_IN]
    ->(:SourceRecord)` directly in Cypher;
  - hybrid search only ranks nodes that exist in FalkorDB with a
    `search_text`/embedding, same as every other entity type.

This module is the bridge: it reads open ledger findings and upserts them as
`Finding` nodes the same way `graph.jira_pipeline` etc. write any other
entity -- `graph.writer.upsert_entities` + `link_mentioned_in`, plus an
immediate embedding via `graph.jira_pipeline._embed_now` so they are
retrievable by both the fulltext and vector legs of `graph.search`
(`Finding` is already in `graph.schema.FULLTEXT_LABELS`/`VECTOR_LABELS`).

It is a separate, explicit sync step rather than something
`record_ingestion_assessments` does itself, because that method only has a
SQL connection (no `Graph`) -- `graph.semantic_pass.run_semantic_pass` calls
it per chunk with no graph object to pass through. Call this after a route's
semantic pass returns, the same way `graph.jira_pipeline.
delete_orphaned_shared_entities` is called once after a sync's writes.
"""

from __future__ import annotations

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger
from graph import vector_store
from graph import writer as w
from graph.jira_pipeline import _embed_now


def sync_ledger_findings(
    graph: Graph, ledger: ConnectorLedger, *,
    record_prefix: str | None = None, collection: str = vector_store.COLLECTION,
) -> int:
    """Upsert every ledger finding (any status -- `stale`/`resolved` stay
    visible with that status, they are not deleted) as a `Finding` node.

    Returns how many were synced. Safe to call repeatedly: every write here
    is a MERGE keyed on a uid derived from `finding_key`, so re-running after
    new findings appear only adds/updates, never duplicates.
    """
    findings = ledger.findings(record_prefix=record_prefix, limit=10_000)
    if not findings:
        return 0

    entity_rows = []
    mention_rows = []
    embeds = []
    for finding in findings:
        uid = w.make_uid("Finding", finding.finding_key)
        search_text = "\n\n".join(
            part for part in (finding.title, finding.summary, finding.reasoning) if part
        )
        entity_rows.append({"uid": uid, "props": {
            "name": finding.title, "search_text": search_text,
            "kind": finding.kind, "status": finding.status, "severity": finding.severity,
            "confidence": finding.confidence,
            "created_at": finding.created_at, "stale_at": finding.stale_at,
        }})
        mention_rows.append({"uid": uid, "record_key": finding.record_key})
        if search_text:
            embeds.append((uid, search_text, finding.title))

    w.upsert_entities(graph, "Finding", entity_rows)
    w.link_mentioned_in(graph, "Finding", mention_rows)
    for uid, search_text, title in embeds:
        _embed_now(uid, "Finding", search_text, collection=collection, name=title)
    return len(entity_rows)
