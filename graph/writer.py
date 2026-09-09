"""Batched Cypher writer — `UNWIND ... MERGE`, never one query per row
(plan.md §3, §4). Every pattern here was verified directly against the
running FalkorDB before being wrapped (see CHECKLIST.md Block 3):
`ON CREATE`/`ON MATCH` + `+=` map-merge, `vecf32()` embedding writes from a
batch parameter, and the temporal supersession pattern (invalidate the live
edge, then let the next `upsert_fact_edges` call create the replacement).

Node identity is a deterministic `uid = uuid5(label + canonical identity)`
(plan.md §2.2) — callers compute it with `make_uid()` before building rows, so
the same source record always MERGEs onto the same node instead of duplicating.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from falkordb import Graph

_UID_NAMESPACE = uuid.UUID("2f9c9b0e-6c2a-4f7b-9b0a-8e2c3f6a1d4e")


def make_uid(label: str, *identity_parts: str) -> str:
    """Deterministic node identity. Same (label, identity) always -> same uid,
    so re-ingesting a record MERGEs onto the existing node instead of
    duplicating it."""
    key = label + "|" + "|".join(identity_parts)
    return str(uuid.uuid5(_UID_NAMESPACE, key))


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _label(value: str) -> str:
    """Cypher labels/relationship types can't contain spaces or most
    punctuation, and can't be parameterized — they're always interpolated.
    Values come from our own controlled label/relation lists (plan.md §2.2,
    §2.3), never from raw source text, but sanitize anyway as defense in depth."""
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in (value or "Entity"))
    cleaned = cleaned.strip("_") or "Entity"
    return cleaned if (cleaned[0].isalpha() or cleaned[0] == "_") else f"_{cleaned}"


def upsert_source_records(graph: Graph, rows: list[dict[str, Any]]) -> None:
    """rows: dicts with at least `record_key` — every other key becomes a
    property (provider, connection_id, entity_type, external_id, name, url,
    content_hash, semantic_status, updated_at — plan.md §2.1)."""
    if not rows:
        return
    graph.query(
        """
        UNWIND $rows AS row
        MERGE (s:SourceRecord {record_key: row.record_key})
        ON CREATE SET s.ingested_at = $now
        SET s += row
        """,
        params={"rows": rows, "now": now_iso()},
    )


def upsert_entities(graph: Graph, label: str, rows: list[dict[str, Any]]) -> None:
    """rows: {uid, props: {...}}. `first_seen_at` is set once on creation and
    never overwritten; `last_seen_at` bumps on every re-ingestion."""
    if not rows:
        return
    graph.query(
        f"""
        UNWIND $rows AS row
        MERGE (n:{_label(label)} {{uid: row.uid}})
        ON CREATE SET n.first_seen_at = $now, n += row.props
        ON MATCH SET n.last_seen_at = $now, n += row.props
        """,
        params={"rows": rows, "now": now_iso()},
    )


def upsert_entity_embeddings(graph: Graph, label: str, rows: list[dict[str, Any]]) -> None:
    """rows: {uid, embedding: list[float]}. Only call this for content-bearing
    labels (plan.md §2.5 VECTOR_LABELS) — embedding every structural node is
    the RAM cost §4 warns about."""
    if not rows:
        return
    graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (n:{_label(label)} {{uid: row.uid}})
        SET n.embedding = vecf32(row.embedding)
        """,
        params={"rows": rows},
    )


def ensure_node_stub(graph: Graph, label: str, uid: str) -> None:
    """Bare `MERGE` with no properties — guarantees a node exists so an edge
    to it can be written even if the record that fully describes it hasn't
    been processed yet in this sync (plan.md §4: within one project sync,
    Jira returns issues ordered by `updated ASC`, so an issue can reference
    one not yet reached). When that record IS processed, `upsert_entities`'s
    `ON MATCH` fills in real properties on this same node — nothing is lost,
    the stub is just a placeholder identity reserved early."""
    graph.query(
        f"MERGE (n:{_label(label)} {{uid: $uid}})",
        params={"uid": uid},
    )


def link_mentioned_in(graph: Graph, label: str, rows: list[dict[str, Any]]) -> None:
    """rows: {uid, record_key} — entity -> :SourceRecord provenance edge
    (plan.md §2.2). Presence-only fact, no temporal fields needed."""
    if not rows:
        return
    graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (n:{_label(label)} {{uid: row.uid}})
        MATCH (s:SourceRecord {{record_key: row.record_key}})
        MERGE (n)-[:MENTIONED_IN]->(s)
        """,
        params={"rows": rows},
    )


def upsert_fact_edges(
    graph: Graph, rel_type: str, from_label: str, to_label: str, rows: list[dict[str, Any]]
) -> None:
    """Create-or-confirm a live fact edge. rows: {from_uid, to_uid,
    source_record_keys, evidence, extraction_method, confidence}.

    `ON MATCH` clears `invalid_at` even if it was just set — re-confirming an
    edge means it's true again/still, which matters for the single-valued-fact
    pattern: caller calls `supersede_fact_edges` first (invalidates whatever
    is currently live out of `from_uid`), then this. If the new target is the
    SAME as before, this MERGEs onto that just-invalidated edge and revives it
    (verified: an unchanged Jira assignee across two syncs must stay live, not
    get marked invalid by its own reconfirmation). If the new target DIFFERS,
    this creates a fresh live edge via `ON CREATE` while the old one — a
    different (a,rel,b) triple — stays invalidated from the supersede call.

    Calling this WITHOUT superseding first just adds/confirms one edge among
    possibly several, which is correct for multi-valued facts (e.g. a
    WorkItem can BLOCK several others) and wrong for single-valued ones —
    that distinction is the caller's responsibility.
    """
    if not rows:
        return
    graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (a:{_label(from_label)} {{uid: row.from_uid}})
        MATCH (b:{_label(to_label)} {{uid: row.to_uid}})
        MERGE (a)-[r:{_label(rel_type)}]->(b)
        ON CREATE SET
            r.valid_at = $now, r.invalid_at = null, r.first_seen_at = $now,
            r.source_record_keys = row.source_record_keys,
            r.evidence = row.evidence, r.extraction_method = row.extraction_method,
            r.confidence = row.confidence, r.last_confirmed_at = $now
        ON MATCH SET
            r.last_confirmed_at = $now, r.invalid_at = null,
            r.source_record_keys = CASE
                WHEN row.source_record_keys[0] IN coalesce(r.source_record_keys, [])
                THEN r.source_record_keys
                ELSE coalesce(r.source_record_keys, []) + row.source_record_keys
            END
        """,
        params={"rows": rows, "now": now_iso()},
    )


