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

from graph import vector_store
from graph.schema import FULLTEXT_LABELS
from graph.access import AccessScope
from graph.token_usage import TokenUsage

logger = logging.getLogger("neuron.search")

# Tuned against eval/argus_golden.jsonl (scripts/sweep_retrieval_params.py),
# not the RRF-literature default of 60. A real query surfaced why the default
# didn't fit here: `DATAOS-4051`'s exact-match FedRAMP/compliance ticket has a
# long, detailed description, so BM25's length normalization buried it at
# fulltext rank 9 despite ranking #1 on vector similarity by a wide margin
# (0.63 vs the runner-up's 0.49). k=60 damps rank differences so heavily that
# a weak-but-present fulltext signal on a short, generic-titled ticket could
# still outscore vector's confident #1 pick. Swept k in {10, 30, 60} x a
# vector-leg weight in {1.0, 1.5, 2.0, 3.0} x per_method_limit in {10, 20, 30}
# against the golden set: k=10 + vector_weight=3.0 + per_method_limit=30 took
# MRR from 0.39 -> 0.57 and recall from 0.50 -> 0.92 versus the old defaults.
# Re-run the sweep and update these three constants if retrieval quality
# regresses on a larger/different golden set later.
RRF_K = 10
VECTOR_LEG_WEIGHT = 3.0
# Qdrant knows nothing about ACLs, so its results get filtered in the graph
# afterwards; ask for more than we need so the filter doesn't starve the leg.
OVERFETCH_FACTOR = 4

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _fulltext_query(text: str) -> str:
    """RediSearch (what FalkorDB's fulltext index uses under the hood) treats
    space-separated terms as an implicit AND — a full natural-language
    question like "why did we choose redis" then requires every one of those
    words to appear in the same document, which usually matches nothing.
    OR-joining the terms (verified against a real query) turns it into "any
    of these words", which is what you actually want for free-text search.

    Each term also gets a `*` prefix-wildcard suffix. Verified live: a
    question naming a person ("is Animesh mentioned in argus") could never
    match `animeshtmdcio` — RediSearch tokenizes that compound identifier as
    one atomic token, so the literal word "animesh" alone is not equal to
    it. `animesh*` matches it as a prefix. Re-run against
    eval/argus_golden.jsonl after adding this: MRR/precision/recall
    unchanged (0.653/0.125/0.917), so it doesn't cost existing queries
    anything — it only adds matches a bare-word query couldn't get anyway."""
    words = _WORD_RE.findall(text.lower())
    return "|".join(f"{word}*" for word in words) if words else text


@dataclass
class SearchHit:
    uid: str
    label: str
    name: str
    summary: str
    score: float
    methods: list[str] = field(default_factory=list)


def embed_query(
    client: OpenAI, text: str, model: str = "text-embedding-3-small",
    token_usage: TokenUsage | None = None,
) -> list[float]:
    response = client.embeddings.create(model=model, input=[text])
    if token_usage is not None:
        token_usage.add(response.usage)
    return response.data[0].embedding


def _fulltext_search(
    graph: Graph, label: str, query: str, limit: int, scope: AccessScope,
    providers: list[str] | None = None,
) -> list[tuple[str, str, str, float]]:
    try:
        acl, acl_params = scope.cypher("sr", "search_acl")
        provider_clause = "AND sr.provider IN $providers" if providers else ""
        # ORDER BY is load-bearing, not cosmetic: RRF fusion (hybrid_search)
        # trusts each leg's row *position* as its rank. Without sorting by
        # score first, `LIMIT` keeps an arbitrary slice of whatever matched
        # and enumerate() hands out ranks that have nothing to do with
        # relevance -- verified on real data: the single best BM25 match for
        # a query (score 5.0, top of an unordered/unlimited scan) was
        # entirely absent from this leg's `LIMIT 20` once the query actually
        # matched more than 20 documents, because nothing had sorted the 20
        # kept rows to be the best 20.
        rows = graph.query(
            f"CALL db.idx.fulltext.queryNodes('{label}', $query) YIELD node, score "
            "MATCH (node)-[:MENTIONED_IN]->(sr:SourceRecord) "
            f"WHERE sr.deleted_at IS NULL AND {acl} {provider_clause} "
            "RETURN DISTINCT node.uid, node.name, node.search_text, score "
            "ORDER BY score DESC "
            f"LIMIT {int(limit)}",
            params={"query": _fulltext_query(query), **acl_params,
                    **({"providers": providers} if providers else {})},
        ).result_set
    except Exception:
        logger.exception("fulltext search failed for label=%s query=%r", label, query)
        return []
    return [(r[0], r[1] or "", r[2] or "", float(r[3])) for r in rows]


