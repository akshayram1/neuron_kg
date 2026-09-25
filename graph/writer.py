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

from graph.time_axis import parse_iso

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
    graph: Graph,
    rel_type: str,
    from_label: str,
    to_label: str,
    rows: list[dict[str, Any]],
    *,
    revive: bool = True,
) -> None:
    """Create-or-confirm a live fact edge. rows: {from_uid, to_uid,
    source_record_keys, evidence, extraction_method, confidence, ...}.
    Optional per-row fields: `pinned` (bool), `decay_class` (str),
    `attested_by_record` (str, a source record key -- see below),
    `projection_status` ("live" | "pending_review").

    `revive` (25-plan.md Phase 5 §5.0.1, default `True` — every existing
    caller keeps today's exact behavior unless it opts in):

    When `revive=True`, `ON MATCH` clears `invalid_at` even if it was just
    set — re-confirming an edge means it's true again/still, which matters
    for the single-valued-fact pattern: caller calls `supersede_fact_edges`
    first (invalidates whatever is currently live out of `from_uid`), then
    this. If the new target is the SAME as before, this MERGEs onto that
    just-invalidated edge and revives it (verified: an unchanged Jira
    assignee across two syncs must stay live, not get marked invalid by its
    own reconfirmation). If the new target DIFFERS, this creates a fresh
    live edge via `ON CREATE` while the old one — a different (a,rel,b)
    triple — stays invalidated from the supersede call.

    When `revive=False`, a match on a previously closed/corrected edge
    (`r.invalid_at IS NOT NULL`) is left closed: `invalid_at` is NOT
    cleared and `valid_at` is NOT changed. The write may still attach new
    provenance (append to `source_record_keys`, update `evidence`/
    `confidence`/etc.) — it just cannot reopen history. Text facts and
    historical/backfill ingestion (25-plan.md §5.1/§5.2) pass `revive=False`
    so that text re-asserting a claim identical to one already closed
    cannot silently un-close it. A match on a still-live edge behaves the
    same regardless of `revive` (there is nothing to "not reopen").

    Calling this WITHOUT superseding first just adds/confirms one edge among
    possibly several, which is correct for multi-valued facts (e.g. a
    WorkItem can BLOCK several others) and wrong for single-valued ones —
    that distinction is the caller's responsibility.

    `last_confirmed_at` (25-plan.md §5.6, four clocks) only bumps on a
    genuinely content-changing match: new/changed `evidence`, changed
    `confidence`, a source record key not already recorded, or a revive
    that actually reopens a closed edge. A KEEP-sync that re-asserts
    identical content leaves `last_confirmed_at` untouched — this is a
    behavior change from the previous unconditional `r.last_confirmed_at =
    $now` on every match; see the module-level notes in the Phase 5 writer
    report for why.

    `attested_from` stays an ISO timestamp only (never a record key — see
    `graph/time_axis.py`, which parses it as a date). A source record key
    that documents *who/what confirmed this* belongs in the separate
    `attested_by_record` field instead.
    """
    if not rows:
        return
    rows = [
        {**row, "fact_uid": row.get("fact_uid") or make_uid(
            "Fact", str(row["from_uid"]), rel_type, str(row["to_uid"])
        )}
        for row in rows
    ]
    content_changed = """(
                (row.evidence IS NOT NULL AND row.evidence <> r.evidence)
                OR (row.confidence IS NOT NULL AND row.confidence <> r.confidence)
                OR (row.source_record_keys IS NOT NULL AND size(row.source_record_keys) > 0
                    AND NOT row.source_record_keys[0] IN coalesce(r.source_record_keys, []))
                OR (r.invalid_at IS NOT NULL AND $revive)
            )"""
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
            r.attested_from = row.attested_from,
            r.attested_by_record = row.attested_by_record,
            r.adopted_batch = row.adopted_batch,
            r.pinned = coalesce(row.pinned, false),
            r.decay_class = row.decay_class,
            r.assertion_status = 'live',
            r.projection_status = coalesce(row.projection_status, 'live')
        ON MATCH SET
            r.last_confirmed_at = CASE WHEN {content_changed} THEN $now ELSE r.last_confirmed_at END,
            r.valid_at = CASE
                WHEN NOT $revive THEN r.valid_at
                WHEN r.invalid_at IS NULL THEN r.valid_at
                ELSE coalesce(row.valid_at, $now)
            END,
            r.invalid_at = CASE WHEN $revive THEN null ELSE r.invalid_at END,
            r.derived = CASE WHEN row.derived IS NULL THEN r.derived ELSE row.derived END,
            r.derived_rule = CASE WHEN row.derived_rule IS NULL THEN r.derived_rule ELSE row.derived_rule END,
            r.premise_fact_uids = CASE WHEN row.premise_fact_uids IS NULL THEN r.premise_fact_uids ELSE row.premise_fact_uids END,
            r.ended_unknown = CASE WHEN row.ended_unknown IS NULL THEN r.ended_unknown ELSE row.ended_unknown END,
            r.attested_from = CASE WHEN row.attested_from IS NULL THEN r.attested_from ELSE row.attested_from END,
            r.attested_by_record = CASE WHEN row.attested_by_record IS NULL THEN r.attested_by_record ELSE row.attested_by_record END,
            r.adopted_batch = CASE WHEN row.adopted_batch IS NULL THEN r.adopted_batch ELSE row.adopted_batch END,
            r.pinned = CASE WHEN row.pinned = true THEN true ELSE coalesce(r.pinned, false) END,
            r.decay_class = CASE WHEN row.decay_class IS NULL THEN r.decay_class ELSE row.decay_class END,
            r.projection_status = CASE WHEN row.projection_status IS NULL THEN coalesce(r.projection_status, 'live') ELSE row.projection_status END,
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
        params={"rows": rows, "now": now_iso(), "revive": revive},
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


