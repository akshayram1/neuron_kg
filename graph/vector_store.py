"""Dense vector index with PostgreSQL/pgvector and legacy Qdrant adapters.

Postgres is selected when ``VECTOR_BACKEND=postgres`` (or when DATABASE_URL
is configured and VECTOR_BACKEND is unset). Qdrant remains as a compatibility
backend while existing deployments migrate.

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

from storage.postgres import PostgresVectorClient, database_url

logger = logging.getLogger("neuron.vector_store")

COLLECTION = os.getenv("VECTOR_COLLECTION", os.getenv("QDRANT_COLLECTION", "neuron_entities"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_DIMENSION = 1536  # text-embedding-3-small
# Changing the model or dimension requires recreating the collection:
#   uv run python -m scripts.rebuild_vectors --recreate

# Named vector channels. See `ensure_collection` for why there are two.
NAME_VECTOR = "name"
CONTENT_VECTOR = "content"

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


def build_client() -> QdrantClient | PostgresVectorClient:
    backend = os.getenv("VECTOR_BACKEND", "postgres" if database_url() else "qdrant").lower()
    if backend == "postgres":
        handle = PostgresVectorClient()
        handle.store.bootstrap()
        return handle
    if backend != "qdrant":
        raise ValueError("VECTOR_BACKEND must be 'postgres' or 'qdrant'")
    return QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))


_client: QdrantClient | PostgresVectorClient | None = None


def client() -> QdrantClient | PostgresVectorClient:
    """Process-wide lazy client, collection ensured on first use. Qdrant's
    HTTP client is safe to share, and holding it here keeps every call site
    free of plumbing another handle through signatures that already carry
    `graph` and the OpenAI client."""
    global _client
    if _client is None:
        _client = build_client()
        ensure_collection(_client)
    return _client


def ensure_collection(
    client: QdrantClient | PostgresVectorClient, collection: str = COLLECTION,
) -> None:
    """Idempotent. Quantized vectors stay in RAM (small, fast first pass),
    originals live on disk and are only read to rescore the shortlist — the
    standard Qdrant memory-efficiency setup.

    TWO named vectors per point, not one. A question comes in two shapes —
    a short one that names a thing ("which file handles the Vulcan client")
    and a long one that describes it — and a single embedding of
    `name + full content` serves the first shape badly: the short side
    systematically produces smaller distances, so a 95-byte empty
    `__init__.py` (whose entire embedded text IS its path) beat the real
    19,564-byte `client.py` on exactly that query. Verified on real data
    here, and independently arrived at by Utopia, which hit the same bias
    four times before splitting its class vectors the same way
    (`utopia/migrations/0003_graph.sql:3-13`).

    So: `name` embeds the node's name/path alone, `content` embeds the full
    `search_text`. Short queries are matched against short documents.
    """
    if isinstance(client, PostgresVectorClient):
        client.store.bootstrap()
        return
    if client.collection_exists(collection):
        return
    params = VectorParams(
        size=EMBEDDING_DIMENSION,
        distance=Distance.COSINE,
        on_disk=True,
    )
    client.create_collection(
        collection_name=collection,
        vectors_config={NAME_VECTOR: params, CONTENT_VECTOR: params},
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
        collection_name=collection, field_name="label", field_schema="keyword"
    )
    logger.info("created Qdrant collection %s (named vectors)", collection)


def upsert_vectors(
    client: QdrantClient | PostgresVectorClient,
    rows: list[dict[str, Any]], collection: str = COLLECTION,
) -> None:
    """rows: {uid, label, embedding, name_embedding, embedded_text?}.
    `uid` is already a uuid5 string, which Qdrant accepts directly as a point
    id — so a re-upsert of the same entity overwrites in place rather than
    duplicating, matching the graph's MERGE semantics.

    `embedded_text` (the exact string that was embedded) and
    `embedded_model` go in the payload instead of an `embedded_at`
    timestamp: a timestamp answers "was this embedded", but the question that
    actually matters when backfilling is "is this embedding still of this
    text", and only the text itself answers that.
    """
    if not rows:
        return
    if isinstance(client, PostgresVectorClient):
        client.store.vector_upsert(collection, rows)
        return
    client.upsert(
        collection_name=collection,
        points=[
            PointStruct(
                id=row["uid"],
                vector={
                    CONTENT_VECTOR: row["embedding"],
                    # A node with no distinct name reuses its content vector
                    # rather than leaving the channel empty, so the name
                    # channel never silently drops a whole label.
                    NAME_VECTOR: row.get("name_embedding") or row["embedding"],
                },
                payload={
                    "label": row["label"], "uid": row["uid"],
                    **({"embedded_text": row["embedded_text"]} if row.get("embedded_text") else {}),
                    "embedded_model": row.get("embedded_model") or EMBEDDING_MODEL,
                },
            )
            for row in rows
        ],
    )


def _label_filter(label: str | None) -> Filter | None:
    if not label:
        return None
    return Filter(must=[FieldCondition(key="label", match=MatchValue(value=label))])


def search(
    client: QdrantClient | PostgresVectorClient,
    embedding: list[float], *, label: str | None = None, limit: int = 20,
    collection: str = COLLECTION, using: str = CONTENT_VECTOR,
) -> list[tuple[str, float]]:
    """Returns (uid, similarity) pairs, best first. Similarity is cosine in
    [-1, 1]; 1.0 means identical.

    `using` picks the named channel (`name` or `content`). Scores from the
    two channels are NOT comparable and must never be merged by score — see
    `graph.search.interleave`.
    """
    if isinstance(client, PostgresVectorClient):
        try:
            return client.store.vector_search(
                collection, embedding, label=label, limit=limit, channel=using,
            )
        except Exception:
            logger.exception(
                "pgvector search failed for label=%s collection=%s using=%s",
                label, collection, using,
            )
            return []
    try:
        result = client.query_points(
            collection_name=collection,
            query=embedding,
            using=using,
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
        logger.exception(
            "Qdrant search failed for label=%s collection=%s using=%s", label, collection, using
        )
        return []
    return [(str(point.payload.get("uid") or point.id), float(point.score)) for point in result.points]


def find_similar_uid(
    client: QdrantClient | PostgresVectorClient,
    label: str, embedding: list[float], min_similarity: float = 0.90,
    collection: str = COLLECTION,
) -> str | None:
    """Write-time entity dedup: reuse an existing node's uid when the LLM
    re-extracted the same real-world thing under slightly different wording.

    `min_similarity` is a COSINE SIMILARITY (not a distance — see the module
    docstring). 0.90 corresponds to the ~0.10 cosine *distance* threshold
    that was calibrated on real embeddings, where a genuine near-duplicate
    pair scored ~0.04 distance (~0.96 similarity) and an unrelated pair
    ~0.80 distance (~0.20 similarity)."""
    hits = search(client, embedding, label=label, limit=1, collection=collection)
    if hits and hits[0][1] >= min_similarity:
        return hits[0][0]
    return None


def delete_vectors(
    client: QdrantClient | PostgresVectorClient,
    uids: Iterable[str], collection: str = COLLECTION,
) -> None:
    ids = list(uids)
    if not ids:
        return
    if isinstance(client, PostgresVectorClient):
        client.store.vector_delete(collection, ids)
        return
    client.delete(collection_name=collection, points_selector=ids)


def count(client: QdrantClient | PostgresVectorClient, collection: str = COLLECTION) -> int:
    if isinstance(client, PostgresVectorClient):
        return client.store.vector_count(collection)
    return client.count(collection_name=collection, exact=True).count
