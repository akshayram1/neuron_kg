"""Batch the write-time embedding calls.

The deterministic pass embeds one record at a time: 659 Bitbucket records
meant 659 separate OpenAI requests, issued back to back. Measured on an idle
machine a full `write_file` takes ~740 ms (so ~81 records/min, ~8 minutes for
that repo), but the real sync ran at 3-5 records/min -- 16-27x slower, in
bursts separated by long stalls. The work per record had not changed; the
number of REQUESTS had. Rate limiting and the occasional hung connection are
counted per request, not per token.

So: inside `batch_embeddings(...)`, `_embed_now` stops calling the API and
appends instead. One request then carries many records, cutting request count
by the batch size while embedding exactly the same text.

Scoped with a ContextVar rather than a module global so the batch belongs to
the sync that opened it, and an unrelated code path embedding something in
the meantime still goes straight out.

DURABILITY NOTE: a record is committed to the ledger before its batch is
flushed, so a crash mid-batch can leave a committed record with no vector,
which a later sync would skip as unchanged. `scripts/rebuild_vectors.py`
re-embeds from the graph's own `search_text` and is the repair path for
exactly that; the batch is kept small so the window is small.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from openai import OpenAI

from graph import vector_store

logger = logging.getLogger("neuron.embed_batch")

# 16 records = 32 inputs (content + name each). Each input is already clipped
# to 8k tokens, so a full batch stays under ~256k tokens -- inside the
# embeddings endpoint's per-request ceiling with room to spare. A token
# budget is enforced as well, for the case where every record is a big file.
BATCH_RECORDS = 16
BATCH_TOKEN_BUDGET = 100_000

_active: ContextVar["EmbeddingBatch | None"] = ContextVar("neuron_embed_batch", default=None)


@dataclass
class EmbeddingBatch:
    client: OpenAI
    model: str
    collection: str
    max_records: int = BATCH_RECORDS
    token_budget: int = BATCH_TOKEN_BUDGET
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)
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
        if not self.rows:
            return 0
        rows, self.rows, self._tokens = self.rows, [], 0
        # Content first, then names, so `data[i]` and `data[len+i]` pair up.
        inputs = [content for _uid, _label, content, _name in rows]
        inputs += [name for _uid, _label, _content, name in rows]
        response = self.client.embeddings.create(model=self.model, input=inputs)
        self.requests += 1
        vector_store.upsert_vectors(vector_store.client(), [
            {
                "uid": uid, "label": label,
                "embedding": response.data[index].embedding,
                "name_embedding": response.data[len(rows) + index].embedding,
                "embedded_text": content[:400],
                "embedded_model": self.model,
            }
            for index, (uid, label, content, _name) in enumerate(rows)
        ], collection=self.collection)
        self.embedded += len(rows)
        return len(rows)


def active_batch() -> EmbeddingBatch | None:
    return _active.get()


@contextmanager
def batch_embeddings(client: OpenAI, model: str, collection: str, **kwargs):
    """Collect write-time embeddings for the duration of one sync."""
    batch = EmbeddingBatch(client=client, model=model, collection=collection, **kwargs)
    token = _active.set(batch)
    try:
        yield batch
        batch.flush()
    finally:
        _active.reset(token)
        if batch.embedded:
            logger.info(
                "embedded %d records in %d request(s) (%.0fx fewer than one-per-record)",
                batch.embedded, batch.requests, batch.embedded / max(batch.requests, 1),
            )


def open_batch(client: OpenAI, model: str, collection: str, **kwargs) -> EmbeddingBatch:
    """Explicit open/close instead of only a `with` block.

    The sync routines are long `try:` bodies; wrapping them in a context
    manager would mean re-indenting sixty lines of working code in four
    files, and a bulk re-indent is a poor trade for a bookkeeping change.
    `close_batch()` must be called on every exit path, success or failure --
    a dropped batch is a set of records with no vectors.
    """
    batch = EmbeddingBatch(client=client, model=model, collection=collection, **kwargs)
    _active.set(batch)
    return batch


def close_batch() -> int:
    """Flush whatever is pending and clear the slot. Safe to call twice."""
    batch = _active.get()
    if batch is None:
        return 0
    written = 0
    try:
        written = batch.flush()
    except Exception:
        # Called from failure handlers too. If the flush itself fails -- most
        # likely because the embedding API is exactly what broke the sync --
        # raising here would replace the real error with this one. The
        # records are already in the graph with their `search_text`, so
        # `scripts/rebuild_vectors.py` can still supply the vectors.
        logger.exception("embedding batch flush failed; run rebuild_vectors to repair")
    finally:
        _active.set(None)
    if batch.embedded:
        logger.info(
            "embedded %d records in %d request(s) instead of %d",
            batch.embedded, batch.requests, batch.embedded,
        )
    return written