# ---------------------------------------------------------------------------
# 25-plan.md Phase 5 §5.0 — writer contract for text facts.
#
# `supersede_fact_edges` above is subject-wide: it invalidates *every* live
# edge of a relation type out of a `from_uid`, which is correct for
# deterministic/single-valued facts (a Jira assignee, a WorkItem's status)
# where the caller already knows "whatever is live now is about to be
# replaced". Text facts don't have that guarantee — `resolve_text_fact`
# (§5.2, built after this) identifies a *specific* conflicting fact by its
# `fact_uid` via a candidate query, and closing/correcting anything wider
# than that one edge would be wrong. `close_fact` and `correct_fact` are
# that fact_uid-scoped alternative; both reuse the same `FactHistory`
# archival shape as `_archive_history_rows`/`upsert_history_intervals`
# rather than inventing a second history mechanism.
# ---------------------------------------------------------------------------


def _find_live_fact(graph: Graph, fact_uid: str) -> dict[str, Any]:
    """Locate the one live edge carrying `fact_uid`, regardless of its
    relationship type or endpoint labels (matches `uid` the same
    label-agnostic way `invalidate_edges_by_uid_pairs`/`delete_node` do).

    Raises `ValueError` — never silently no-ops — if no live edge matches,
    or if more than one does (should not happen: `fact_uid` is deterministic
    per (from_uid, rel_type, to_uid) via `make_uid`, so two *live* matches
    would mean a real data bug, not a normal race)."""
    result = graph.query(
        """
        MATCH (a)-[r]->(b)
        WHERE r.fact_uid = $fact_uid AND r.invalid_at IS NULL
        RETURN a.uid, a.name, type(r), b.uid, b.name, r.valid_at, r.first_seen_at,
               r.last_confirmed_at, r.source_record_keys, r.evidence,
               r.extraction_method, r.confidence, r.chunk_id, r.chunk_hash,
               r.extractor_version, r.model
        """,
        params={"fact_uid": fact_uid},
    ).result_set
    if not result:
        raise ValueError(f"no live fact edge found for fact_uid={fact_uid!r}")
    if len(result) > 1:
        raise ValueError(
            f"ambiguous fact_uid={fact_uid!r}: matched {len(result)} live edges"
        )
    (from_uid, from_name, rel_type, to_uid, to_name, valid_at, first_seen_at,
     last_confirmed_at, source_keys, evidence, method, confidence, chunk_id,
     chunk_hash, extractor_version, model) = result[0]
    return {
        "from_uid": from_uid, "from_name": from_name, "rel_type": rel_type,
        "to_uid": to_uid, "to_name": to_name, "valid_at": valid_at,
        "first_seen_at": first_seen_at, "last_confirmed_at": last_confirmed_at,
        "source_record_keys": source_keys, "evidence": evidence,
        "extraction_method": method, "confidence": confidence,
        "chunk_id": chunk_id, "chunk_hash": chunk_hash,
        "extractor_version": extractor_version, "model": model,
    }


