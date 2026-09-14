"""Deterministic derived facts with a proof chain.

Asserted edges always win: a derivation never closes or replaces a live
human/API fact of the same triple. Derived rows are marked `derived=true`
and carry `premise_fact_uids` so the entity panel and chat can say
"inferred because …". Materialization is cheap Cypher, not an OWL reasoner.
"""

from __future__ import annotations

import logging

from falkordb import Graph

from graph import writer as w

logger = logging.getLogger("neuron.derived")

# A wrong rule multiplies wrong edges. Keep the set small and named.
PARENT_IMPLEMENTS = "parent_implements"
PARENT_DOCUMENTS = "parent_documents"
SHARED_CONCEPT = "shared_concept"
PR_IMPLEMENTS = "pr_implements"
VERIFIED_EMAIL = "verified_email"


def materialize_around(graph: Graph, seed_uid: str, record_key: str) -> int:
    """Recompute derived edges that touch `seed_uid` (or its parent/children)."""
    written = 0
    written += _lift_through_parent(graph, seed_uid, record_key, "IMPLEMENTS", PARENT_IMPLEMENTS)
    written += _lift_through_parent(graph, seed_uid, record_key, "DOCUMENTS", PARENT_DOCUMENTS)
    written += _shared_concept_documents(graph, seed_uid, record_key)
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


def _shared_concept_documents(graph: Graph, seed_uid: str, record_key: str) -> int:
    """Document and WorkItem that share a Term or System → Document DOCUMENTS WorkItem."""
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
        RETURN doc.uid, 'Document', wi.uid, 'WorkItem',
               concept.name, r1.fact_uid, r1.source_record_keys, r1.valid_at,
               r2.fact_uid
        """,
        params={"uid": seed_uid},
    ).result_set
    written = 0
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row[0], row[2])
        grouped.setdefault(key, {
            "from_uid": row[0], "from_label": row[1], "to_uid": row[2], "to_label": row[3],
            "concept": row[4], "premises": [], "keys": list(row[6] or []), "valid_at": row[7],
        })
        grouped[key]["premises"].extend([uid for uid in (row[5], row[8]) if uid])
    for item in grouped.values():
        written += _write_derived(graph, "DOCUMENTS", SHARED_CONCEPT, record_key, [[
            item["from_uid"], item["from_label"], item["to_uid"], item["to_label"],
            item["concept"], item["premises"][0] if item["premises"] else None,
            item["keys"], item["valid_at"],
        ]], {
            "evidence": lambda row: f"Inferred: both mention {row[4]}.",
            "premises": lambda item_row, bundled=item: bundled["premises"],
        })
    return written


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
