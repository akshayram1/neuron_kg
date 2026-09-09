"""Read the graph for the demo UI's canvas — plain Cypher against our own
schema (plan.md §2), not Graphiti's typed `EntityNode`/`EntityEdge` accessors.

Replaces `graphiti_context_explorer/ui/graph_view.py`. That file's actual
rendering logic (`render_pyvis_html`, `TYPE_COLORS`, `GROUP_SHAPES`,
`filter_graph`) belonged to the retired Streamlit app (plan.md §6a.3) — the
React frontend renders client-side, so only the data-fetch half survives here,
reshaped for a single unified graph instead of one-FalkorDB-graph-per-source.

"documents" per node/edge — the old version resolved Graphiti episode
`source_description`s. Here that's just the linked `:SourceRecord.name`s via
`MENTIONED_IN` / `source_record_keys`, no separate episode-lookup pass needed.
"""

from __future__ import annotations

from typing import Any

from falkordb import Graph


def fetch_graph(graph: Graph, providers: list[str] | None = None) -> dict[str, Any]:
    """providers=None returns everything; otherwise filters to SourceRecords
    for those providers and the entities/facts connected to them. `group` on
    each node/edge is a provider list, not a physical-graph selector (plan.md
    §6a.2 — the old `group_id` concept doesn't apply to a unified graph)."""
    provider_filter = "AND sr.provider IN $providers" if providers else ""

    node_rows = graph.query(
        f"""
        MATCH (n)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL {provider_filter}
        WITH n, collect(DISTINCT sr.provider) AS providers, collect(DISTINCT sr.name) AS documents
        RETURN n.uid AS id, labels(n)[0] AS type, n.name AS label,
               n.search_text AS search_text, n.definition AS definition, n.statement AS statement,
               n.purpose AS purpose, providers, documents
        """,
        params={"providers": providers} if providers else {},
    ).result_set

    nodes = []
    for row in node_rows:
        node_id, node_type, label, search_text, definition, statement, purpose, node_providers, documents = row
        summary = definition or statement or purpose or (search_text[:200] if search_text else "") or ""
        nodes.append({
            "id": node_id, "label": label or node_id, "type": node_type,
            "group": node_providers[0] if node_providers else "",
            "summary": summary, "documents": sorted(documents),
        })

    known_ids = {n["id"] for n in nodes}
    edge_provider_filter = "AND sr.provider IN $providers" if providers else ""
    edge_rows = graph.query(
        f"""
        MATCH (a)-[r]->(b)
        WHERE type(r) <> 'MENTIONED_IN' AND r.invalid_at IS NULL
        OPTIONAL MATCH (a)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL {edge_provider_filter}
        RETURN DISTINCT a.uid AS source, b.uid AS target, type(r) AS label, r.evidence AS evidence,
               r.valid_at AS valid_at, r.invalid_at AS invalid_at,
               r.source_record_keys AS source_record_keys, r.extraction_method AS method,
               r.confidence AS confidence
        """,
        params={"providers": providers} if providers else {},
    ).result_set

    edges = []
    for row in edge_rows:
        source, target, label, evidence, valid_at, invalid_at, source_record_keys, method, confidence = row
        if source not in known_ids or target not in known_ids:
            continue
        edges.append({
            "id": f"{source}:{label}:{target}",
            "source": source, "target": target, "label": label,
            "fact": evidence or f"{label} ({method or 'deterministic'})",
            "group": "", "validAt": valid_at, "invalidAt": invalid_at,
            "superseded": invalid_at is not None,
            "documents": [], "confidence": confidence,
        })

    return {"nodes": nodes, "edges": edges}


def fetch_sources(graph: Graph) -> list[dict[str, Any]]:
    """One row per :SourceRecord — backs an "/api/sources" listing."""
    rows = graph.query(
        """
        MATCH (sr:SourceRecord)
        RETURN sr.record_key AS record_key, sr.provider AS provider, sr.entity_type AS entity_type,
               sr.name AS name, sr.url AS url, sr.ingested_at AS ingested_at, sr.deleted_at AS deleted_at
        ORDER BY sr.ingested_at DESC
        """
    ).result_set
    return [
        {
            "recordKey": r[0], "provider": r[1], "entityType": r[2], "name": r[3],
            "url": r[4], "ingestedAt": r[5], "deletedAt": r[6],
        }
        for r in rows
    ]