def close_fact(graph: Graph, fact_uid: str, valid_to: str, reason: str = "superseded") -> None:
    """§5.0.2 — close exactly ONE live fact (found by `fact_uid`, never
    subject-wide) at the caller-supplied WORLD-time instant `valid_to` — not
    `now()`. Archives the pre-close snapshot into `FactHistory` (same shape
    `_archive_history_rows` produces, plus `close_reason`), then sets
    `r.invalid_at = valid_to` on the live edge.

    This is the `newer_state` shape from §5.0's own table: "old fact valid
    until `new.valid_at`" is exactly `close_fact(old.fact_uid, new.valid_at)`.
    `close_fact` never sets `assertion_status` — a closed fact was true and
    then stopped being true, which is a different claim than `correct_fact`'s
    "was never true" (kept distinct in storage per §5.0.3).

    Raises `ValueError` on a missing/ambiguous `fact_uid` (via
    `_find_live_fact`) and on an invalid interval (`valid_to` before the
    fact's own `valid_at`, when both parse as dates)."""
    fact = _find_live_fact(graph, fact_uid)
    parsed_to, parsed_from = parse_iso(valid_to), parse_iso(fact["valid_at"])
    if parsed_to is not None and parsed_from is not None and parsed_to < parsed_from:
        raise ValueError(
            f"close_fact: valid_to={valid_to!r} is before this fact's own "
            f"valid_at={fact['valid_at']!r} for fact_uid={fact_uid!r}"
        )
    now = now_iso()
    history_row = {
        "uid": make_uid(
            "FactHistory", fact["from_uid"], fact["rel_type"], fact["to_uid"],
            str(fact["valid_at"] or ""), now,
        ),
        "props": {
            "name": f"{fact['from_name'] or fact['from_uid']} {fact['rel_type']} "
                    f"{fact['to_name'] or fact['to_uid']}",
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"], "relation": fact["rel_type"],
            "fact_uid": fact_uid,
            "valid_from": fact["valid_at"], "valid_to": valid_to,
            "observed_from": fact["first_seen_at"], "observed_to": now,
            "last_confirmed_at": fact["last_confirmed_at"],
            "source_record_keys": fact["source_record_keys"] or [], "evidence": fact["evidence"],
            "extraction_method": fact["extraction_method"], "confidence": fact["confidence"],
            "chunk_id": fact["chunk_id"], "chunk_hash": fact["chunk_hash"],
            "extractor_version": fact["extractor_version"], "model": fact["model"],
            "close_reason": reason,
        },
    }
    upsert_entities(graph, "FactHistory", [history_row])
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}})-[r:{_label(fact['rel_type'])}]->(b {{uid: $to_uid}})
        WHERE r.fact_uid = $fact_uid
        SET r.invalid_at = $valid_to, r.close_reason = $reason
        """,
        params={
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"],
            "fact_uid": fact_uid, "valid_to": valid_to, "reason": reason,
        },
    )


def correct_fact(graph: Graph, fact_uid: str, corrected_by: str, observed_to: str) -> None:
    """§5.0.3 — mark a fact as WRONG, not merely superseded. Distinct from
    `close_fact` (which keeps the fact's old world-time validity intact):
    a corrected assertion "is excluded from every 'what was true' world-time
    query, including dates before the correction, because it was never true."

    Storage shape:
    - `FactHistory` archive row: `assertion_status="corrected"`,
      `corrected_by=<the new fact_uid>`, `correction_observed_at=observed_to`,
      and — this is the "still visible via record-time history until
      observed_to" mechanism the plan asks to verify — `observed_from` is
      the original `first_seen_at` and `observed_to` is the given
      `observed_to`, so `held_at(observed_from, observed_to, as_of)`
      (graph/time_axis.py, unmodified) already returns True for any `as_of`
      strictly before `observed_to` and False at/after it. No new record-time
      machinery is needed; the existing `observed_from`/`observed_to`
      interval on the archived row is exactly that machinery.
    - Live edge: `assertion_status="corrected"`, `corrected_by`,
      `correction_observed_at`, `metadata_revised_at` bumped (a review-style
      metadata change), and — the conservative choice documented here since
      the plan leaves the exact "how" to this task's judgment —
      `invalid_at` is set equal to `valid_at`, collapsing the live edge's
      world-time window to zero width. That is not a second "closing"
      mechanism: it doesn't encode "true until X" — it encodes "there is no
      instant at which `valid_at <= at < invalid_at` holds", i.e. never true
      under ordinary interval math, so a reader who has NOT yet adopted the
      §5.0.6 predicate (`graph/fact_predicates.py`) and only checks the
      historical `invalid_at IS NULL` idiom still treats it as not-live, and
      one who *does* check `holds_at(valid_at, invalid_at, at)` gets `False`
      for every `at`, not just dates after the correction. The authoritative
      check either way is `assertion_status != 'corrected'` from the
      centralized predicate; the zero-width interval is defense in depth for
      the ~23 call sites that don't use it yet (see `fact_predicates.py`).

    Raises `ValueError` on a missing/ambiguous `fact_uid` (via
    `_find_live_fact`, same guard as `close_fact`)."""
    fact = _find_live_fact(graph, fact_uid)
    now = now_iso()
    history_row = {
        "uid": make_uid(
            "FactHistory", fact["from_uid"], fact["rel_type"], fact["to_uid"],
            str(fact["valid_at"] or ""), now,
        ),
        "props": {
            "name": f"{fact['from_name'] or fact['from_uid']} {fact['rel_type']} "
                    f"{fact['to_name'] or fact['to_uid']}",
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"], "relation": fact["rel_type"],
            "fact_uid": fact_uid,
            "valid_from": fact["valid_at"], "valid_to": fact["valid_at"],
            "observed_from": fact["first_seen_at"], "observed_to": observed_to,
            "last_confirmed_at": fact["last_confirmed_at"],
            "source_record_keys": fact["source_record_keys"] or [], "evidence": fact["evidence"],
            "extraction_method": fact["extraction_method"], "confidence": fact["confidence"],
            "chunk_id": fact["chunk_id"], "chunk_hash": fact["chunk_hash"],
            "extractor_version": fact["extractor_version"], "model": fact["model"],
            "assertion_status": "corrected", "corrected_by": corrected_by,
            "correction_observed_at": observed_to,
        },
    }
    upsert_entities(graph, "FactHistory", [history_row])
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}})-[r:{_label(fact['rel_type'])}]->(b {{uid: $to_uid}})
        WHERE r.fact_uid = $fact_uid
        SET r.assertion_status = 'corrected',
            r.corrected_by = $corrected_by,
            r.correction_observed_at = $observed_to,
            r.metadata_revised_at = $now,
            r.invalid_at = r.valid_at
        """,
        params={
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"], "fact_uid": fact_uid,
            "corrected_by": corrected_by, "observed_to": observed_to, "now": now,
        },
    )


