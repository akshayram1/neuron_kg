from __future__ import annotations

import numpy as np
import pytest

from graph import embeddings


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

