"""Qdrant-backed dense vector index.

Why Qdrant instead of FalkorDB's built-in vector index: FalkorDB stores
vectors as full-precision float32 in RAM with no quantization option
(verified against the running instance — its vector index exposes only
`dimension`/`similarityFunction`/HNSW `M`/`ef*`), so ~6 KB per 1536-dim
embedding sits permanently in memory. Qdrant gives scalar quantization
(~4x smaller) with the originals on disk, so the embedding count stops
being bounded by RAM.

DESIGN RULE — the graph is the single source of truth; this collection is a
rebuildable projection of it.

That rule is why this module stores the absolute minimum per point:
`uid`, `label`, and the vector. No ACL, no provider, no `deleted_at`. Every
authorization and lifecycle filter stays in FalkorDB, where the truth lives,
and is applied to the uids Qdrant returns (callers over-fetch to absorb the
post-filter recall loss). Keeping metadata out of Qdrant means almost nothing
here *can* go stale, and whatever does can be regenerated with
`rebuild_from_graph()`.

SCORE SEMANTICS — read before touching thresholds. FalkorDB's vector index
returns a DISTANCE (0.0 = identical, larger = less similar). Qdrant with
COSINE returns a SIMILARITY (1.0 = identical, smaller = less similar). The
two are inverted, so a threshold ported straight across silently stops
matching anything. `find_similar_uid` here takes `min_similarity`, not
`max_distance`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import tiktoken
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    QuantizationSearchParams,
    ScalarQuantization,
    ScalarQuantizationConfig,
    ScalarType,
    SearchParams,
    VectorParams,
)

logger = logging.getLogger("neuron.vector_store")

COLLECTION = os.getenv("QDRANT_COLLECTION", "neuron_entities")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSION = 1536  # text-embedding-3-small
# Changing the model or dimension requires recreating the collection:
#   uv run python -m scripts.rebuild_vectors --recreate

_MAX_EMBEDDING_TOKENS = 8000  # OpenAI's hard cap is 8192; leave headroom
_encoding = tiktoken.get_encoding("cl100k_base")


def truncate_for_embedding(text: str) -> str:
    """Clip to the embedding API's input limit.

    Only SourceFile search_text (a whole file's content) is ever big enough
    to matter here -- verified live: adding SourceFile to VECTOR_LABELS hit
    `Invalid 'input[13]': maximum input length is 8192 tokens` on the first
    real rebuild. Truncating loses the tail of very large files, which is an
    acceptable lossy fallback for a projection that's rebuildable anyway --
    the graph's own `search_text` (used for fulltext + as the source of
    truth) is untouched."""
    tokens = _encoding.encode(text)
    if len(tokens) <= _MAX_EMBEDDING_TOKENS:
        return text
    return _encoding.decode(tokens[:_MAX_EMBEDDING_TOKENS])


def build_client() -> QdrantClient:
    return QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))


_client: QdrantClient | None = None


def client() -> QdrantClient:
    """Process-wide lazy client, collection ensured on first use. Qdrant's
    HTTP client is safe to share, and holding it here keeps every call site
    free of plumbing another handle through signatures that already carry
    `graph` and the OpenAI client."""
    global _client
    if _client is None:
        _client = build_client()
        ensure_collection(_client)
    return _client


def ensure_collection(client: QdrantClient) -> None:
    """Idempotent. Quantized vectors stay in RAM (small, fast first pass),
    originals live on disk and are only read to rescore the shortlist — the
    standard Qdrant memory-efficiency setup."""
    if client.collection_exists(COLLECTION):
        return
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(
            size=EMBEDDING_DIMENSION,
            distance=Distance.COSINE,
            on_disk=True,
        ),
        quantization_config=ScalarQuantization(
            scalar=ScalarQuantizationConfig(
                type=ScalarType.INT8,
                always_ram=True,
            )
        ),
    )
    # `label` is the only field we ever filter on inside Qdrant; everything
    # else is filtered in the graph.
    client.create_payload_index(
        collection_name=COLLECTION, field_name="label", field_schema="keyword"
    )
    logger.info("created Qdrant collection %s", COLLECTION)


def upsert_vectors(client: QdrantClient, rows: list[dict[str, Any]]) -> None:
    """rows: {uid, label, embedding}. `uid` is already a uuid5 string, which
    Qdrant accepts directly as a point id — so a re-upsert of the same entity
    overwrites in place rather than duplicating, matching the graph's MERGE
    semantics."""
    if not rows:
        return
    client.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=row["uid"],
                vector=row["embedding"],
                payload={"label": row["label"], "uid": row["uid"]},
            )
            for row in rows
        ],
    )


def _label_filter(label: str | None) -> Filter | None:
    if not label:
        return None
    return Filter(must=[FieldCondition(key="label", match=MatchValue(value=label))])


def search(
    client: QdrantClient, embedding: list[float], *, label: str | None = None, limit: int = 20
) -> list[tuple[str, float]]:
    """Returns (uid, similarity) pairs, best first. Similarity is cosine in
    [-1, 1]; 1.0 means identical."""
    try:
        result = client.query_points(
            collection_name=COLLECTION,
            query=embedding,
            query_filter=_label_filter(label),
            limit=limit,
            search_params=SearchParams(
                # Rescore the quantized shortlist against the on-disk
                # originals; without this, int8 quantization noise shows up
                # directly in the ranking.
                quantization=QuantizationSearchParams(rescore=True)
            ),
        )
    except Exception:
        logger.exception("Qdrant search failed for label=%s", label)
        return []
    return [(str(point.payload.get("uid") or point.id), float(point.score)) for point in result.points]


def find_similar_uid(
    client: QdrantClient, label: str, embedding: list[float], min_similarity: float = 0.90
) -> str | None:
    """Write-time entity dedup: reuse an existing node's uid when the LLM
    re-extracted the same real-world thing under slightly different wording.

    `min_similarity` is a COSINE SIMILARITY (not a distance — see the module
    docstring). 0.90 corresponds to the ~0.10 cosine *distance* threshold
    that was calibrated on real embeddings, where a genuine near-duplicate
    pair scored ~0.04 distance (~0.96 similarity) and an unrelated pair
    ~0.80 distance (~0.20 similarity)."""
    hits = search(client, embedding, label=label, limit=1)
    if hits and hits[0][1] >= min_similarity:
        return hits[0][0]
    return None


def delete_vectors(client: QdrantClient, uids: Iterable[str]) -> None:
    ids = list(uids)
    if not ids:
        return
    client.delete(collection_name=COLLECTION, points_selector=ids)


def count(client: QdrantClient) -> int:
    return client.count(collection_name=COLLECTION, exact=True).count