def confirm_fact(
    graph: Graph,
    fact_uid: str,
    *,
    at: str | None = None,
    source_record_key: str | None = None,
    evidence: str | None = None,
    confidence: float | None = None,
) -> None:
    """§5.0.1 — the narrow "confirm" primitive: attach new provenance to an
    existing LIVE fact edge (the §5.2 `duplicate` case — new evidence
    restates a fact already known) WITHOUT touching `valid_at`/`invalid_at`
    at all, ever.

    Judgment call (flagged per the task instructions): `upsert_fact_edges(
    revive=False)` already covers "don't reopen a closed edge", but its
    `ON MATCH` branch still has to decide, in one query, what a match on a
    CLOSED edge should do. `confirm_fact` is deliberately narrower and
    fact_uid-scoped (matching `close_fact`/`correct_fact`'s shape rather
    than `upsert_fact_edges`'s label/uid shape) so `resolve_text_fact` (§5.2)
    has one call, for one already-identified live fact, that provably cannot
    touch the temporal fields — there is no code path in this function that
    writes `valid_at` or `invalid_at`. It only ever matches a LIVE edge (via
    `_find_live_fact`, so a missing/closed `fact_uid` raises rather than
    silently doing nothing) because every §5.2 caller of `duplicate` already
    found `old` via a `r.invalid_at IS NULL` candidate query — confirming a
    closed fact is not a case that arises for this primitive; a caller that
    needs that should use `upsert_fact_edges(revive=False)` directly.

    `last_confirmed_at` bumps to `at` (falling back to `now()`) only when
    this is a genuinely new supporting record or changed evidence/confidence
    — the same content-change rule as `upsert_fact_edges` (§5.6) — so a
    `duplicate` confirmation that adds nothing new does not move the clock.
    """
    fact = _find_live_fact(graph, fact_uid)
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}})-[r:{_label(fact['rel_type'])}]->(b {{uid: $to_uid}})
        WHERE r.fact_uid = $fact_uid
        SET r.last_confirmed_at = CASE WHEN (
                ($evidence IS NOT NULL AND $evidence <> r.evidence)
                OR ($confidence IS NOT NULL AND $confidence <> r.confidence)
                OR ($key IS NOT NULL AND NOT $key IN coalesce(r.source_record_keys, []))
            ) THEN coalesce($at, $now) ELSE r.last_confirmed_at END,
            r.source_record_keys = CASE
                WHEN $key IS NULL THEN r.source_record_keys
                WHEN $key IN coalesce(r.source_record_keys, []) THEN r.source_record_keys
                ELSE coalesce(r.source_record_keys, []) + [$key]
            END,
            r.evidence = CASE WHEN $evidence IS NULL THEN r.evidence ELSE $evidence END,
            r.confidence = CASE WHEN $confidence IS NULL THEN r.confidence ELSE $confidence END
        """,
        params={
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"], "fact_uid": fact_uid,
            "at": at, "now": now_iso(), "key": source_record_key,
            "evidence": evidence, "confidence": confidence,
        },
    )


def touch_metadata(graph: Graph, fact_uid: str, at: str | None = None) -> None:
    """§5.6 — bump `metadata_revised_at`, the third of the four clocks:
    status/pin/dispute/review decisions, never content. Kept a distinct
    primitive from `last_confirmed_at` (content restated, `upsert_fact_edges`
    / `confirm_fact`) and `first_seen_at` (`ON CREATE` only) so the three
    clocks can never be conflated by a caller reusing the wrong setter.

    No real caller exists yet — the status/pin/dispute/review decisions that
    should call this belong to Phase 6's review-approval flow, out of scope
    here. This function exists so that field/primitive is ready when that
    flow is built, per the writer-contract task ("just make sure the
    field/helper EXISTS and is correctly scoped").

    Matches by `fact_uid` alone (any relationship type/endpoints, live or
    closed — metadata decisions like "pin" or "dispute" are not restricted
    to live facts) and is a no-op if `fact_uid` matches nothing.
    """
    graph.query(
        "MATCH ()-[r]->() WHERE r.fact_uid = $fact_uid SET r.metadata_revised_at = $at",
        params={"fact_uid": fact_uid, "at": at or now_iso()},
    )


def reinforce_count(source_record_keys: list[str] | None) -> int:
    """§5.7 — `reinforce_count = size(r.source_record_keys)` (distinct
    supporting records). A pure computed value, not a stored field:
    `source_record_keys` already exists on every fact edge and already
    dedupes on write (`upsert_fact_edges`'s `ON MATCH` only appends a key
    not already present). Consumers (a rerank tie-break boost, an evidence
    line "seen in N records") live in `graph/chat.py`/`graph/rerank.py`,
    both off-limits here; this is only the pure function they should call.
    """
    return len(source_record_keys or [])


# `last_retrieved_at` (§5.6, fourth clock) is a NODE property, not an edge
# property: it is set "when a node reaches the final answer set", which is a
# `graph/chat.py` read-path concern (off-limits to this task). There is no
# writer here for it — a future `graph/chat.py` change should, after
# assembling the final answer set, do the node-scoped equivalent of:
#     MATCH (n {uid: $uid}) SET n.last_retrieved_at = $now
# for each node that made it into the answer, using this module's
# `now_iso()`. Documented here, per the writer-contract task, as the
# schema/property convention that change should follow — not implemented,
# since its only real caller is in the off-limits file.


# ---------------------------------------------------------------------------
# 25-plan.md Phase 5 §5.2 — two small additive primitives `resolve_text_fact`
# (`graph/resolve_text_fact.py`, built alongside this) needs and that no
# existing §5.0 primitive covers. Both match by `fact_uid` alone, the same
# label-agnostic idiom `touch_metadata`/`invalidate_edges_by_uid_pairs` use,
# so neither caller needs to know the edge's relationship type or endpoint
# labels up front.
# ---------------------------------------------------------------------------


def mark_ended_unknown(
    graph: Graph, fact_uid: str, *, attested_from: str, attested_by_record: str | None = None
) -> None:
    """§5.2 `mark(old, ended_unknown=True, attested_from=..., attested_by_record=...)`
    — the live fact's end date can't be resolved (missing/out-of-order dates
    on one or both sides), so it is neither closed (there is no instant to
    set `invalid_at` to) nor silently left to read as open-ended forever.

    Deliberately NOT `close_fact`: no `invalid_at` is set and no `FactHistory`
    row is archived — nothing has actually ended as far as storage is
    concerned. `graph/time_axis.py::holds_at`'s existing `ended_unknown`
    handling already treats a fact like this as ended at its own
    `attested_from` moment for world-time reads, with no new interval
    machinery required here.

    `attested_from` must be an ISO timestamp, never a source record key —
    same rule `upsert_fact_edges`'s docstring documents for its own
    `attested_from` field (`graph/time_axis.py` parses it as a date). A
    record key documenting *what attested this* belongs in the separate
    `attested_by_record` field.

    Matches only a LIVE edge (via `_find_live_fact`, so a missing/closed/
    ambiguous `fact_uid` raises rather than silently no-op'ing) — every
    `resolve_text_fact` caller of this already found `old` via a
    `r.invalid_at IS NULL` candidate query.
    """
    fact = _find_live_fact(graph, fact_uid)
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}})-[r:{_label(fact['rel_type'])}]->(b {{uid: $to_uid}})
        WHERE r.fact_uid = $fact_uid
        SET r.ended_unknown = true, r.attested_from = $attested_from,
            r.attested_by_record = $attested_by_record
        """,
        params={
            "from_uid": fact["from_uid"], "to_uid": fact["to_uid"], "fact_uid": fact_uid,
            "attested_from": attested_from, "attested_by_record": attested_by_record,
        },
    )


def set_projection_status(graph: Graph, fact_uid: str, projection_status: str) -> None:
    """§5.2 compensating-state primitive: set one fact edge's
    `projection_status` by `fact_uid` alone (live or not — a `pending_review`
    edge is, by definition, not matched by `_find_live_fact`'s `invalid_at IS
    NULL` requirement, so this does not go through it).

    `resolve_text_fact`'s `auto`-mode path uses this to promote an incoming
    fact from `pending_review` to `live` only *after* the corresponding old
    fact has already been closed/corrected/disputed — see
    `graph/resolve_text_fact.py` for why that ordering, not a single atomic
    write, is the safe one under a partial failure (this codebase's FalkorDB
    client has no multi-statement transaction primitive to reach for
    instead — see that module's docstring and this task's final report).

    A no-op if `fact_uid` matches nothing.
    """
    graph.query(
        "MATCH ()-[r]->() WHERE r.fact_uid = $fact_uid SET r.projection_status = $status",
        params={"fact_uid": fact_uid, "status": projection_status},
    )
