"""Tests for graph.vector_store.search_above (25-plan.md §4.2, rungs 4/5).

Gated behind NEURON_INTEGRATION=1 and run against real Qdrant and real
Postgres/pgvector, matching the convention in tests/test_graph_integration.py
(real FalkorDB, unique scratch namespace per test, cleaned up in a finally
block / fixture teardown) rather than mocking either vector backend.
"""

from __future__ import annotations

import math
import os
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from graph import vector_store
from storage.postgres import PostgresConfigurationError, PostgresStore, PostgresVectorClient, database_url

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local Qdrant/Postgres",
)

_DIM = vector_store.EMBEDDING_DIMENSION


def _vec(similarity: float) -> list[float]:
    """A vector in the plane spanned by the first two axes, at cosine
    `similarity` to `_BASE` ([1, 0, 0, ...]). Every other axis is 0, so the
    dot product (and cosine similarity) with `_BASE` is exactly `similarity`
    regardless of the embedding's real dimensionality -- lets tests target
    an exact score without depending on a real embedding model."""
    theta = math.acos(max(-1.0, min(1.0, similarity)))
    return [math.cos(theta), math.sin(theta)] + [0.0] * (_DIM - 2)


_BASE = _vec(1.0)


def _qdrant_upsert(client, collection, uid, label, embedding, namespace_uid=None):
    payload = {"label": label, "uid": uid}
    if namespace_uid is not None:
        payload["namespace_uid"] = namespace_uid
    client.upsert(
        collection_name=collection,
        points=[PointStruct(
            id=uid,
            vector={vector_store.CONTENT_VECTOR: embedding, vector_store.NAME_VECTOR: embedding},
            payload=payload,
        )],
    )


def _pg_upsert(handle, collection, uid, label, embedding, namespace_uid=None):
    with handle.store.connect() as connection:
        connection.execute(
            """
            INSERT INTO entity_embeddings(
                collection, uid, label, content_embedding, name_embedding,
                embedded_model, namespace_uid
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (collection, uid) DO UPDATE SET
                label = excluded.label,
                content_embedding = excluded.content_embedding,
                name_embedding = excluded.name_embedding,
                namespace_uid = excluded.namespace_uid
            """,
            (collection, uid, label, embedding, embedding, "test-model", namespace_uid),
        )
        connection.commit()


class _Backend:
    """Thin adapter so the same test bodies run against both backends
    `search_above` supports."""

    def __init__(self, client, collection, upsert_fn):
        self.client = client
        self.collection = collection
        self._upsert_fn = upsert_fn

    def upsert(self, label: str, embedding: list[float], namespace_uid: str | None = None) -> str:
        uid = str(uuid.uuid4())
        self._upsert_fn(self.client, self.collection, uid, label, embedding, namespace_uid)
        return uid

    def search_above(self, label: str, embedding: list[float], min_similarity: float, **kwargs):
        return vector_store.search_above(
            self.client, label, embedding, min_similarity,
            collection=self.collection, **kwargs,
        )


@pytest.fixture(params=["qdrant", "postgres"])
def backend(request):
    if request.param == "qdrant":
        client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
        collection = f"test_search_above_{uuid.uuid4().hex}"
        vector_store.ensure_collection(client, collection)
        try:
            yield _Backend(client, collection, _qdrant_upsert)
        finally:
            client.delete_collection(collection_name=collection)
    else:
        if not database_url():
            pytest.skip("DATABASE_URL not configured for Postgres integration test")
        try:
            store = PostgresStore()
            store.bootstrap()
        except PostgresConfigurationError as exc:
            pytest.skip(str(exc))
        collection = f"test_search_above_{uuid.uuid4().hex}"
        handle = PostgresVectorClient(store)
        try:
            yield _Backend(handle, collection, _pg_upsert)
        finally:
            handle.delete_collection(collection)


def test_returns_multiple_candidates_above_threshold(backend):
    """The key behavior gap vs find_similar_uid: more than one hit, not
    just the top-1."""
    uid_a = backend.upsert("Decision", _vec(0.95))
    uid_b = backend.upsert("Decision", _vec(0.92))
    backend.upsert("Decision", _vec(0.50))  # below threshold, must be excluded

    hits = backend.search_above("Decision", _BASE, 0.90)

    assert {uid for uid, _ in hits} == {uid_a, uid_b}
    assert hits[0][0] == uid_a  # best first
    assert hits[0][1] > hits[1][1]


def test_max_similarity_excludes_at_or_above_upper_bound(backend):
    """Rung 5's gray zone is [0.75, 0.90) -- 0.90 itself must NOT come back."""
    uid_gray = backend.upsert("Decision", _vec(0.80))
    backend.upsert("Decision", _vec(0.90))  # at the boundary, excluded
    backend.upsert("Decision", _vec(0.95))  # well above, excluded
    backend.upsert("Decision", _vec(0.50))  # below the floor, excluded

    hits = backend.search_above("Decision", _BASE, 0.75, max_similarity=0.90)

    assert [uid for uid, _ in hits] == [uid_gray]


def test_label_filter_applies(backend):
    """No regression vs search()'s existing label-scoped behavior: a
    high-similarity candidate under a different label must not leak in."""
    uid_decision = backend.upsert("Decision", _vec(0.96))
    backend.upsert("Term", _vec(0.99))  # closer, but wrong label

    hits = backend.search_above("Decision", _BASE, 0.90)

    assert [uid for uid, _ in hits] == [uid_decision]


def test_namespace_uid_filter_narrows_results(backend):
    uid_ns1 = backend.upsert("Decision", _vec(0.95), namespace_uid="ns-1")
    backend.upsert("Decision", _vec(0.93), namespace_uid="ns-2")

    hits = backend.search_above("Decision", _BASE, 0.90, namespace_uid="ns-1")

    assert [uid for uid, _ in hits] == [uid_ns1]


def test_namespace_uid_none_is_all_namespaces(backend):
    uid_ns1 = backend.upsert("Decision", _vec(0.95), namespace_uid="ns-1")
    uid_ns2 = backend.upsert("Decision", _vec(0.93), namespace_uid="ns-2")

    hits = backend.search_above("Decision", _BASE, 0.90, namespace_uid=None)

    assert {uid for uid, _ in hits} == {uid_ns1, uid_ns2}


def test_limit_resource_ceiling_is_respected(backend):
    uids_best_first = [
        backend.upsert("Decision", _vec(sim))
        for sim in (0.99, 0.98, 0.97, 0.96, 0.95)
    ]

    hits = backend.search_above("Decision", _BASE, 0.90, limit=3)

    assert len(hits) == 3
    assert [uid for uid, _ in hits] == uids_best_first[:3]


def test_empty_when_nothing_clears_threshold(backend):
    backend.upsert("Decision", _vec(0.50))
    backend.upsert("Decision", _vec(0.30))

    hits = backend.search_above("Decision", _BASE, 0.90)

    assert hits == []
