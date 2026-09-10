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
    never overwritten; `last_seen_at` bumps on every re-ingestion.

    `None` values are stripped from `props` before the write — Cypher's
    `n += {...}` map-merge sets a key to null rather than skipping it, so a
    field that was populated in an earlier extraction (e.g. a Decision's
    `rationale`) would otherwise be silently erased the moment a *later*
    write for the same node happens not to know that field (verified against
    a real write: a stored `url` was nulled out by a second call passing
    `url: None`). Once a property is known, a later "don't know" must not
    erase it."""
    if not rows:
        return
    clean_rows = [
        {"uid": row["uid"], "props": {k: v for k, v in row["props"].items() if v is not None}}
        for row in rows
    ]
    graph.query(
        f"""
        UNWIND $rows AS row
        MERGE (n:{_label(label)} {{uid: row.uid}})
        ON CREATE SET n.first_seen_at = $now, n += row.props
        ON MATCH SET n.last_seen_at = $now, n += row.props
        """,
        params={"rows": clean_rows, "now": now_iso()},
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
    rows = [
        {**row, "fact_uid": row.get("fact_uid") or make_uid(
            "Fact", str(row["from_uid"]), rel_type, str(row["to_uid"])
        )}
        for row in rows
    ]
    graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (a:{_label(from_label)} {{uid: row.from_uid}})
        MATCH (b:{_label(to_label)} {{uid: row.to_uid}})
        MERGE (a)-[r:{_label(rel_type)}]->(b)
        ON CREATE SET
            r.valid_at = coalesce(row.valid_at, $now), r.invalid_at = null, r.first_seen_at = $now,
            r.source_record_keys = row.source_record_keys,
            r.evidence = row.evidence, r.extraction_method = row.extraction_method,
            r.confidence = row.confidence, r.last_confirmed_at = $now,
            r.chunk_id = row.chunk_id, r.chunk_hash = row.chunk_hash,
            r.extractor_version = row.extractor_version, r.model = row.model,
            r.fact_uid = row.fact_uid,
            r.derived = coalesce(row.derived, false),
            r.derived_rule = row.derived_rule,
            r.premise_fact_uids = row.premise_fact_uids,
            r.ended_unknown = coalesce(row.ended_unknown, false),
            r.attested_from = row.attested_from
        ON MATCH SET
            r.last_confirmed_at = $now,
            r.valid_at = CASE WHEN r.invalid_at IS NULL THEN r.valid_at ELSE coalesce(row.valid_at, $now) END,
            r.invalid_at = null,
            r.derived = CASE WHEN row.derived IS NULL THEN r.derived ELSE row.derived END,
            r.derived_rule = CASE WHEN row.derived_rule IS NULL THEN r.derived_rule ELSE row.derived_rule END,
            r.premise_fact_uids = CASE WHEN row.premise_fact_uids IS NULL THEN r.premise_fact_uids ELSE row.premise_fact_uids END,
            r.ended_unknown = CASE WHEN row.ended_unknown IS NULL THEN r.ended_unknown ELSE row.ended_unknown END,
            r.attested_from = CASE WHEN row.attested_from IS NULL THEN r.attested_from ELSE row.attested_from END,
            r.source_record_keys = CASE
                WHEN row.source_record_keys[0] IN coalesce(r.source_record_keys, [])
                THEN r.source_record_keys
                ELSE coalesce(r.source_record_keys, []) + row.source_record_keys
            END,
            r.chunk_id = CASE WHEN row.chunk_id IS NULL THEN r.chunk_id ELSE row.chunk_id END,
            r.chunk_hash = CASE WHEN row.chunk_hash IS NULL THEN r.chunk_hash ELSE row.chunk_hash END,
            r.extractor_version = CASE WHEN row.extractor_version IS NULL THEN r.extractor_version ELSE row.extractor_version END,
            r.model = CASE WHEN row.model IS NULL THEN r.model ELSE row.model END,
            r.fact_uid = row.fact_uid
        """,
        params={"rows": rows, "now": now_iso()},
    )


