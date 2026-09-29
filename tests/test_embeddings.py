from __future__ import annotations

import numpy as np
import pytest

from graph.storage import embeddings


class FakeModel:
    tokenizer = None

    def encode(self, texts, **kwargs):
        self.kwargs = kwargs
        return np.ones((len(texts), 1024), dtype=np.float32)


def test_bge_dense_vectors_are_normalized_and_1024_dimensional(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(embeddings, "_load_model", lambda _name: fake)
    result = embeddings.embed_texts(["alpha", "beta"])

    assert len(result.vectors) == 2
    assert len(result.vectors[0]) == 1024
    assert fake.kwargs["normalize_embeddings"] is True
    assert fake.kwargs["convert_to_numpy"] is True


def test_wrong_dimension_fails_before_storage(monkeypatch):
    fake = FakeModel()
    fake.encode = lambda *_args, **_kwargs: np.ones((1, 3), dtype=np.float32)
    monkeypatch.setattr(embeddings, "_load_model", lambda _name: fake)

    with pytest.raises(RuntimeError, match="3-dimensional"):
        embeddings.embed_texts(["alpha"])


def test_write_time_content_is_bounded_without_mutating_source_text():
    original = "x" * (embeddings.WRITE_TIME_EMBED_CHARS + 100)

    clipped = embeddings.write_time_content(original, "SourceFile")

    assert len(clipped) == embeddings.WRITE_TIME_EMBED_CHARS
    assert len(original) == embeddings.WRITE_TIME_EMBED_CHARS + 100


def test_mps_default_uses_small_inference_micro_batches(monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(embeddings, "_load_model", lambda _name: fake)
    monkeypatch.setattr(embeddings, "embedding_device", lambda: "mps")
    monkeypatch.delenv("EMBEDDING_BATCH_SIZE", raising=False)

    embeddings._embed_in_process(["alpha"], "BAAI/bge-m3")

    assert fake.kwargs["batch_size"] == 4
