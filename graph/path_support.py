"""Path support scoring and explicit query-verified candidate creation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger
from graph.fact_predicates import live_fact_cypher
from graph.link_candidates import LayaRelationClassifier

EPSILON = 0.01


@dataclass(frozen=True)
class PathSupport:
    score: float
    pair_scores: tuple[float, ...]
    low_support: bool


def _count(graph: Graph, query: str, a_uid: str, b_uid: str) -> int:
    rows = graph.query(query, params={"a_uid": a_uid, "b_uid": b_uid}).result_set
    return int(rows[0][0]) if rows else 0


def _metadata(graph: Graph, a_uid: str, b_uid: str) -> tuple[str, str, str, str]:
    rows = graph.query(
        "MATCH (a {uid: $a_uid}), (b {uid: $b_uid}) "
        "RETURN coalesce(a.name, ''), coalesce(a.search_text, a.summary, a.statement, ''), "
        "coalesce(b.name, ''), coalesce(b.search_text, b.summary, b.statement, '')",
        params={"a_uid": a_uid, "b_uid": b_uid},
    ).result_set
    return tuple(str(value or "") for value in rows[0]) if rows else ("", "", "", "")


def direct_edge_exists(graph: Graph, a_uid: str, b_uid: str) -> bool:
    return _count(
        graph,
        f"MATCH (a {{uid: $a_uid}})-[r]-(b {{uid: $b_uid}}) "
        f"WHERE {live_fact_cypher('r')} RETURN count(r)",
        a_uid, b_uid,
    ) > 0


def pair_support(graph: Graph, a_uid: str, b_uid: str) -> float:
    """Score one adjacent cited pair using only graph-verifiable signals."""
    if a_uid == b_uid:
        return 1.0
    scores = [EPSILON]
    if direct_edge_exists(graph, a_uid, b_uid):
        scores.append(1.0)
    if _count(
        graph,
        f"MATCH (a {{uid: $a_uid}})-[r1]-(mid)-[r2]-(b {{uid: $b_uid}}) "
        "WHERE type(r1) <> 'MENTIONED_IN' AND type(r2) <> 'MENTIONED_IN' "
        f"AND {live_fact_cypher('r1')} AND {live_fact_cypher('r2')} RETURN count(DISTINCT mid)",
        a_uid, b_uid,
    ):
        scores.append(0.8)
    a_name, a_text, b_name, b_text = _metadata(graph, a_uid, b_uid)
    if ((a_name and a_name.casefold() in b_text.casefold())
            or (b_name and b_name.casefold() in a_text.casefold())):
        scores.append(0.7)
    if _count(
        graph,
        "MATCH (a {uid: $a_uid})-[:MENTIONED_IN]->(sr:SourceRecord)"
        "<-[:MENTIONED_IN]-(b {uid: $b_uid}) WHERE sr.deleted_at IS NULL RETURN count(sr)",
        a_uid, b_uid,
    ):
        scores.append(0.5)
    return max(scores)


def path_support(graph: Graph, node_uids: list[str], *, minimum: float = 0.4) -> PathSupport:
    ordered = list(dict.fromkeys(node_uids))
    if len(ordered) < 2:
        return PathSupport(score=1.0, pair_scores=(), low_support=False)
    pairs = tuple(pair_support(graph, left, right) for left, right in zip(ordered, ordered[1:]))
    score = math.exp(sum(math.log(max(EPSILON, value)) for value in pairs) / len(pairs))
    return PathSupport(score=score, pair_scores=pairs, low_support=score < minimum)


def propose_verified_links(
    graph: Graph,
    ledger: ConnectorLedger,
    node_uids: list[str],
    *,
    minimum: float = 0.6,
    classifier: LayaRelationClassifier | None = None,
) -> list[int]:
    """Create review candidates after an explicit positive user signal."""
    classifier = classifier or LayaRelationClassifier()
    ordered = list(dict.fromkeys(node_uids))
    created: list[int] = []
    for left, right in zip(ordered, ordered[1:]):
        score = pair_support(graph, left, right)
        if score < minimum or direct_edge_exists(graph, left, right):
            continue
        a_name, a_text, b_name, b_text = _metadata(graph, left, right)
        relation, confidence = classifier.classify(
            "\n".join(part for part in (a_text, b_text) if part), a_name, b_name,
        )
        if relation == "none" or confidence < 0.6:
            continue
        created.append(ledger.create_link_candidate(
            left, right, relation, derived_rule="query_verified", confidence=score,
        ))
    return created
