"""Hybrid search (plan.md §6) — BM25 fulltext + vector similarity, fused with
reciprocal rank fusion (RRF) computed in Python. This is what Graphiti's
`NODE_HYBRID_SEARCH_RRF` recipe did internally; here it's built directly on
FalkorDB's native fulltext/vector indexes (plan.md §2.5) since there's no
framework doing it for us anymore.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from falkordb import Graph
from openai import OpenAI

from graph.schema import FULLTEXT_LABELS, VECTOR_LABELS

logger = logging.getLogger("neuron.search")

RRF_K = 60  # standard RRF constant -- damps the impact of any single rank

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _fulltext_query(text: str) -> str:
    """RediSearch (what FalkorDB's fulltext index uses under the hood) treats
    space-separated terms as an implicit AND — a full natural-language
    question like "why did we choose redis" then requires every one of those
    words to appear in the same document, which usually matches nothing.
    OR-joining the terms (verified against a real query) turns it into "any
    of these words", which is what you actually want for free-text search."""
    words = _WORD_RE.findall(text.lower())
    return "|".join(words) if words else text


@dataclass
class SearchHit:
    uid: str
    label: str
    name: str
    summary: str
    score: float
    methods: list[str] = field(default_factory=list)


def embed_query(client: OpenAI, text: str, model: str = "text-embedding-3-small") -> list[float]:
    return client.embeddings.create(model=model, input=[text]).data[0].embedding


def _fulltext_search(
    graph: Graph, label: str, query: str, limit: int, providers: list[str] | None = None
) -> list[tuple[str, str, str, float]]:
    try:
        provider_clause = (
            "MATCH (node)-[:MENTIONED_IN]->(sr:SourceRecord) "
            "WHERE sr.deleted_at IS NULL AND sr.provider IN $providers "
            if providers else ""
        )
        rows = graph.query(
            f"CALL db.idx.fulltext.queryNodes('{label}', $query) YIELD node, score "
            f"{provider_clause}RETURN DISTINCT node.uid, node.name, node.search_text, score "
            f"LIMIT {int(limit)}",
            params={"query": _fulltext_query(query), **({"providers": providers} if providers else {})},
        ).result_set
    except Exception:
        logger.exception("fulltext search failed for label=%s query=%r", label, query)
        return []
    return [(r[0], r[1] or "", r[2] or "", float(r[3])) for r in rows]


def _vector_search(
    graph: Graph, label: str, embedding: list[float], limit: int,
    providers: list[str] | None = None,
) -> list[tuple[str, str, str, float]]:
    try:
        provider_clause = (
            "MATCH (node)-[:MENTIONED_IN]->(sr:SourceRecord) "
            "WHERE sr.deleted_at IS NULL AND sr.provider IN $providers "
            if providers else ""
        )
        rows = graph.query(
            f"CALL db.idx.vector.queryNodes('{label}', 'embedding', {int(limit)}, vecf32($embedding)) "
            f"YIELD node, score {provider_clause}"
            "RETURN DISTINCT node.uid, node.name, node.search_text, score",
            params={"embedding": embedding, **({"providers": providers} if providers else {})},
        ).result_set
    except Exception:
        logger.exception("vector search failed for label=%s", label)
        return []
    return [(r[0], r[1] or "", r[2] or "", float(r[3])) for r in rows]


def find_similar_uid(graph: Graph, label: str, embedding: list[float], max_distance: float = 0.1) -> str | None:
    """Lightweight entity dedup at write time (plan.md §5 Tier 1 extension):
    if a node of this label already exists with an embedding this close, the
    LLM almost certainly re-extracted the same real-world thing with slightly
    different wording — e.g. "use Redis-backed sliding-window rate limiting"
    vs "use a Redis-backed sliding-window rate limiter" from two near-
    duplicate tickets, which exact-name matching (`semantic_uid`) misses
    entirely since the strings differ.

    Calibrated against real embeddings (text-embedding-3-small): FalkorDB's
    vector score is a DISTANCE (0 = identical), not similarity — a genuine
    near-duplicate pair scored ~0.04, an unrelated pair scored ~0.80. 0.1
    leaves comfortable margin above near-duplicates and well below unrelated
    content.

    This is intentionally narrower than full Tier-3 resolution (plan.md §5):
    it only ever reuses an EXISTING uid for what look like the same entity,
    never creates a new cross-entity edge on a similarity score alone."""
    try:
        rows = graph.query(
            f"CALL db.idx.vector.queryNodes('{label}', 'embedding', 1, vecf32($embedding)) "
            "YIELD node, score RETURN node.uid, score",
            params={"embedding": embedding},
        ).result_set
    except Exception:
        logger.exception("similarity dedup check failed for label=%s", label)
        return None
    if rows and rows[0][1] <= max_distance:
        return rows[0][0]
    return None


def hybrid_search(
    graph: Graph,
    client: OpenAI,
    query: str,
    *,
    labels: list[str] | None = None,
    limit: int = 8,
    per_method_limit: int = 20,
    providers: list[str] | None = None,
) -> list[SearchHit]:
    """Search across every content-bearing label, fuse fulltext + vector
    rankings via RRF, return the top `limit` overall. Labels default to every
    label that has BOTH index types (plan.md §2.5's FULLTEXT_LABELS ∩
    VECTOR_LABELS) — Commit has fulltext but no vector index, so it's included
    in the fulltext pass but would never surface from a pure vector query;
    restricting to the intersection keeps both signals meaningful for every
    candidate."""
    search_labels = labels or sorted(set(FULLTEXT_LABELS) & set(VECTOR_LABELS))
    embedding = embed_query(client, query)

    rrf_scores: dict[str, float] = {}
    info: dict[str, SearchHit] = {}

    for label in search_labels:
        fulltext_hits = _fulltext_search(graph, label, query, per_method_limit, providers)
        for rank, (uid, name, summary, _score) in enumerate(fulltext_hits):
            rrf_scores[uid] = rrf_scores.get(uid, 0.0) + 1.0 / (RRF_K + rank + 1)
            hit = info.setdefault(uid, SearchHit(uid, label, name, summary, 0.0))
            if "fulltext" not in hit.methods:
                hit.methods.append("fulltext")

        vector_hits = _vector_search(graph, label, embedding, per_method_limit, providers)
        for rank, (uid, name, summary, _score) in enumerate(vector_hits):
            rrf_scores[uid] = rrf_scores.get(uid, 0.0) + 1.0 / (RRF_K + rank + 1)
            hit = info.setdefault(uid, SearchHit(uid, label, name, summary, 0.0))
            if "vector" not in hit.methods:
                hit.methods.append("vector")

    for uid, score in rrf_scores.items():
        info[uid].score = score

    ranked = sorted(info.values(), key=lambda h: h.score, reverse=True)
    return ranked[:limit]
