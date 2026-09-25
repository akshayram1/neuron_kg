"""Deterministic derived facts with a proof chain.

Asserted edges always win: a derivation never closes or replaces a live
human/API fact of the same triple. Derived rows are marked `derived=true`
and carry `premise_fact_uids` so the entity panel and chat can say
"inferred because …". Materialization is cheap Cypher, not an OWL reasoner.
"""

from __future__ import annotations

import logging

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger
from graph import writer as w

logger = logging.getLogger("neuron.derived")

# A wrong rule multiplies wrong edges. Keep the set small and named.
PARENT_IMPLEMENTS = "parent_implements"
PARENT_DOCUMENTS = "parent_documents"
SHARED_CONCEPT = "shared_concept"
PR_IMPLEMENTS = "pr_implements"
VERIFIED_EMAIL = "verified_email"


def materialize_around(
    graph: Graph, seed_uid: str, record_key: str, *, ledger: ConnectorLedger | None = None,
) -> int:
    """Recompute derived edges that touch `seed_uid` (or its parent/children).

    `ledger` is new, optional, and keyword-only -- threaded through to
    `_shared_concept_documents` only (25-plan.md §6.2 moved that one path
    from writing a direct edge to proposing a `link_candidates` row; see
    that function's docstring for the real behavior change). Existing
    callers (`graph/jira_pipeline.py`, `graph/resolver.py`) do not pass a
    ledger yet and are out of this task's scope to update -- so they keep
    calling this exactly as before, and `_shared_concept_documents` simply
    no-ops (logged, not a crash) until one of those call sites is updated
    to pass a real `ConnectorLedger`. See QUERIES.md.
    """
    written = 0
    written += _lift_through_parent(graph, seed_uid, record_key, "IMPLEMENTS", PARENT_IMPLEMENTS)
    written += _lift_through_parent(graph, seed_uid, record_key, "DOCUMENTS", PARENT_DOCUMENTS)
    written += _shared_concept_documents(graph, seed_uid, record_key, ledger=ledger)
    written += _document_via_pr(graph, seed_uid, record_key)
    return written


def _lift_through_parent(
    graph: Graph, seed_uid: str, record_key: str, relation: str, rule: str,
) -> int:
    """If impl -[:REL]-> child and child -[:PARENT_OF]-> parent, derive impl -[:REL]-> parent."""
    query = f"""
        MATCH (child {{uid: $child}})-[:PARENT_OF]->(parent {{uid: $parent}})
        MATCH (impl)-[r:{relation}]->(child)
        WHERE r.invalid_at IS NULL AND coalesce(r.derived, false) = false
        OPTIONAL MATCH (impl)-[existing:{relation}]->(parent)
        WHERE existing.invalid_at IS NULL AND coalesce(existing.derived, false) = false
        WITH impl, parent, child, r, existing
        WHERE existing IS NULL
        RETURN impl.uid, labels(impl)[0], parent.uid, labels(parent)[0],
               child.name, r.fact_uid, r.source_record_keys, r.valid_at
    """
    rows = []
    child_parent = graph.query(
        "MATCH (child {uid: $uid})-[:PARENT_OF]->(parent) RETURN child.uid, parent.uid",
        params={"uid": seed_uid},
    ).result_set
    parent_of = graph.query(
        "MATCH (child)-[:PARENT_OF]->(parent {uid: $uid}) RETURN child.uid, parent.uid",
        params={"uid": seed_uid},
    ).result_set
    pairs = {(row[0], row[1]) for row in child_parent + parent_of}
    for child_uid, parent_uid in pairs:
        rows.extend(graph.query(query, params={"child": child_uid, "parent": parent_uid}).result_set)
    return _write_derived(graph, relation, rule, record_key, rows, {
        "evidence": lambda row: f"Inferred: {row[4]} is a child of the parent and already {relation.lower()}s it.",
    })


