"""Shared local BGE-M3 embeddings and the write-time batch around them.

The deterministic pass encounters one record at a time. `EmbeddingBatch`
queues those records and a single background thread encodes them, so a sync
can keep writing the graph while the model runs. `close_batch()` waits until
that sync's vectors are stored. One thread owns the model: Apple MPS is not
safe to call from the request thread and the embed worker at the same time.

A record can be committed to the ledger before its batch is flushed, so a
crash mid-batch can leave a committed record with no vector. A later sync
skips that record as unchanged. `scripts/rebuild_vectors.py` re-embeds from
the graph's own `search_text` and is the repair path.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Sequence

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
DIMENSION = 1024
SCHEMA_VERSION = 2
MAX_SEQUENCE_LENGTH = int(os.getenv("EMBEDDING_MAX_TOKENS", "8192"))
# Nodes keep their full text in FalkorDB for BM25 and context packing. Dense
# retrieval uses a bounded opening: BGE-M3 pads a micro-batch to its longest
# sequence, so one 32K-character source file can otherwise turn every item in
# that micro-batch into an 8192-token forward and stall ingestion on MPS.
WRITE_TIME_EMBED_CHARS = int(os.getenv("EMBEDDING_WRITE_MAX_CHARS", "4096"))
WORK_ITEM_EMBED_CHARS = min(4096, WRITE_TIME_EMBED_CHARS)


def write_time_content(text: str, label: str) -> str:
    """Bound dense input without changing the complete graph source text."""
    limit = WORK_ITEM_EMBED_CHARS if label == "WorkItem" else WRITE_TIME_EMBED_CHARS
    return text[:limit]

# Imported after the constants above: vector_store reads them while this
# module is still loading.
from graph.storage import vector_store

_model: Any | None = None
_model_key: tuple[str, str, str] | None = None
_lock = threading.Lock()
_encode_lock = threading.Lock()


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int
    model: str


def embedding_device() -> str:
    """``EMBEDDING_DEVICE`` when set, otherwise MPS on Apple Silicon, else CPU."""
    chosen = os.getenv("EMBEDDING_DEVICE", "").strip()
    if chosen:
        return chosen
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _load_model(model_name: str | None = None):
    """Lazily load BGE-M3 once; importing Neuron never downloads a model."""
    global _model, _model_key
    name = model_name or MODEL_NAME
    device = embedding_device()
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
    raw_batch = os.getenv("EMBEDDING_BATCH_SIZE", "").strip()
    if raw_batch:
        batch_size = max(1, int(raw_batch))
    else:
        # BGE-M3 is a large model. These are inference micro-batches inside a
        # write batch, not extra model calls; conservative defaults avoid MPS
        # memory pressure while retaining the ingest throughput benefit.
        batch_size = 4 if embedding_device() == "mps" else 8
    with _encode_lock:
        vectors = model.encode(
            clean,
            batch_size=batch_size,
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
    """Return normalized dense vectors from the backend's shared local model.

    Callers outside the embed worker wait on that thread. The worker encodes
    inline so a batch job cannot deadlock waiting for itself.
    """
    clean = [str(text or " ") for text in texts]
    name = model_name or MODEL_NAME
    if not clean:
        return EmbeddingResult([], 0, name)
    if threading.get_ident() == _worker_ident:
        return _embed_in_process(clean, name)
    job = _TextJob(clean, name)
    _submit(job)
    job.done.wait()
    if job.error is not None:
        raise job.error
    assert job.result is not None
    return job.result


def warmup_embedding_model() -> None:
    """Load BGE-M3 before the first sync so encode does not stall mid-ingest."""
    embed_texts(["."])
    logger.info("embedding model warm device=%s model=%s", embedding_device(), MODEL_NAME)


def reset_model_cache() -> None:
    global _model, _model_key
    with _lock:
        _model = None
        _model_key = None


logger = logging.getLogger("neuron.embed_batch")

# 16 records = 32 inputs (content + name each). The token budget limits peak
# memory when a batch contains unusually large files.
BATCH_RECORDS = 16
BATCH_TOKEN_BUDGET = 100_000

_active: ContextVar["EmbeddingBatch | None"] = ContextVar("neuron_embed_batch", default=None)

# A few queued batches is enough overlap. A full queue blocks the writer
# (backpressure) instead of holding every issue's text in memory.
_QUEUE_DEPTH = 4
_jobs: queue.Queue[Any] | None = None
_worker: threading.Thread | None = None
_worker_ident: int | None = None
_worker_lock = threading.Lock()


@dataclass
class _TextJob:
    texts: list[str]
    model: str
    done: threading.Event = field(default_factory=threading.Event)
    result: EmbeddingResult | None = None
    error: BaseException | None = None


@dataclass
class _BatchJob:
    rows: list[tuple[str, str, str, str]]
    model: str
    collection: str
    done: threading.Event = field(default_factory=threading.Event)
    count: int = 0
    error: BaseException | None = None


def _submit(job: _TextJob | _BatchJob) -> None:
    _ensure_worker()
    assert _jobs is not None
    _jobs.put(job)


def _ensure_worker() -> None:
    global _jobs, _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _jobs = queue.Queue(maxsize=_QUEUE_DEPTH)
        _worker = threading.Thread(target=_embed_worker, name="neuron-embed", daemon=True)
        _worker.start()


def _embed_worker() -> None:
    global _worker_ident
    _worker_ident = threading.get_ident()
    assert _jobs is not None
    while True:
        job = _jobs.get()
        try:
            if isinstance(job, _BatchJob):
                job.count = _encode_batch(job)
            elif isinstance(job, _TextJob):
                job.result = _embed_in_process(job.texts, job.model)
        except BaseException as exc:
            job.error = exc
            logger.exception("embedding job failed")
        finally:
            job.done.set()
            _jobs.task_done()


def _encode_batch(job: _BatchJob) -> int:
    rows = job.rows
    # Content first, then names, so `data[i]` and `data[len+i]` pair up.
    inputs = [content for _uid, _label, content, _name in rows]
    inputs += [name for _uid, _label, _content, name in rows]
    response = embed_texts(inputs, model_name=job.model)
    vector_store.upsert_vectors(vector_store.client(), [
        {
            "uid": uid, "label": label,
            "embedding": response.vectors[index],
            "name_embedding": response.vectors[len(rows) + index],
            "embedded_text": content[:400],
            "embedded_model": job.model,
            "embedded_content_hash": vector_store.embedding_content_hash(content),
            "embedding_schema_version": vector_store.EMBEDDING_SCHEMA_VERSION,
        }
        for index, (uid, label, content, _name) in enumerate(rows)
    ], collection=job.collection)
    logger.info("embedded batch of %d records", len(rows))
    return len(rows)


@dataclass
class EmbeddingBatch:
    model: str
    collection: str
    max_records: int = BATCH_RECORDS
    token_budget: int = BATCH_TOKEN_BUDGET
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)
    pending: list[_BatchJob] = field(default_factory=list)
    _tokens: int = 0
    requests: int = 0
    embedded: int = 0

    def add(self, uid: str, label: str, content: str, name: str) -> None:
        self.rows.append((uid, label, content, name))
        # len//4 is a deliberately cheap stand-in for a token count: this only
        # decides when to flush, and paying tiktoken on every record to make
        # the guard exact would cost more than the guard saves.
        self._tokens += len(content) // 4
        if len(self.rows) >= self.max_records or self._tokens >= self.token_budget:
            self.flush()

    def flush(self) -> int:
        """Hand the current rows to the embed worker and return immediately."""
        if not self.rows:
            return 0
        rows, self.rows, self._tokens = self.rows, [], 0
        job = _BatchJob(rows=rows, model=self.model, collection=self.collection)
        self.pending.append(job)
        _submit(job)
        return len(rows)

    def wait(self) -> int:
        """Block until every batch handed to the worker has been stored."""
        total = 0
        error: BaseException | None = None
        for job in self.pending:
            job.done.wait()
            if job.error is not None:
                error = job.error
                continue
            total += job.count
            self.requests += 1
            self.embedded += job.count
        self.pending.clear()
        if error is not None:
            raise error
        return total


def active_batch() -> EmbeddingBatch | None:
    return _active.get()


@contextmanager
def batch_embeddings(client: object, model: str, collection: str, **kwargs):
    """Collect write-time embeddings for the duration of one sync."""
    # ``client`` is retained in the public signature while connector callers
    # also use it for LLM work; local embedding does not use it.
    batch = EmbeddingBatch(model=model, collection=collection, **kwargs)
    token = _active.set(batch)
    try:
        yield batch
        batch.flush()
        batch.wait()
    finally:
        _active.reset(token)
        if batch.embedded:
            logger.info(
                "embedded %d records in %d local batch(es) (%.0fx fewer forwards than one-per-record)",
                batch.embedded, batch.requests, batch.embedded / max(batch.requests, 1),
            )


def open_batch(client: object, model: str, collection: str, **kwargs) -> EmbeddingBatch:
    """Explicit open/close instead of only a `with` block.

    The sync routines are long `try:` bodies; wrapping them in a context
    manager would mean re-indenting sixty lines of working code in four
    files, and a bulk re-indent is a poor trade for a bookkeeping change.
    `close_batch()` must be called on every exit path, success or failure --
    a dropped batch is a set of records with no vectors.
    """
    batch = EmbeddingBatch(model=model, collection=collection, **kwargs)
    _active.set(batch)
    return batch


def close_batch() -> int:
    """Flush whatever is pending and clear the slot. Safe to call twice."""
    batch = _active.get()
    if batch is None:
        return 0
    written = 0
    try:
        batch.flush()
        written = batch.wait()
    except Exception:
        # Called from failure handlers too. Raising here would replace the
        # real sync error with this one. The
        # records are already in the graph with their `search_text`, so
        # `scripts/rebuild_vectors.py` can still supply the vectors.
        logger.exception("embedding batch flush failed; run rebuild_vectors to repair")
    finally:
        _active.set(None)
    if batch.embedded:
        logger.info(
            "embedded %d records in %d local batch(es) instead of %d forwards",
            batch.embedded, batch.requests, batch.embedded,
        )
    return written
