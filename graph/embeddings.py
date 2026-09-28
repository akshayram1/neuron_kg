"""Shared local BGE-M3 embedding implementation."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Sequence

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DIMENSION = 1024
SCHEMA_VERSION = 2
MAX_SEQUENCE_LENGTH = int(os.getenv("EMBEDDING_MAX_TOKENS", "8192"))

_model: Any | None = None
_model_key: tuple[str, str, str] | None = None
_lock = threading.Lock()
_encode_lock = threading.Lock()


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int
    model: str


def _load_model(model_name: str | None = None):
    """Lazily load BGE-M3 once; importing Neuron never downloads a model."""
    global _model, _model_key
    name = model_name or MODEL_NAME
    device = os.getenv("EMBEDDING_DEVICE", "").strip()
    revision = os.getenv("EMBEDDING_MODEL_REVISION", "").strip()
    key = (name, device, revision)
    with _lock:
        if _model is None or _model_key != key:
            from sentence_transformers import SentenceTransformer

            kwargs: dict[str, Any] = {}
            if device:
                kwargs["device"] = device
            if revision:
                kwargs["revision"] = revision
            # Prefer the existing Hugging Face cache so every backend restart
            # does not block on remote HEAD requests. A fresh machine falls
            # back to the normal download path once.
            try:
                _model = SentenceTransformer(name, local_files_only=True, **kwargs)
            except OSError:
                _model = SentenceTransformer(name, **kwargs)
            _model.max_seq_length = MAX_SEQUENCE_LENGTH
            dimension_fn = getattr(
                _model, "get_embedding_dimension", _model.get_sentence_embedding_dimension,
            )
            dimension = int(dimension_fn())
            if dimension != DIMENSION:
                raise RuntimeError(
                    f"{name} produced {dimension}-dimensional vectors; expected {DIMENSION}"
                )
            _model_key = key
    return _model


def _token_count(model: Any, texts: Sequence[str]) -> int:
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        return sum(max(1, len(text) // 4) for text in texts)
    encoded = tokenizer(
        list(texts), truncation=True, max_length=MAX_SEQUENCE_LENGTH,
        padding=False, add_special_tokens=True,
    )
    return sum(len(ids) for ids in encoded.get("input_ids", []))


def _embed_in_process(clean: list[str], name: str) -> EmbeddingResult:
    model = _load_model(name)
    # A single backend process shares one model. Serialize inference so two
    # web requests cannot make PyTorch run competing forwards on that model.
    with _encode_lock:
        vectors = model.encode(
            clean,
            batch_size=max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "16"))),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
    output = vectors.tolist() if hasattr(vectors, "tolist") else [list(v) for v in vectors]
    for vector in output:
        if len(vector) != DIMENSION:
            raise RuntimeError(
                f"{name} returned a {len(vector)}-dimensional vector; expected {DIMENSION}"
            )
    return EmbeddingResult(output, _token_count(model, clean), name)


def embed_texts(texts: Sequence[str], *, model_name: str | None = None) -> EmbeddingResult:
    """Return normalized dense vectors from the backend's shared local model."""
    clean = [str(text or " ") for text in texts]
    name = model_name or MODEL_NAME
    if not clean:
        return EmbeddingResult([], 0, name)
    return _embed_in_process(clean, name)


def reset_model_cache() -> None:
    global _model, _model_key
    with _lock:
        _model = None
        _model_key = None