def _shared_concept_documents(
    graph: Graph, seed_uid: str, record_key: str, *, ledger: ConnectorLedger | None = None,
) -> int:
    """Document and WorkItem that share a Term or System -> a
    `link_candidates` proposal, NOT a direct `DOCUMENTS` edge.

    REAL BEHAVIOR CHANGE (25-plan.md §6.2: "Also move derived.py ->
    _shared_concept_documents onto this path: produce candidates, not
    direct edges."). Before this change, a Document/WorkItem pair sharing a
    Term/System got an immediate, unreviewed `derived=true` `DOCUMENTS`
    edge via `_write_derived`. Now each such pair instead becomes a
    `pending` row in `link_candidates`
    (`ledger.create_link_candidate(..., derived_rule="shared_concept",
    confidence=0.5)` -- DICE-neutral, same convention every other §6.2
    candidate source uses) and needs an explicit approval
    (`graph.link_candidates.apply_approved_link_candidate`) before a real
    edge is written. `derived_rule="shared_concept"` reuses this module's
    existing `SHARED_CONCEPT` constant rather than the generic `"two_hop"`
    §6.2 otherwise uses for its own DICE bridge -- this join is narrower
    and more specific (a literal shared Term/System edge on both sides, not
    the general two-hop-with-degree-cap search
    `graph/link_candidates.py::find_two_hop_candidates` runs), so it keeps
    its own, more descriptive rule name.

    `ledger` is optional, keyword-only: when `None` (every existing caller
    today -- see `materialize_around`'s docstring), this is a no-op
    (logged), not a crash, since this function has no way to construct its
    own `ConnectorLedger` (every other call site in this codebase
    constructs one from a graph-specific `ledger_path` the caller owns, not
    a global default) and updating `graph/jira_pipeline.py`/
    `graph/resolver.py` to pass one is out of this task's scope (flagged in
    QUERIES.md).

    Returns the number of candidates created/found this call (idempotent:
    `create_link_candidate` resolves to the same row for the same
    (from_uid, to_uid, relation) triple across repeated runs) -- the same
    "count of things done" role this function's return value played before,
    now counting candidates instead of edges.
    """
    if ledger is None:
        logger.info(
            "shared_concept: materialize_around called without a ledger, "
            "skipping link_candidates proposal for seed=%s (see graph/derived.py docstring)",
            seed_uid,
        )
        return 0
    rows = graph.query(
        """
        MATCH (concept) WHERE concept:Term OR concept:System
        MATCH (doc:Document)-[r1]->(concept)
        WHERE r1.invalid_at IS NULL AND type(r1) <> 'MENTIONED_IN'
        MATCH (wi:WorkItem)-[r2]->(concept)
        WHERE r2.invalid_at IS NULL AND type(r2) <> 'MENTIONED_IN'
          AND (doc.uid = $uid OR wi.uid = $uid OR concept.uid = $uid)
        OPTIONAL MATCH (doc)-[existing:DOCUMENTS]->(wi)
        WHERE existing.invalid_at IS NULL AND coalesce(existing.derived, false) = false
        WITH doc, wi, concept, r1, r2, existing
        WHERE existing IS NULL
        RETURN DISTINCT doc.uid, wi.uid
        """,
        params={"uid": seed_uid},
    ).result_set
    created = 0
    for doc_uid, wi_uid in rows:
        ledger.create_link_candidate(
            doc_uid, wi_uid, "DOCUMENTS", derived_rule=SHARED_CONCEPT, confidence=0.5,
        )
        created += 1
    if created:
        logger.info("shared_concept candidates ×%d via %s", created, record_key)
    return created


def _document_via_pr(graph: Graph, seed_uid: str, record_key: str) -> int:
    """Document -DOCUMENTS-> PR -IMPLEMENTS-> WorkItem → Document DOCUMENTS WorkItem.

    The PR is the hub: Notion names the PR (URL or `PR #12`), the PR names
    the Jira key. Without this lift the three nodes are a path, not a
    Document↔ticket edge chat already knows how to cite.
    """
    rows = graph.query(
        """
        MATCH (doc:Document)-[r1:DOCUMENTS]->(pr:PullRequest)
        WHERE r1.invalid_at IS NULL AND coalesce(r1.derived, false) = false
        MATCH (pr)-[r2:IMPLEMENTS]->(wi:WorkItem)
        WHERE r2.invalid_at IS NULL AND coalesce(r2.derived, false) = false
          AND (doc.uid = $uid OR pr.uid = $uid OR wi.uid = $uid)
        OPTIONAL MATCH (doc)-[existing:DOCUMENTS]->(wi)
        WHERE existing.invalid_at IS NULL AND coalesce(existing.derived, false) = false
        WITH doc, pr, wi, r1, r2, existing
        WHERE existing IS NULL
        RETURN doc.uid, 'Document', wi.uid, 'WorkItem',
               pr.name, r1.fact_uid, r1.source_record_keys, r1.valid_at,
               r2.fact_uid
        """,
        params={"uid": seed_uid},
    ).result_set
    return _write_derived(graph, "DOCUMENTS", PR_IMPLEMENTS, record_key, rows, {
        "evidence": lambda row: (
            f"Inferred: {row[4]} implements the ticket and this page documents that PR."
        ),
        "premises": lambda row: [uid for uid in (row[5], row[8]) if uid],
    })


def _write_derived(
    graph: Graph,
    relation: str,
    rule: str,
    record_key: str,
    rows: list,
    render: dict,
) -> int:
    by_label: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        from_uid, from_label, to_uid, to_label = row[0], row[1], row[2], row[3]
        if not from_uid or not to_uid or from_uid == to_uid:
            continue
        evidence = render["evidence"](row)
        premises = render["premises"](row) if "premises" in render else [row[5]] if row[5] else []
        keys = [key for key in (row[6] or []) if key] or [record_key]
        by_label.setdefault((from_label, to_label), []).append({
            "from_uid": from_uid, "to_uid": to_uid,
            "source_record_keys": keys,
            "evidence": evidence,
            "extraction_method": "derived",
            "confidence": 0.6,
            "valid_at": row[7],
            "derived": True,
            "derived_rule": rule,
            "premise_fact_uids": [uid for uid in premises if uid],
            "extractor_version": "derived-v1",
            "model": None,
            "chunk_id": None, "chunk_hash": None,
        })
    count = 0
    for (from_label, to_label), payload in by_label.items():
        w.upsert_fact_edges(graph, relation, from_label, to_label, payload)
        count += len(payload)
    if count:
        logger.info("derived %s ×%d via %s", rule, count, record_key)
    return count
