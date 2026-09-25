"""One-hop graph expansion (plan.md §1.3) and read-time authority tiers
(plan.md §1.4).

`expand_neighbors` pulls typed neighbours of a candidate pool through the
graph so retrieval isn't limited to what fulltext/vector search directly
matched. `tier_for_extraction_method` is a pure lookup used to floor which
of those neighbours (and, once wired into `graph/chat.py`, which evidence
generally) are trustworthy enough to surface for a given question.

Neither function writes or mutates graph state — both are read-time only.
"""

from __future__ import annotations

import logging

from falkordb import Graph

from graph.access import AccessScope
from graph.search import SearchHit

logger = logging.getLogger("neuron.expand")

# Structural relations worth walking outward from a seed node. Deliberately
# excludes CONTAINS/BELONGS_TO/AUTHORED_BY/ASSIGNED_TO -- plan.md §1.3: those
# lead straight into hub nodes (a Repository, a Person...) that would flood
# the candidate pool with everything else attached to the same hub.
# Assignee information still reaches the answer through `_entity_evidence`
# facts in graph/chat.py, not through expansion.
EXPAND_RELS = ["IMPLEMENTS", "DOCUMENTS", "PARENT_OF", "REFERENCES",
               "MODIFIES", "APPLIES_TO", "DEFINES"]
# Labels never returned as expansion *targets* -- walking one more hop from
# one of these would pull in everything else mentioning the same hub, not
# things actually related to the seed.
HUB_LABELS = {"Repository", "Project", "Workspace", "Person", "SourceRecord"}
MAX_SEEDS, PER_SEED = 8, 4

# Safety valve only, not a correctness knob: keeps a pathological fan-out
# from returning an unbounded result set before Python does the real
# per-seed capping and clamping below. Not tied to MAX_SEEDS x PER_SEED --
# it just has to be generous enough that no seed's real candidates get cut
# off before ordering/capping ever sees them.
_QUERY_SAFETY_LIMIT = 5000

# Authority tiers (plan.md §1.4), ordered lowest to highest. Kept as an
# explicit ordered tuple (not a bare string comparison) so tier comparisons
# don't silently break if tier names are ever reordered.
TIER_ORDER = ("unknown", "derived", "secondary", "primary")
_TIER_RANK = {tier: rank for rank, tier in enumerate(TIER_ORDER)}

_PRIMARY_METHODS = {"deterministic", "exact_anchor", "changelog"}


def tier_for_extraction_method(extraction_method: str | None) -> str:
    """Pure lookup, plan.md §1.4 table. Computed at read time only -- no
    stored data changes, so trust policy can be retuned without a rewrite."""
    if extraction_method in _PRIMARY_METHODS:
        return "primary"
    if extraction_method == "llm":
        return "secondary"
    if extraction_method == "derived":
        return "derived"
    return "unknown"