def _vector_search(
    graph: Graph, label: str, embedding: list[float], limit: int,
    scope: AccessScope, providers: list[str] | None = None,
) -> list[tuple[str, str, str, float]]:
    """Dense-vector leg is served by Qdrant; authorization stays in FalkorDB.

    Qdrant deliberately holds no ACL/provider/`deleted_at` metadata (see
    `graph/vector_store.py` — it is a rebuildable projection, so the less it
    knows the less can go stale), which means its top-K has to be filtered
    against the graph afterwards. Post-filtering can discard results, so we
    ask Qdrant for `OVERFETCH_FACTOR`x more than we need and keep its ranking
    for whatever survives — RRF downstream depends on rank order, so the
    original Qdrant order must be preserved, not the order the graph
    happens to return rows in.
    """
    hits = vector_store.search(
        vector_store.client(), embedding, label=label, limit=limit * OVERFETCH_FACTOR
    )
    if not hits:
        return []
    scores = {uid: score for uid, score in hits}
    try:
        acl, acl_params = scope.cypher("sr", "vector_acl")
        provider_clause = "AND sr.provider IN $providers" if providers else ""
        rows = graph.query(
            f"MATCH (node:{label}) WHERE node.uid IN $uids "
            "MATCH (node)-[:MENTIONED_IN]->(sr:SourceRecord) "
            f"WHERE sr.deleted_at IS NULL AND {acl} {provider_clause} "
            "RETURN DISTINCT node.uid, node.name, node.search_text",
            params={"uids": list(scores), **acl_params,
                    **({"providers": providers} if providers else {})},
        ).result_set
    except Exception:
        logger.exception("vector post-filter failed for label=%s", label)
        return []
    visible = {r[0]: (r[0], r[1] or "", r[2] or "", scores[r[0]]) for r in rows if r[0] in scores}
    return [visible[uid] for uid, _score in hits if uid in visible][:limit]


def find_similar_uid(graph: Graph, label: str, embedding: list[float], max_distance: float = 0.1) -> str | None:
    """Lightweight entity dedup at write time (plan.md §5 Tier 1 extension):
    if a node of this label already exists with an embedding this close, the
    LLM almost certainly re-extracted the same real-world thing with slightly
    different wording — e.g. "use Redis-backed sliding-window rate limiting"
    vs "use a Redis-backed sliding-window rate limiter" from two near-
    duplicate tickets, which exact-name matching (`semantic_uid`) misses
    entirely since the strings differ.

    Now served by Qdrant. `max_distance` is kept as the caller-facing unit
    (cosine distance, 0 = identical) because that is what the original
    calibration was expressed in — a genuine near-duplicate pair measured
    ~0.04 distance, an unrelated pair ~0.80 — but Qdrant reports cosine
    SIMILARITY, so it is converted here rather than at each call site.
    Verified against real embeddings through Qdrant: identical 1.0000,
    near-duplicate 0.9596, unrelated 0.2036.

    `graph` is unused now and kept only so existing call sites don't change;
    dedup no longer touches FalkorDB at all.

    This is intentionally narrower than full Tier-3 resolution (plan.md §5):
    it only ever reuses an EXISTING uid for what look like the same entity,
    never creates a new cross-entity edge on a similarity score alone."""
    return vector_store.find_similar_uid(
        vector_store.client(), label, embedding, min_similarity=1.0 - max_distance
    )


def hybrid_search(
    graph: Graph,
    client: OpenAI,
    query: str,
    *,
    labels: list[str] | None = None,
    limit: int = 8,
    per_method_limit: int = 30,
    providers: list[str] | None = None,
    scope: AccessScope,
    token_usage: TokenUsage | None = None,
) -> list[SearchHit]:
    """Search across every content-bearing label, fuse fulltext + vector
    rankings via RRF, return the top `limit` overall.

    Labels default to FULLTEXT_LABELS, not the FULLTEXT_LABELS ∩ VECTOR_LABELS
    intersection this used to use. That intersection excludes `Commit`
    entirely (it has no vector index) from *every* chat query, not just from
    the vector leg -- verified live: "where in the code was X added" could
    never once retrieve a Commit, regardless of how well its message matched,
    because it was never in `search_labels` to search in the first place.
    `_vector_search` on a label with no embedded nodes just returns nothing,
    same as before RRF existed; only the fulltext leg was ever meaningful for
    Commit, and it now actually gets to run.

    Follow-up finding once Commit could be searched at all: fulltext alone
    still missed a real commit whose message used an identifier verbatim
    (`ai_instructions`) against a question phrased in natural language ("AI
    instructions") -- RediSearch indexes `ai_instructions` as one atomic
    token, so the separate words "ai" and "instructions" never match it.
    Commit has since been added to VECTOR_LABELS/`_EMBEDDABLE_LABELS` so the
    vector leg (which does associate "AI instructions" with `ai_instructions`
    semantically) can catch what fulltext's literal tokenization can't.
    """
    search_labels = labels or FULLTEXT_LABELS
    embedding = embed_query(client, query, token_usage=token_usage)

    rrf_scores: dict[str, float] = {}
    info: dict[str, SearchHit] = {}

    for label in search_labels:
        fulltext_hits = _fulltext_search(graph, label, query, per_method_limit, scope, providers)
        for rank, (uid, name, summary, _score) in enumerate(fulltext_hits):
            rrf_scores[uid] = rrf_scores.get(uid, 0.0) + 1.0 / (RRF_K + rank + 1)
            hit = info.setdefault(uid, SearchHit(uid, label, name, summary, 0.0))
            if "fulltext" not in hit.methods:
                hit.methods.append("fulltext")

        vector_hits = _vector_search(graph, label, embedding, per_method_limit, scope, providers)
        for rank, (uid, name, summary, _score) in enumerate(vector_hits):
            rrf_scores[uid] = rrf_scores.get(uid, 0.0) + VECTOR_LEG_WEIGHT / (RRF_K + rank + 1)
            hit = info.setdefault(uid, SearchHit(uid, label, name, summary, 0.0))
            if "vector" not in hit.methods:
                hit.methods.append("vector")

    for uid, score in rrf_scores.items():
        info[uid].score = score

    ranked = sorted(info.values(), key=lambda h: h.score, reverse=True)
    return ranked[:limit]
