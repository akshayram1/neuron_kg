"""25-plan.md §9.5 migration: move pre-Phase-4 System/Term/Decision nodes onto
the namespace-scoped identity scheme Phase 4 (§4.0) introduced, and backfill
`valid_at_basis`/`assertion_status` on fact edges written before Phase 5
existed.

    uv run python -m scripts.migrate_scoped_identity                # dry run, default graph
    uv run python -m scripts.migrate_scoped_identity --graph demo   # dry run, named graph
    uv run python -m scripts.migrate_scoped_identity --out report.json
    uv run python -m scripts.migrate_scoped_identity --apply        # actually mutate the graph/ledger

Dry-run (no `--apply`) is the default and does **zero** graph/ledger writes --
safe to run against any graph, including one you don't intend to migrate yet,
just to see the shape of the report.

--------------------------------------------------------------------------
WHAT THIS MIGRATES

1. System/Term nodes minted before Phase 4 (§4.0) landed, still on the old
   global `semantic_uid(label, name)` identity (uid = `make_uid(label,
   name_norm)`, two identity parts, no namespace). §4.0's scheme is
   `make_uid(label, namespace_uid, name_norm)` (three parts) and stores
   `namespace_uid` explicitly on the node -- see `graph.semantic_pass`'s
   `_resolve_semantic_entity`/`_write_extraction`, read (never edited) for
   this script.

   DETECTION: `namespace_uid IS NULL`, exactly as this task's brief
   specifies -- verified against `_write_extraction`, not assumed: every
   write path in the current code (rung 2 scoped-exact reuse, rung 3 alias
   reuse, rung 4 vector reuse, a fresh mint) ends in the same `upsert_entities`
   call, which stamps `namespace_uid` onto the row unconditionally for every
   semantic label, System/Term included. A node lacking the property has
   therefore never been touched by any post-Phase-4 write -- a true
   pre-migration node.

   CAVEAT found while verifying this (not asked for, flagged under QUERIES
   below): the same unconditional stamp means a genuinely pre-Phase-4 node
   COULD acquire a `namespace_uid` property without its `uid` ever being
   migrated, if a later run's rung-4 vector match happened to reuse its old
   uid (`_remember(candidate_uid)` remembers whatever uid rung 4 returns,
   then the row-building loop stamps `namespace_uid` onto that same uid
   regardless of resolved_by). `find_system_term_candidates` below reports
   this separately (`stamped_but_unscoped`, read-only, never migrated by
   this script) so it isn't silently missed, but the primary candidate list
   still uses bare `namespace_uid IS NULL` per the brief.

2. Decision nodes minted before Phase 4 under the OLD global name-keyed
   identity (`uid == make_uid("Decision", name.strip().lower())` --
   `graph.semantic_pass.semantic_uid`). Phase 4 rescoped Decision identity to
   `make_uid("Decision", source_record_key, statement_norm)` -- a Decision is
   an assertion from ONE record, not a name-keyed global entity (§4.0).
   `namespace_uid` presence is NOT used to detect these (see the same
   caveat as above -- it would double the false-negative risk for exactly
   the node type the plan cares most about getting right); instead this
   recomputes the OLD identity formula directly, which is robust to whether
   the node was ever incidentally re-touched.

   A legacy Decision node mentioned by more than one distinct (non-deleted)
   SourceRecord is a "cross-record merge candidate" -- under the new
   identity it could never have been one node in the first place, and per
   this task's brief it ALWAYS goes to review, never auto-applied,
   regardless of anything else. A legacy Decision node with exactly one
   supporting record is unambiguous and migrates like a System/Term node.
   A legacy Decision with NO supporting records is treated the same
   conservative way as "ambiguous" (nothing to derive a scope from).

--------------------------------------------------------------------------
NAMESPACE DERIVATION (System/Term only)

For each candidate node, gather every (non-deleted) SourceRecord it is
`MENTIONED_IN` (`graph.duplicate_collector._node_source_record_keys` --
already-merged, read-only reuse, not reinvented). For each such record, look
up its ledger entry for `primary_node_uid` (durably stored per record_key by
`connectors.core.ledger.ConnectorLedger.commit`, so this works without
needing to be inside a live sync run), derive `record_own_kind` from the
record_key (`graph.semantic_pass._record_own_kind`, a pure parse), and run
that pair through `graph.semantic_pass._derive_namespace_uid` -- the EXACT
function `_write_extraction` calls for a live write. A missing ledger entry
or `primary_node_uid` degrades gracefully: `_derive_namespace_uid`'s
structural branch simply finds nothing and falls back to its own
connection-level scope (`provider:connection_id`, parsed straight from the
record_key) -- never raises, never needs the ledger to be populated to run
a dry-run against an arbitrary graph (this script's exit criterion says the
dry run must work "independent of the nilus/less_token data-restoration
blocker").

If every record a node is mentioned in agrees on one namespace, the mapping
is UNAMBIGUOUS. If they disagree, or the node has no (non-deleted) mention
at all, the mapping is AMBIGUOUS and is never auto-applied -- it goes to the
Phase 3 review queue instead (`connectors.core.ledger.create_review`,
type=`scoped_identity_migration`), per §9.5's "Put ambiguous mappings ...
into the Phase 3 review queue."

--------------------------------------------------------------------------
APPLY MODE

For each UNAMBIGUOUS mapping (System/Term with one namespace, or Decision
with exactly one source record):

  - if the computed new uid happens to equal the old uid already (namespace
    resolves to the same string the node's global name-only uid already
    was -- vanishingly rare, but free to handle safely), only the
    `namespace_uid` property is backfilled onto the SAME node -- no new
    node, no alias, no trace. Nothing was actually renamed.
  - otherwise: if a node with the new uid already exists (a fresh Phase-4
    extraction independently created the "real" scoped node before this
    migration ran against the same data -- a realistic scenario, not just a
    hypothetical), that node is the survivor AS-IS -- its properties are
    never overwritten by the old node's (the same non-destructive posture
    `graph.duplicate_collector.apply_approved_duplicate_merge` already takes
    for its survivor). Otherwise a brand new node is created at the new uid,
    copying every property off the old node (minus `uid` itself).
  - either way, `graph.duplicate_collector._absorb_fact_edges` and
    `_absorb_mentions` (imported directly, not reimplemented -- FalkorDB
    genuinely cannot retarget a relationship's endpoint in place; both
    already implement redirect-by-recreate-and-invalidate and are already
    tested) move every live fact edge and MENTIONED_IN provenance edge from
    the old node onto the new one.
  - `ledger.record_merge_trace(new_uid, old_uid, label, reason=
    "scoped_identity_migration")` -- see QUERIES for why this table
    (built for Phase 6.4 pairwise duplicate merges) is reused here rather
    than a dedicated migration-trace table.
  - `ledger.add_entity_alias(label, namespace, alias_norm, new_uid,
    source="migration_9.5")` so old references (by normalized name) keep
    resolving through the alias table's rung 3 of the resolution ladder.
  - the OLD node is never deleted. Per §9.5: "Preserve old UIDs as
    aliases/redirects until graph edges, ledger references and Qdrant
    points have been rebuilt and verified." After this script, the old node
    has no live outgoing/incoming fact edges (all redirected) but still
    exists, still resolvable by uid, and its own MENTIONED_IN edges are
    left untouched (an audit trail, matching `_absorb_mentions`'s own
    documented non-destructive behavior). Deleting old nodes and rebuilding
    Qdrant are explicit, separate, later steps -- out of scope here.

For every AMBIGUOUS System/Term mapping and every cross-record (or
record-less) Decision candidate: no graph write at all -- a `pending`
review is created instead (or silently skipped if an identical proposal was
already rejected -- `create_review`'s own dedup, reused as-is).

--------------------------------------------------------------------------
TEMPORAL BACKFILL (independent of the identity migration above -- run with
`--skip-identity` to do only this, or `--skip-temporal` for the reverse)

Every live-or-historical fact edge (any relationship type except
`MENTIONED_IN`) missing `valid_at_basis` and/or `assertion_status` gets:

  - `assertion_status = "live"` if missing. §9.5's own wording says
    `"active"` -- checked directly against `graph.writer.upsert_fact_edges`,
    which unconditionally sets `r.assertion_status = 'live'` on every new
    edge it has EVER created (`ON CREATE SET ... r.assertion_status =
    'live'`, ID '<label removed>'>=), and against `graph.fact_predicates`,
    whose `LIVE_FACT_CYPHER`/`is_live_fact` coalesce a missing value to
    `'live'`, not `'active'`. `"active"` is never written or read as a
    value anywhere else in this codebase. This is the same "plan text
    predates the real Phase 5 implementation's settled vocabulary" pattern,
    not a second real status -- `'live'` is used, `'active'` is flagged
    under QUERIES, per this task's explicit instruction for exactly this
    situation.
  - `valid_at_basis`: §9.5 asks for `"api"` on deterministic edges and
    `"record_time"` on existing LLM edges. Checked directly: NOTHING in
    this codebase ever writes or reads `"api"` as a `valid_at_basis` value
    -- `graph.semantic_pass` only ever writes `"stated"` (a real date
    parsed from evidence) or `"record_time"` (fallback), and
    `graph.writer.upsert_fact_edges` itself now defaults a missing
    `valid_at_basis` to `"record_time"` on ANY edge, deterministic or not
    (`coalesce(row.valid_at_basis, 'record_time')`). Introducing `"api"`
    here would be a second, inconsistent value nothing else ever produces
    or checks for -- exactly what this task's brief says to avoid.
    Backfilled value is `"record_time"` for every edge missing it,
    regardless of `extraction_method` (matching the writer's own current
    default), flagged under QUERIES. The by-`extraction_method` breakdown
    is still reported (not just a single count) so a reviewer can see the
    deterministic/LLM split even though both land on the same value.

Matched and updated the same way `graph.writer.remove_record_support`/
`invalidate_edges_by_uid_pairs` already do it -- grouped by relationship
type, matched by endpoint `uid` pairs, never by FalkorDB's internal `id()`
(that's only ever used for full-graph scans elsewhere in this codebase,
e.g. `scripts/backup_falkordb_rdf.py`, never as a write-match key).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from falkordb import Graph

from util import paths as _paths  # noqa: F401 -- loads .env from repo root
from util.logging import configure_logging
from util.paths import DATA_DIR

from connectors.core.ledger import ConnectorLedger
from graph.storage import multigraph
from graph.storage import vector_store
from graph.resolution.duplicate_collector import (
    _absorb_fact_edges,
    _absorb_mentions,
    _node_source_record_keys,
)
from graph.storage.falkor_client import get_graph
from graph.ingestion.semantic_pass import (
    _derive_namespace_uid,
    _normalize_identity,
    _record_own_kind,
    semantic_uid,
)
from graph.storage.writer import _label, make_uid, upsert_entities

logger = logging.getLogger("neuron.migrate_scoped_identity")

REVIEW_TYPE = "scoped_identity_migration"
ALIAS_SOURCE = "migration_9.5"

# §9.5 flags this as a wording mismatch against the real, settled Phase 5
# convention (see module docstring + QUERIES). Kept as a named constant so
# the one place this decision is made is easy to find and revisit.
BACKFILL_ASSERTION_STATUS = "live"
BACKFILL_VALID_AT_BASIS = "record_time"


# --------------------------------------------------------------------------- resolve target


def resolve_target(graph_name: str) -> multigraph.GraphTarget:
    """Same resolution the API routes / other scripts use, so this migration
    never disagrees with the product about which physical graph a name
    means (`scripts/evaluate_retrieval.py::resolve_target` is the precedent
    this mirrors)."""
    return multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store.COLLECTION,
    )


# --------------------------------------------------------------------------- candidate data shapes


@dataclass
class SystemTermMapping:
    label: str
    old_uid: str
    name: str
    normalized_name: str
    record_keys: list[str]
    namespaces: list[str]
    ambiguous: bool
    ambiguous_reason: str | None
    new_uid: str | None


@dataclass
class DecisionMapping:
    old_uid: str
    name: str
    statement: str | None
    record_keys: list[str]
    ambiguous: bool
    ambiguous_reason: str | None
    new_uid: str | None


@dataclass
class StampedButUnscopedNode:
    """Read-only diagnostic (see module docstring's CAVEAT): a node that has
    a `namespace_uid` property but whose `uid` does not match what §4.0's
    scheme would compute from it -- i.e. it was re-touched by a post-Phase-4
    write without ever being migrated. Never auto-migrated by this script;
    surfaced so it isn't silently missed."""

    label: str
    uid: str
    name: str
    namespace_uid: str
    expected_new_uid: str


# --------------------------------------------------------------------------- namespace derivation


def _namespace_for_record(graph: Graph, ledger: ConnectorLedger, record_key: str) -> str:
    """One record_key -> the namespace `_derive_namespace_uid` would compute
    for it today -- the exact function a live write uses, reused verbatim.
    A record with no ledger entry (or no `primary_node_uid`) degrades to
    `_derive_namespace_uid`'s own connection-level fallback rather than
    raising -- see module docstring."""
    entry = ledger.get(record_key)
    primary_uid = (entry.primary_node_uid if entry else None) or ""
    own_kind = _record_own_kind(record_key)
    return _derive_namespace_uid(graph, record_key, own_kind, primary_uid)


def _namespaces_for_node(
    graph: Graph, ledger: ConnectorLedger, uid: str, cache: dict[str, str],
) -> tuple[list[str], list[str]]:
    """(record_keys, distinct namespaces) for everything `uid` is
    MENTIONED_IN. `cache` is keyed by record_key and shared across every
    candidate node in one run -- many System/Term mentions in the same
    project share the same handful of source records, so this avoids
    re-deriving the same namespace hundreds of times."""
    record_keys = _node_source_record_keys(graph, uid)
    namespaces: list[str] = []
    seen: set[str] = set()
    for record_key in record_keys:
        if record_key not in cache:
            cache[record_key] = _namespace_for_record(graph, ledger, record_key)
        namespace = cache[record_key]
        if namespace not in seen:
            seen.add(namespace)
            namespaces.append(namespace)
    return record_keys, namespaces


# --------------------------------------------------------------------------- System/Term candidates


def find_system_term_candidates(
    graph: Graph, ledger: ConnectorLedger,
) -> tuple[list[SystemTermMapping], list[StampedButUnscopedNode]]:
    mappings: list[SystemTermMapping] = []
    stamped_but_unscoped: list[StampedButUnscopedNode] = []
    namespace_cache: dict[str, str] = {}

    for label in ("System", "Term"):
        rows = graph.query(
            f"MATCH (n:{label}) WHERE n.namespace_uid IS NULL RETURN n.uid, n.name"
        ).result_set
        for uid, name in rows:
            name = name or ""
            normalized_name = _normalize_identity(name)
            record_keys, namespaces = _namespaces_for_node(graph, ledger, uid, namespace_cache)
            if len(namespaces) == 1:
                namespace = namespaces[0]
                new_uid = make_uid(label, namespace, normalized_name)
                mappings.append(SystemTermMapping(
                    label=label, old_uid=uid, name=name, normalized_name=normalized_name,
                    record_keys=record_keys, namespaces=namespaces,
                    ambiguous=False, ambiguous_reason=None, new_uid=new_uid,
                ))
            else:
                reason = "no_source_records" if not namespaces else "conflicting_namespaces"
                mappings.append(SystemTermMapping(
                    label=label, old_uid=uid, name=name, normalized_name=normalized_name,
                    record_keys=record_keys, namespaces=namespaces,
                    ambiguous=True, ambiguous_reason=reason, new_uid=None,
                ))

        # Diagnostic-only sweep for the CAVEAT in the module docstring:
        # nodes that DO have namespace_uid but whose uid was never actually
        # rescoped. Never migrated here -- read-only.
        stamped_rows = graph.query(
            f"MATCH (n:{label}) WHERE n.namespace_uid IS NOT NULL "
            "RETURN n.uid, n.name, n.namespace_uid"
        ).result_set
        for uid, name, namespace_uid in stamped_rows:
            expected = make_uid(label, namespace_uid, _normalize_identity(name or ""))
            if expected != uid:
                stamped_but_unscoped.append(StampedButUnscopedNode(
                    label=label, uid=uid, name=name or "", namespace_uid=namespace_uid,
                    expected_new_uid=expected,
                ))

    return mappings, stamped_but_unscoped


# --------------------------------------------------------------------------- Decision candidates


def find_decision_candidates(graph: Graph) -> list[DecisionMapping]:
    """Legacy Decision nodes: `uid` still matches the OLD global name-keyed
    formula (`graph.semantic_pass.semantic_uid`), from before §4.0 rescoped
    Decision identity to record+statement. Detected by recomputing that old
    formula directly (see module docstring for why this, not `namespace_uid`
    absence, is used for Decision)."""
    mappings: list[DecisionMapping] = []
    rows = graph.query("MATCH (n:Decision) RETURN n.uid, n.name, n.statement").result_set
    for uid, name, statement in rows:
        name = name or ""
        if uid != semantic_uid("Decision", name):
            continue  # already on the new (record, statement) identity scheme
        record_keys = _node_source_record_keys(graph, uid)
        distinct_records = sorted(set(record_keys))
        if len(distinct_records) == 1:
            record_key = distinct_records[0]
            statement_norm = _normalize_identity(statement or name)
            new_uid = make_uid("Decision", record_key, statement_norm)
            mappings.append(DecisionMapping(
                old_uid=uid, name=name, statement=statement, record_keys=record_keys,
                ambiguous=False, ambiguous_reason=None, new_uid=new_uid,
            ))
        else:
            reason = "no_source_records" if not distinct_records else "cross_record_decision_merge"
            mappings.append(DecisionMapping(
                old_uid=uid, name=name, statement=statement, record_keys=record_keys,
                ambiguous=True, ambiguous_reason=reason, new_uid=None,
            ))
    return mappings


# --------------------------------------------------------------------------- apply: node migration


def _node_properties(graph: Graph, uid: str) -> dict[str, Any]:
    rows = graph.query(
        "MATCH (n {uid: $uid}) RETURN properties(n)", params={"uid": uid},
    ).result_set
    return dict(rows[0][0]) if rows else {}


def _migrate_node(
    graph: Graph, ledger: ConnectorLedger, *,
    label: str, old_uid: str, new_uid: str,
    alias_namespace: str | None, alias_norm: str,
) -> dict[str, Any]:
    """Shared apply-mode mechanism for both System/Term and single-record
    Decision mappings -- create-or-reuse the new node, redirect the old
    node's live edges/mentions onto it (via the already-merged, already-
    tested `graph.duplicate_collector` primitives), record a trace row, and
    add the alias. Never deletes or overwrites the old node."""
    if new_uid == old_uid:
        # Namespace happened to resolve to a string that reproduces the old
        # global uid exactly -- nothing to rename, just backfill the
        # property this node was missing.
        upsert_entities(graph, label, [{"uid": old_uid, "props": {"namespace_uid": alias_namespace or ""}}])
        return {
            "label": label, "old_uid": old_uid, "new_uid": new_uid,
            "status": "namespace_uid_backfilled_only", "created_new_node": False,
        }

    existing = graph.query(
        "MATCH (n {uid: $uid}) RETURN n.uid LIMIT 1", params={"uid": new_uid},
    ).result_set
    created_new_node = not existing
    if created_new_node:
        old_props = _node_properties(graph, old_uid)
        old_props.pop("uid", None)
        if label in {"System", "Term"}:
            # §4.0: namespace_uid is part of System/Term identity and must be
            # stored explicitly -- the old node never had it (that absence is
            # exactly this script's detection signal), so it has to be set
            # here rather than merely copied from `old_props`.
            old_props["namespace_uid"] = alias_namespace or ""
        upsert_entities(graph, label, [{"uid": new_uid, "props": old_props}])

    _absorb_fact_edges(graph, label, new_uid, old_uid)
    _absorb_mentions(graph, label, new_uid, old_uid)
    trace_id = ledger.record_merge_trace(new_uid, old_uid, label, reason="scoped_identity_migration")
    ledger.add_entity_alias(label, alias_namespace, alias_norm, new_uid, source=ALIAS_SOURCE)

    return {
        "label": label, "old_uid": old_uid, "new_uid": new_uid,
        "status": "migrated", "created_new_node": created_new_node, "trace_id": trace_id,
    }


def _create_review(ledger: ConnectorLedger, payload: dict[str, Any]) -> int | None:
    identity = f"{REVIEW_TYPE}:{payload['label']}:{payload['old_uid']}"
    return ledger.create_review(REVIEW_TYPE, payload, identity=identity)


# --------------------------------------------------------------------------- temporal backfill


def backfill_temporal_fields(graph: Graph, *, apply: bool) -> dict[str, Any]:
    """§9.5's `valid_at_basis`/`assertion_status` backfill for every fact
    edge (any relation except MENTIONED_IN) missing either. See module
    docstring for the `"api"`/`"active"` wording-vs-real-convention QUERY.
    """
    rows = graph.query(
        "MATCH (a)-[r]->(b) WHERE type(r) <> 'MENTIONED_IN' "
        "AND (r.valid_at_basis IS NULL OR r.assertion_status IS NULL) "
        "RETURN a.uid, type(r), b.uid, r.extraction_method, r.valid_at_basis, r.assertion_status"
    ).result_set

    by_type: dict[str, list[dict[str, Any]]] = {}
    by_extraction_method: dict[str, int] = {}
    for a_uid, rel_type, b_uid, method, vab, astat in rows:
        by_extraction_method[method or "unknown"] = by_extraction_method.get(method or "unknown", 0) + 1
        by_type.setdefault(rel_type, []).append({
            "from_uid": a_uid, "to_uid": b_uid,
            "valid_at_basis": vab or BACKFILL_VALID_AT_BASIS,
            "assertion_status": astat or BACKFILL_ASSERTION_STATUS,
        })

    if apply:
        for rel_type, typed_rows in by_type.items():
            graph.query(
                f"""
                UNWIND $rows AS row
                MATCH (a {{uid: row.from_uid}})-[r:{_label(rel_type)}]->(b {{uid: row.to_uid}})
                SET r.valid_at_basis = coalesce(r.valid_at_basis, row.valid_at_basis),
                    r.assertion_status = coalesce(r.assertion_status, row.assertion_status)
                """,
                params={"rows": typed_rows},
            )

    return {
        "edges_missing_fields": len(rows),
        "by_extraction_method": by_extraction_method,
        "backfilled_valid_at_basis": BACKFILL_VALID_AT_BASIS,
        "backfilled_assertion_status": BACKFILL_ASSERTION_STATUS,
        "applied": bool(apply and rows),
    }


# --------------------------------------------------------------------------- orchestration


def run_migration(
    graph: Graph, ledger: ConnectorLedger, *,
    apply: bool, skip_identity: bool = False, skip_temporal: bool = False,
    graph_name: str = multigraph.DEFAULT_GRAPH_NAME,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "graph": graph_name,
        "mode": "apply" if apply else "dry-run",
    }

    if not skip_identity:
        system_term, stamped_but_unscoped = find_system_term_candidates(graph, ledger)
        decisions = find_decision_candidates(graph)

        system_term_results = []
        for mapping in system_term:
            if mapping.ambiguous:
                review_id = _create_review(ledger, {
                    "label": mapping.label, "old_uid": mapping.old_uid, "name": mapping.name,
                    "normalized_name": mapping.normalized_name, "namespaces": mapping.namespaces,
                    "record_keys": mapping.record_keys, "reason": mapping.ambiguous_reason,
                }) if apply else None
                system_term_results.append({**asdict(mapping), "review_id": review_id})
                continue
            if apply:
                result = _migrate_node(
                    graph, ledger, label=mapping.label, old_uid=mapping.old_uid,
                    new_uid=mapping.new_uid, alias_namespace=mapping.namespaces[0],
                    alias_norm=mapping.normalized_name,
                )
                system_term_results.append({**asdict(mapping), **result})
            else:
                system_term_results.append(asdict(mapping))

        decision_results = []
        for mapping in decisions:
            if mapping.ambiguous:
                review_id = _create_review(ledger, {
                    "label": "Decision", "old_uid": mapping.old_uid, "name": mapping.name,
                    "statement": mapping.statement, "record_keys": mapping.record_keys,
                    "reason": mapping.ambiguous_reason,
                }) if apply else None
                decision_results.append({**asdict(mapping), "review_id": review_id})
                continue
            if apply:
                result = _migrate_node(
                    graph, ledger, label="Decision", old_uid=mapping.old_uid,
                    new_uid=mapping.new_uid, alias_namespace=None,
                    alias_norm=_normalize_identity(mapping.name),
                )
                decision_results.append({**asdict(mapping), **result})
            else:
                decision_results.append(asdict(mapping))

        report["system_term"] = system_term_results
        report["stamped_but_unscoped"] = [asdict(item) for item in stamped_but_unscoped]
        report["decisions"] = decision_results
        report["summary"] = {
            "system_term_total": len(system_term),
            "system_term_unambiguous": sum(1 for m in system_term if not m.ambiguous),
            "system_term_ambiguous": sum(1 for m in system_term if m.ambiguous),
            "decision_total": len(decisions),
            "decision_unambiguous": sum(1 for m in decisions if not m.ambiguous),
            "decision_ambiguous_or_cross_record": sum(1 for m in decisions if m.ambiguous),
            "stamped_but_unscoped": len(stamped_but_unscoped),
        }

    if not skip_temporal:
        report["temporal_backfill"] = backfill_temporal_fields(graph, apply=apply)

    return report


# --------------------------------------------------------------------------- CLI


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--graph", default=multigraph.DEFAULT_GRAPH_NAME,
        help="named graph target (see graph.multigraph); defaults to the unified default graph",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually mutate the graph/ledger; default is dry-run (report only, zero writes)",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="also write the JSON report to this path (stdout always gets it too)",
    )
    parser.add_argument(
        "--skip-identity", action="store_true",
        help="skip the System/Term/Decision scoped-identity migration; temporal backfill only",
    )
    parser.add_argument(
        "--skip-temporal", action="store_true",
        help="skip the valid_at_basis/assertion_status backfill; identity migration only",
    )
    args = parser.parse_args()

    target = resolve_target(args.graph)
    graph = get_graph(name=target.falkor_name)
    ledger = ConnectorLedger(target.ledger_path)

    report = run_migration(
        graph, ledger, apply=args.apply,
        skip_identity=args.skip_identity, skip_temporal=args.skip_temporal,
        graph_name=args.graph,
    )
    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.out:
        args.out.write_text(text)
        logger.info("report written to %s", args.out)
    logger.info(
        "migrate_scoped_identity: graph=%s mode=%s%s",
        args.graph, "apply" if args.apply else "dry-run",
        "" if not args.out else f" (report -> {args.out})",
    )


if __name__ == "__main__":
    main()