def expand_neighbors(
    graph: Graph,
    seed_uids: list[str],
    scope: AccessScope,
    providers: list[str] | None,
    *,
    min_tier: str = "derived",
    exclude_edges: frozenset[tuple[str, str, str]] = frozenset(),
) -> list[SearchHit]:
    """One-hop typed expansion around `seed_uids` (plan.md §1.3).

    `seed_uids` is whatever pool the caller passes -- pool-truncation policy
    (which uids make it into that pool) is the caller's job (Phase 1.1/1.7
    wiring). This function only caps defensively at `MAX_SEEDS` and logs
    when that cap is hit (router honesty, per DICE).

    Returns new candidates only: uids already present in `seed_uids` (the
    full input, not just the capped seed set) are never returned, and the
    result is de-duplicated by uid across seeds/edges. Asserted edges are
    ordered before derived edges; each seed contributes at most `PER_SEED`
    candidates; the overall result never exceeds `MAX_SEEDS * PER_SEED`.
    """
    if min_tier not in _TIER_RANK:
        raise ValueError(f"unknown tier: {min_tier!r} (expected one of {TIER_ORDER})")
    min_rank = _TIER_RANK[min_tier]

    all_seed_uids = list(dict.fromkeys(seed_uids))  # de-dup, preserve order
    seeds = all_seed_uids[:MAX_SEEDS]
    if len(all_seed_uids) > MAX_SEEDS:
        logger.info(
            "expand_neighbors: seed pool capped %d -> %d (MAX_SEEDS)",
            len(all_seed_uids), MAX_SEEDS,
        )
    if not seeds:
        return []

    # ACL via MENTIONED_IN (graph/search.py's pattern): a neighbour is only
    # a candidate if it has at least one SourceRecord this scope can see.
    # This is the node-authorization check -- separate from, and in addition
    # to, whatever provenance the edge itself carries.
    acl, acl_params = scope.cypher("sr", "expand_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    query = f"""
        MATCH (seed) WHERE seed.uid IN $seed_uids
        MATCH (seed)-[r]-(neighbor)
        WHERE type(r) IN $rels
          AND r.invalid_at IS NULL
          AND NOT neighbor.uid IN $exclude_uids
          AND NOT (labels(neighbor)[0] IN $hub_labels)
        MATCH (neighbor)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT seed.uid, neighbor.uid, labels(neighbor)[0], neighbor.name,
               neighbor.search_text, type(r), coalesce(r.derived, false),
               r.extraction_method, startNode(r).uid, endNode(r).uid
        LIMIT {_QUERY_SAFETY_LIMIT}
    """
    try:
        rows = graph.query(
            query,
            params={
                "seed_uids": seeds,
                "exclude_uids": all_seed_uids,
                "rels": EXPAND_RELS,
                "hub_labels": list(HUB_LABELS),
                **acl_params,
                **({"providers": providers} if providers else {}),
            },
        ).result_set
    except Exception:
        logger.exception("expand_neighbors: query failed for %d seed(s)", len(seeds))
        return []

    # Group by seed (in seed order) so PER_SEED capping and the
    # asserted-before-derived ordering can both be applied per seed before
    # candidates from different seeds are interleaved.
    by_seed: dict[str, list[dict]] = {}
    seed_order: list[str] = []
    for row in rows:
        (seed_uid, n_uid, n_label, n_name, n_summary, rel_type, derived,
         extraction_method, from_uid, to_uid) = row

        if (from_uid, rel_type, to_uid) in exclude_edges:
            continue
        tier = tier_for_extraction_method(extraction_method)
        if _TIER_RANK[tier] < min_rank:
            continue

        if seed_uid not in by_seed:
            by_seed[seed_uid] = []
            seed_order.append(seed_uid)
        by_seed[seed_uid].append({
            "uid": n_uid, "label": n_label, "name": n_name or "",
            "summary": n_summary or "", "rel": rel_type, "derived": bool(derived),
        })

    per_seed_cap_hit = False
    ordered: list[tuple[bool, dict]] = []  # (derived, entry) — global order preserved below
    added_uids: set[str] = set()
    for seed_uid in seed_order:
        entries = by_seed[seed_uid]
        # Stable sort: asserted (derived=False) before derived, ties keep
        # the graph's own return order.
        entries.sort(key=lambda entry: entry["derived"])
        if len(entries) > PER_SEED:
            per_seed_cap_hit = True
        for entry in entries[:PER_SEED]:
            if entry["uid"] in added_uids:
                continue  # de-dup: same neighbour reached from >1 seed/edge
            added_uids.add(entry["uid"])
            ordered.append((entry["derived"], entry))

    if per_seed_cap_hit:
        logger.info("expand_neighbors: PER_SEED cap (%d) hit for at least one seed", PER_SEED)

    # Global asserted-before-derived ordering across all seeds, stable so
    # per-seed relative order survives within each group.
    ordered.sort(key=lambda pair: pair[0])

    max_total = MAX_SEEDS * PER_SEED
    if len(ordered) > max_total:
        logger.info(
            "expand_neighbors: overall clamp hit, %d candidates -> %d (MAX_SEEDS * PER_SEED)",
            len(ordered), max_total,
        )
    ordered = ordered[:max_total]

    return [
        SearchHit(
            uid=entry["uid"], label=entry["label"], name=entry["name"],
            summary=entry["summary"], score=0.0, methods=[f"graph:{entry['rel']}"],
        )
        for _derived, entry in ordered
    ]