def remove_record_support(graph: Graph, record_key: str, rows: list[dict[str, str]]) -> None:
    """Remove one SourceRecord from known edges without dropping support
    contributed by other records. Edges with no support left are invalidated."""
    by_type: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_type.setdefault(row["rel_type"], []).append(row)
    for rel_type, typed_rows in by_type.items():
        graph.query(
            f"""
            UNWIND $rows AS row
            MATCH (a {{uid: row.from_uid}})-[r:{_label(rel_type)}]->(b {{uid: row.to_uid}})
            WITH r, [key IN coalesce(r.source_record_keys, []) WHERE key <> $record_key] AS remaining
            SET r.source_record_keys = remaining,
                r.invalid_at = CASE WHEN size(remaining) = 0 THEN $now ELSE r.invalid_at END
            """,
            params={"rows": typed_rows, "record_key": record_key, "now": now_iso()},
        )


def unlink_record_mentions_except(graph: Graph, record_key: str, primary_uid: str) -> None:
    """Clear derived provenance before an updated record is re-extracted."""
    graph.query(
        """
        MATCH (n)-[r:MENTIONED_IN]->(s:SourceRecord {record_key: $record_key})
        WHERE n.uid <> $primary_uid
        DELETE r
        """,
        params={"record_key": record_key, "primary_uid": primary_uid},
    )


def invalidate_edges_by_uid_pairs(graph: Graph, rows: list[dict[str, Any]]) -> None:
    """Set `invalid_at` on specific edges identified by (from_uid, to_uid,
    rel_type), without needing to know either endpoint's label — `uid` is
    unique per node regardless of label (verified), so `MATCH (x {uid:...})`
    unambiguously finds one node. Used for deletion cleanup (plan.md §4.1):
    `connectors.core.ledger.edges_for_record` only stores uids and rel_type,
    not labels, so this is the matching invalidation primitive.
    rows: {from_uid, to_uid, rel_type}."""
    if not rows:
        return
    graph.query(
        """
        UNWIND $rows AS row
        MATCH (a {uid: row.from_uid})-[r]->(b {uid: row.to_uid})
        WHERE type(r) = row.rel_type
        SET r.invalid_at = $now
        """,
        params={"rows": rows, "now": now_iso()},
    )


def delete_node(graph: Graph, uid: str) -> None:
    """Hard delete a node and every edge touching it (any direction/type).
    Matches by `uid` regardless of label, like `invalidate_edges_by_uid_pairs`."""
    graph.query("MATCH (n {uid: $uid}) DETACH DELETE n", params={"uid": uid})


def delete_source_record(graph: Graph, record_key: str) -> None:
    graph.query("MATCH (s:SourceRecord {record_key: $record_key}) DETACH DELETE s", params={"record_key": record_key})


def delete_orphaned_entities(graph: Graph, labels: list[str]) -> int:
    """Delete any node of these labels no longer linked to a live
    :SourceRecord via MENTIONED_IN — e.g. a Decision/Term/System that was
    only ever mentioned by records that have since been deleted. Structural
    entities (WorkItem/Project) are never passed here; they're deleted
    directly by uid since deleting a connection's records already identifies
    them precisely."""
    if not labels:
        return 0
    total = 0
    for label in labels:
        result = graph.query(
            f"MATCH (n:{_label(label)}) WHERE NOT (n)-[:MENTIONED_IN]->(:SourceRecord) DETACH DELETE n RETURN count(n)"
        )
        total += result.result_set[0][0] if result.result_set else 0
    return total


def mark_source_record_deleted(graph: Graph, record_key: str) -> None:
    """Deletion keeps the :SourceRecord node (audit trail — plan.md §6b
    philosophy of preserving history) but stamps when it stopped being live."""
    graph.query(
        "MATCH (s:SourceRecord {record_key: $record_key}) SET s.deleted_at = $now",
        params={"record_key": record_key, "now": now_iso()},
    )


def supersede_fact_edges(
    graph: Graph, rel_type: str, from_label: str, to_label: str, from_uids: list[str]
) -> None:
    """Set `invalid_at` on the current live edge(s) of this type out of each
    `from_uid`. Call this BEFORE `upsert_fact_edges` writes the replacement —
    plan.md §3 Pass A step 5 (temporal diff on changed fields)."""
    if not from_uids:
        return
    graph.query(
        f"""
        UNWIND $from_uids AS from_uid
        MATCH (a:{_label(from_label)} {{uid: from_uid}})-[r:{_label(rel_type)}]->(:{_label(to_label)})
        WHERE r.invalid_at IS NULL
        SET r.invalid_at = $now
        """,
        params={"from_uids": from_uids, "now": now_iso()},
    )
