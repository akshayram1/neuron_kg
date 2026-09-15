"""Write-time embedding batching.

What this protects is a throughput property, measured rather than assumed:
a full `write_file` takes ~740 ms on an idle machine (~81 records/min), yet a
real 659-record Bitbucket sync ran at 3-5 records/min in bursts separated by
long stalls. The work per record had not changed; the number of API REQUESTS
had. Batching cuts request count, which is what rate limiting counts.
"""

from __future__ import annotations

from dataclasses import dataclass

import graph.embed_batch as eb


@dataclass
class _FakeEmbedding:
    embedding: list[float]


class _FakeResponse:
    def __init__(self, n):
        self.data = [_FakeEmbedding([float(i)]) for i in range(n)]


class _FakeEmbeddings:
    def __init__(self, parent):
        self.parent = parent

    def create(self, model, input):
        self.parent.calls.append(list(input))
        return _FakeResponse(len(input))


class FakeOpenAI:
    """Counts requests. The point of the batch is the request count."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.embeddings = _FakeEmbeddings(self)


def _capture_upserts(monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(eb.vector_store, "client", lambda: object())
    monkeypatch.setattr(
        eb.vector_store, "upsert_vectors",
        lambda _client, rows, collection=None: written.extend(rows),
    )
    return written


def test_one_request_carries_the_whole_batch(monkeypatch):
    written = _capture_upserts(monkeypatch)
    client = FakeOpenAI()
    with eb.batch_embeddings(client, "m", "coll", max_records=8) as batch:
        for i in range(8):
            batch.add(f"uid{i}", "SourceFile", f"content {i}", f"name{i}")

    assert len(client.calls) == 1, "8 records must not cost 8 requests"
    # content inputs first, then names, so the halves pair up by position.
    assert client.calls[0][:8] == [f"content {i}" for i in range(8)]
    assert client.calls[0][8:] == [f"name{i}" for i in range(8)]
    assert len(written) == 8


def test_content_and_name_vectors_pair_up_by_position(monkeypatch):
    written = _capture_upserts(monkeypatch)
    client = FakeOpenAI()
    with eb.batch_embeddings(client, "m", "coll", max_records=4) as batch:
        batch.add("a", "SourceFile", "content-a", "name-a")
        batch.add("b", "SourceFile", "content-b", "name-b")

    first = next(row for row in written if row["uid"] == "a")
    # data[0] is content-a, data[2] is name-a (2 records => offset 2).
    assert first["embedding"] == [0.0]
    assert first["name_embedding"] == [2.0]


def test_a_partial_batch_is_flushed_on_exit(monkeypatch):
    """The last few records of a sync must not be silently dropped."""
    written = _capture_upserts(monkeypatch)
    client = FakeOpenAI()
    with eb.batch_embeddings(client, "m", "coll", max_records=64) as batch:
        batch.add("only", "Commit", "text", "name")
        assert client.calls == []          # still buffered

    assert len(client.calls) == 1
    assert [row["uid"] for row in written] == ["only"]


def test_the_batch_flushes_itself_when_full(monkeypatch):
    _capture_upserts(monkeypatch)
    client = FakeOpenAI()
    with eb.batch_embeddings(client, "m", "coll", max_records=2) as batch:
        for i in range(5):
            batch.add(f"u{i}", "SourceFile", f"c{i}", f"n{i}")
    assert len(client.calls) == 3          # 2 + 2 + 1


def test_a_token_heavy_batch_flushes_early(monkeypatch):
    """A handful of large files must not be packed into one oversized
    request just because the record count is low."""
    _capture_upserts(monkeypatch)
    client = FakeOpenAI()
    with eb.batch_embeddings(client, "m", "coll", max_records=64, token_budget=1_000) as batch:
        batch.add("a", "SourceFile", "x" * 8_000, "a")   # ~2k tokens
        batch.add("b", "SourceFile", "x" * 8_000, "b")   # crosses the budget
    assert len(client.calls) >= 2


def test_no_batch_open_means_no_batching(monkeypatch):
    assert eb.active_batch() is None


def test_the_batch_does_not_leak_out_of_its_scope(monkeypatch):
    _capture_upserts(monkeypatch)
    with eb.batch_embeddings(FakeOpenAI(), "m", "coll"):
        assert eb.active_batch() is not None
    assert eb.active_batch() is None


def test_close_batch_never_masks_the_error_that_broke_the_sync(monkeypatch, caplog):
    """close_batch() runs inside failure handlers. If the embedding API is
    what failed, re-raising here would replace the real error with this one."""
    class Exploding(FakeOpenAI):
        def __init__(self):
            super().__init__()
            self.embeddings = self
        def create(self, model, input):
            raise RuntimeError("embeddings API down")

    _capture_upserts(monkeypatch)
    batch = eb.open_batch(Exploding(), "m", "coll", max_records=64)
    batch.add("a", "SourceFile", "c", "n")

    assert eb.close_batch() == 0           # swallowed, not raised
    assert eb.active_batch() is None       # and the slot is still cleared