def _archive_history_rows(graph: Graph, rel_type: str, rows: list[dict[str, Any]]) -> None:
    """Snapshot live relationship intervals before invalidation.

    Hot traversals continue using live edges; immutable FactHistory nodes keep
    every prior validity/transaction interval without scanning dead edges.
    """
    if not rows:
        return
    now = now_iso()
    result = graph.query(
        f"""
        UNWIND $rows AS row
        MATCH (a {{uid: row.from_uid}})-[r:{_label(rel_type)}]->(b {{uid: row.to_uid}})
        WHERE r.invalid_at IS NULL
        RETURN a.uid, a.name, b.uid, b.name, r.valid_at, row.valid_to, r.first_seen_at,
               r.last_confirmed_at, r.source_record_keys, r.evidence,
               r.extraction_method, r.confidence, r.chunk_id, r.chunk_hash,
               r.extractor_version, r.model
        """,
        params={"rows": rows},
    ).result_set
    history_rows = []
    for item in result:
        (from_uid, from_name, to_uid, to_name, valid_from, valid_to, observed_from,
         last_confirmed, source_keys, evidence, method, confidence, chunk_id,
         chunk_hash, extractor_version, model) = item
        history_rows.append({
            "uid": make_uid("FactHistory", from_uid, rel_type, to_uid, str(valid_from or ""), now),
            "props": {
                "name": f"{from_name or from_uid} {rel_type} {to_name or to_uid}",
                "from_uid": from_uid, "to_uid": to_uid, "relation": rel_type,
                "fact_uid": make_uid("Fact", from_uid, rel_type, to_uid),
                "valid_from": valid_from, "valid_to": valid_to or now,
                "observed_from": observed_from, "observed_to": now,
                "last_confirmed_at": last_confirmed,
                "source_record_keys": source_keys or [], "evidence": evidence,
                "extraction_method": method, "confidence": confidence,
                "chunk_id": chunk_id, "chunk_hash": chunk_hash,
                "extractor_version": extractor_version, "model": model,
            },
        })
    upsert_entities(graph, "FactHistory", history_rows)


def upsert_history_intervals(graph: Graph, rel_type: str, rows: list[dict[str, Any]]) -> None:
    """Write closed world-axis intervals as FactHistory without touching live edges.

    Used for connector changelogs: the source already knows assignee A held
    from T0 to T1, so we record that interval instead of waiting for the next
    sync to supersede it. Uid is deterministic on (endpoints, window) so a
    re-sync MERGEs instead of duplicating.
    rows: from_uid, to_uid, from_name, to_name, valid_from, valid_to,
    source_record_keys, evidence?, observed_from?
    """
    if not rows:
        return
    now = now_iso()
    history_rows = []
    for row in rows:
        from_uid, to_uid = str(row["from_uid"]), str(row["to_uid"])
        valid_from, valid_to = row.get("valid_from"), row.get("valid_to")
        history_rows.append({
            "uid": make_uid(
                "FactHistory", from_uid, rel_type, to_uid,
                str(valid_from or ""), str(valid_to or ""),
            ),
            "props": {
                "name": (
                    f"{row.get('from_name') or from_uid} {rel_type} "
                    f"{row.get('to_name') or to_uid}"
                ),
                "from_uid": from_uid, "to_uid": to_uid, "relation": rel_type,
                "fact_uid": make_uid("Fact", from_uid, rel_type, to_uid),
                "valid_from": valid_from, "valid_to": valid_to,
                "observed_from": row.get("observed_from") or now,
                "observed_to": row.get("observed_to") or now,
                "last_confirmed_at": now,
                "source_record_keys": row.get("source_record_keys") or [],
                "evidence": row.get("evidence"),
                "extraction_method": row.get("extraction_method") or "changelog",
                "confidence": row.get("confidence", 1.0),
                "ended_unknown": row.get("ended_unknown") or False,
                "attested_from": row.get("attested_from") or valid_from,
            },
        })
    upsert_entities(graph, "FactHistory", history_rows)


def remove_record_support(graph: Graph, record_key: str, rows: list[dict[str, Any]]) -> None:
    """Remove one SourceRecord from known edges without dropping support
    contributed by other records. Edges with no support left are invalidated."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["rel_type"], []).append(row)
    for rel_type, typed_rows in by_type.items():
        candidates = graph.query(
            f"""
            UNWIND $rows AS row
            MATCH (a {{uid: row.from_uid}})-[r:{_label(rel_type)}]->(b {{uid: row.to_uid}})
            WHERE r.invalid_at IS NULL
            RETURN a.uid, b.uid, r.source_record_keys
            """,
            params={"rows": typed_rows},
        ).result_set
        to_archive = [
            {"from_uid": from_uid, "to_uid": to_uid,
             "valid_to": next((row.get("valid_to") for row in typed_rows
                               if row["from_uid"] == from_uid and row["to_uid"] == to_uid), None)}
            for from_uid, to_uid, keys in candidates
            if not [key for key in (keys or []) if key != record_key]
        ]
        _archive_history_rows(graph, rel_type, to_archive)
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
    live_rows = graph.query(
        f"""
        UNWIND $from_uids AS from_uid
        MATCH (a:{_label(from_label)} {{uid: from_uid}})-[r:{_label(rel_type)}]->(b:{_label(to_label)})
        WHERE r.invalid_at IS NULL
        RETURN a.uid, b.uid
        """,
        params={"from_uids": from_uids},
    ).result_set
    _archive_history_rows(
        graph, rel_type,
        [{"from_uid": row[0], "to_uid": row[1]} for row in live_rows],
    )
    graph.query(
        f"""
        UNWIND $from_uids AS from_uid
        MATCH (a:{_label(from_label)} {{uid: from_uid}})-[r:{_label(rel_type)}]->(:{_label(to_label)})
        WHERE r.invalid_at IS NULL
        SET r.invalid_at = $now
        """,
        params={"from_uids": from_uids, "now": now_iso()},
    )
