from __future__ import annotations

from graph.retrieval import search
from graph.storage import vector_store
from graph.storage.embeddings import EmbeddingResult
from graph.token_usage import TokenUsage


def test_query_uses_local_projection_model(monkeypatch):
    calls = []

    def fake_embed(texts, *, model_name=None):
        calls.append((texts, model_name))
        return EmbeddingResult([[0.1] * 1024], 7, model_name)

    monkeypatch.setattr(search, "embed_texts", fake_embed)
    usage = TokenUsage()
    result = search.embed_query(object(), "where is the client?", token_usage=usage)

    assert len(result) == vector_store.EMBEDDING_DIMENSION == 1024
    assert calls == [(["where is the client?"], "BAAI/bge-m3")]
    assert usage.input_tokens == usage.total_tokens == 7


def test_explicit_model_is_passed_to_shared_embedder(monkeypatch):
    seen = {}

    def fake_embed(_texts, *, model_name=None):
        seen["model"] = model_name
        return EmbeddingResult([[0.0] * 1024], 1, model_name)

    monkeypatch.setattr(search, "embed_texts", fake_embed)
    search.embed_query(object(), "query", model="BAAI/bge-m3")
    assert seen["model"] == "BAAI/bge-m3"
