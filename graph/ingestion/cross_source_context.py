"""Retrieve bounded cross-source context for ingestion-time adjudication.

Dense retrieval and Laya only nominate existing nodes.  The returned UID
allow-list is consumed by :mod:`graph.ingestion.semantic_pass`, where the LLM
must explicitly choose a candidate and all normal evidence, identity,
ontology, and direction gates still run before any edge is written.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from connectors.core.ledger import PendingChunk
from graph.ingestion.semantic_pass import SemanticContext
from graph.retrieval.rerank import (
    LayaReranker,
    Reranker,
    candidates_from_hits,
    select_final,
)
from graph.retrieval.search import SearchHit
from graph.storage import vector_store
from graph.storage.embeddings import embed_texts

logger = logging.getLogger("neuron.cross_source_context")

DEFAULT_POOL_SIZE = 20
DEFAULT_TOP_K = 7
DEFAULT_THRESHOLD = 0.5


@dataclass(frozen=True)
class CrossSourceCandidate:
    uid: str
    label: str
    name: str
    summary: str
    similarity: float
    providers: tuple[str, ...]
    record_keys: tuple[str, ...]
    source_time: str | None = None


def _provider(record_key: str) -> str:
    return record_key.split(":", 1)[0].strip().lower()


def _candidate_rows(graph, ranked_hits: list[tuple[str, float]]) -> list[CrossSourceCandidate]:
    """Hydrate vector UIDs from the authoritative graph in vector order."""
    if not ranked_hits:
        return []
    scores = dict(ranked_hits)
    rows = graph.query(
        "MATCH (n)-[:MENTIONED_IN]->(sr:SourceRecord) "
        "WHERE n.uid IN $uids AND sr.deleted_at IS NULL "
        "RETURN n.uid, labels(n), coalesce(n.name, n.path, n.issue_key, n.sha, n.uid), "
        "coalesce(n.search_text, n.name, n.path, ''), "
        "collect(DISTINCT sr.provider), collect(DISTINCT sr.record_key), max(sr.source_time)",
        params={"uids": [uid for uid, _score in ranked_hits]},
    ).result_set
    hydrated: dict[str, CrossSourceCandidate] = {}
    for uid, labels, name, summary, providers, record_keys, source_time in rows:
        clean_providers = tuple(sorted(str(value).lower() for value in (providers or []) if value))
        clean_keys = tuple(sorted(str(value) for value in (record_keys or []) if value))
        hydrated[str(uid)] = CrossSourceCandidate(
            uid=str(uid), label=str((labels or [""])[0]), name=str(name or uid),
            summary=str(summary or name or ""), similarity=float(scores[str(uid)]),
            providers=clean_providers, record_keys=clean_keys,
            source_time=str(source_time) if source_time is not None else None,
        )
    return [hydrated[uid] for uid, _score in ranked_hits if uid in hydrated]


def _format_candidate(candidate: CrossSourceCandidate, window: str, laya_score: float | None) -> str:
    score = "off" if laya_score is None else f"{laya_score:.4f}"
    records = ", ".join(candidate.record_keys[:3])
    if len(candidate.record_keys) > 3:
        records += f", +{len(candidate.record_keys) - 3} more"
    return (
        f"[CANDIDATE uid={candidate.uid} kind={candidate.label} name={candidate.name!r} "
        f"providers={','.join(candidate.providers)} vector_similarity={candidate.similarity:.4f} "
        f"laya_score={score} source_time={candidate.source_time or 'unknown'} "
        f"records={records or 'unknown'}]\n{window}"
    )


def build_cross_source_context_provider(
    graph,
    *,
    collection: str = vector_store.COLLECTION,
    reranker: Reranker | None = None,
    embedder: Callable[[list[str]], object] | None = None,
    vector_client=None,
    pool_size: int | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
) -> Callable[[PendingChunk], SemanticContext | None]:
    """Build the callback consumed by ``run_semantic_pass``.

    Only candidates supported by a different connector provider survive the
    graph hydration step.  A Jira chunk can therefore see Notion/Bitbucket
    nodes, but cannot accidentally use its own Jira projection as supposed
    corroborating history.
    """
    pool_size = pool_size or int(os.getenv("NEURON_INGEST_CONTEXT_POOL", str(DEFAULT_POOL_SIZE)))
    top_k = top_k or int(os.getenv("NEURON_INGEST_CONTEXT_TOP_K", str(DEFAULT_TOP_K)))
    threshold = threshold if threshold is not None else float(
        os.getenv("NEURON_INGEST_CONTEXT_THRESHOLD", str(DEFAULT_THRESHOLD))
    )
    mode = os.getenv("NEURON_INGEST_RERANK", os.getenv("NEURON_RERANK", "laya")).lower()
    if mode not in {"laya", "off"}:
        raise ValueError("NEURON_INGEST_RERANK must be 'laya' or 'off'")
    scorer = reranker or (LayaReranker() if mode == "laya" else None)
    embed = embedder or (lambda texts: embed_texts(texts))
    vectors = vector_client or vector_store.client()

    def provide(chunk: PendingChunk) -> SemanticContext | None:
        current_provider = _provider(chunk.record_key)
        response = embed([vector_store.truncate_for_embedding(chunk.text)])
        embedding = response.vectors[0]
        ranked = vector_store.search(
            # Same-provider hits are removed after graph hydration. Overfetch
            # so they cannot consume the entire cross-source pool.
            vectors, embedding, limit=pool_size * 4, collection=collection,
            using=vector_store.CONTENT_VECTOR,
        )
        candidates = [
            item for item in _candidate_rows(graph, ranked)
            if any(provider != current_provider for provider in item.providers)
            and chunk.record_key not in item.record_keys
        ][:pool_size]
        if not candidates:
            logger.info("ingest context: no cross-source candidates record=%s", chunk.record_key)
            return None

        hits = [
            SearchHit(
                uid=item.uid, label=item.label, name=item.name, summary=item.summary,
                score=item.similarity, methods=["vector"],
            )
            for item in candidates
        ]
        # Laya was trained on a short retrieval question. Keep enough of the
        # source header/body to express intent while leaving its 512-token
        # sequence room for the candidate window.
        rerank_query = chunk.text[:800]
        rerank_candidates = candidates_from_hits(rerank_query, hits)
        selected_scores = []
        rerank_failed = False
        if scorer is not None:
            try:
                scores = scorer.score(rerank_query, rerank_candidates)
                selected_scores = select_final(
                    scores, threshold=threshold, min_keep=0, max_keep=top_k,
                )
            except Exception:
                # Retrieval is advisory. Preserve ingestion availability and
                # fall back to the bounded vector order; the LLM and hard
                # admission gates remain authoritative for every graph write.
                logger.exception(
                    "Laya ingestion rerank failed; using vector order record=%s",
                    chunk.record_key,
                )
                rerank_failed = True
        score_by_uid = {item.uid: item.score for item in selected_scores}
        if scorer is None or rerank_failed:
            selected_uids = [item.uid for item in candidates[:top_k]]
        else:
            selected_uids = [item.uid for item in selected_scores]

        by_uid = {item.uid: item for item in candidates}
        window_by_uid = {item.uid: item.window for item in rerank_candidates}
        chosen = [by_uid[uid] for uid in selected_uids if uid in by_uid]
        if not chosen:
            return None
        text = "\n\n".join(
            _format_candidate(item, window_by_uid[item.uid], score_by_uid.get(item.uid))
            for item in chosen
        )
        logger.info(
            "ingest context: provider=%s pool=%d selected=%d record=%s",
            current_provider, len(candidates), len(chosen), chunk.record_key,
        )
        return SemanticContext(text=text, candidate_uids=frozenset(item.uid for item in chosen))

    return provide
