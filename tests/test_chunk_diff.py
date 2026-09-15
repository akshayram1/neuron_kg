"""Incremental re-ingestion: `save_chunks` must reuse unchanged chunks.

The behaviour being pinned is a cost property, not a cosmetic one. Before
this, one edited paragraph in a Notion page deleted every chunk of that page
and re-queued all of them, so the whole document went back through the LLM.
"""

from __future__ import annotations

from connectors.core.ledger import ConnectorLedger, SemanticStatus


def _ledger(tmp_path) -> ConnectorLedger:
    return ConnectorLedger(tmp_path / "ledger.sqlite3")


KEY = "notion:conn-1:page:page-a"


def test_unchanged_chunks_keep_their_done_status(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    ledger.save_chunks(KEY, [("c1", 0, "alpha"), ("c2", 1, "beta")])
    ledger.commit_chunk(KEY, "c1", SemanticStatus.DONE)
    ledger.commit_chunk(KEY, "c2", SemanticStatus.DONE)

    # Second version: `beta` edited, `alpha` untouched, one paragraph appended.
    diff = ledger.save_chunks(KEY, [("c1", 0, "alpha"), ("c3", 1, "beta edited"), ("c4", 2, "gamma")])

    assert diff.kept == 1 and diff.reused_done == 1
    assert diff.added == 2
    assert diff.superseded == 1
    # The already-extracted chunk is NOT queued again; only the new text is.
    assert sorted(c.chunk_id for c in ledger.pending_chunks(10)) == ["c3", "c4"]
    assert ledger.chunk_status(KEY, "c1") == "done"


def test_reingesting_identical_content_queues_nothing(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    chunks = [("c1", 0, "alpha"), ("c2", 1, "beta")]
    ledger.save_chunks(KEY, chunks)
    ledger.commit_chunk(KEY, "c1", SemanticStatus.DONE)
    ledger.commit_chunk(KEY, "c2", SemanticStatus.DONE)

    diff = ledger.save_chunks(KEY, chunks)

    assert (diff.kept, diff.added, diff.superseded) == (2, 0, 0)
    assert diff.reused_done == 2
    assert ledger.pending_chunks(10) == []


def test_superseded_chunks_are_kept_not_deleted(tmp_path):
    """A fact's evidence must survive the chunk it came from going stale."""
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    ledger.save_chunks(KEY, [("c1", 0, "alpha")])
    ledger.commit_chunk(KEY, "c1", SemanticStatus.DONE)

    ledger.save_chunks(KEY, [("c2", 0, "replacement")])

    # Still on disk, still readable, just no longer live.
    assert ledger.chunk_status(KEY, "c1") == "done"
    assert [c.chunk_id for c in ledger.pending_chunks(10)] == ["c2"]


def test_a_superseded_pending_chunk_cannot_block_completion(tmp_path):
    """Regression: superseded rows kept their 'pending' status, so a record
    whose text changed before extraction caught up would never flip to DONE."""
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    ledger.save_chunks(KEY, [("c1", 0, "alpha")])          # never extracted
    ledger.save_chunks(KEY, [("c2", 0, "replacement")])     # c1 superseded while pending
    ledger.commit_chunk(KEY, "c2", SemanticStatus.DONE)

    assert ledger.record_fully_processed(KEY) is True


def test_chunk_index_updates_even_when_text_is_unchanged(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    ledger.save_chunks(KEY, [("c1", 0, "alpha")])
    # A paragraph inserted above shifts position but not identity.
    ledger.save_chunks(KEY, [("c9", 0, "new intro"), ("c1", 1, "alpha")])

    pending = {c.chunk_id: c.chunk_index for c in ledger.pending_chunks(10)}
    assert pending["c1"] == 1


def test_versions_are_appended_only_when_content_changes(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")
    ledger.commit(KEY, "hash-1", primary_node_uid="uid-1")   # same bytes re-synced
    ledger.commit(KEY, "hash-2", primary_node_uid="uid-1")

    versions = ledger.versions(KEY)
    assert [(v, h) for v, h, _at in versions] == [(1, "hash-1"), (2, "hash-2")]


def test_find_moved_from_spots_a_rename_within_one_connection(tmp_path):
    ledger = _ledger(tmp_path)
    old_key = "bitbucket:conn-1:source_file:{repo}:src/old_name.py"
    new_key = "bitbucket:conn-1:source_file:{repo}:src/new_name.py"
    ledger.commit(old_key, "same-bytes", primary_node_uid="uid-old")

    moved = ledger.find_moved_from(new_key, "same-bytes")
    assert moved is not None and moved.record_key == old_key
    assert moved.primary_node_uid == "uid-old"


def test_a_renamed_record_does_not_re_extract_identical_text(tmp_path):
    """chunk_id is namespaced by record_key, so a rename changes every id even
    when the text is byte-identical. Without adoption that is a full
    re-extraction of an unchanged document."""
    ledger = _ledger(tmp_path)
    old_key = "notion:conn-1:page:old-id"
    new_key = "notion:conn-1:page:new-id"
    ledger.commit(old_key, "same-bytes", primary_node_uid="uid-old")
    ledger.save_chunks(old_key, [("old-c1", 0, "alpha"), ("old-c2", 1, "beta")])
    ledger.commit_chunk(old_key, "old-c1", SemanticStatus.DONE)
    ledger.commit_chunk(old_key, "old-c2", SemanticStatus.DONE)

    moved = ledger.find_moved_from(new_key, "same-bytes")
    assert moved is not None
    diff = ledger.save_chunks(
        new_key, [("new-c1", 0, "alpha"), ("new-c2", 1, "beta")],
        adopt_from=moved.record_key,
    )
    # `pending_chunks` joins `source_records`, so the record must exist for
    # the queue assertions below to mean anything.
    ledger.commit(new_key, "same-bytes", primary_node_uid="uid-new")

    assert diff.added == 2 and diff.reused_done == 2
    assert ledger.pending_chunks(10) == []          # nothing queued for the LLM
    assert ledger.record_fully_processed(new_key) is True


def test_adoption_only_covers_text_that_actually_matches(tmp_path):
    ledger = _ledger(tmp_path)
    old_key, new_key = "notion:conn-1:page:old", "notion:conn-1:page:new"
    ledger.commit(old_key, "h", primary_node_uid="uid-old")
    ledger.save_chunks(old_key, [("old-c1", 0, "alpha")])
    ledger.commit_chunk(old_key, "old-c1", SemanticStatus.DONE)

    diff = ledger.save_chunks(
        new_key, [("new-c1", 0, "alpha"), ("new-c2", 1, "genuinely new")],
        adopt_from=old_key,
    )
    ledger.commit(new_key, "h2", primary_node_uid="uid-new")

    assert diff.reused_done == 1
    assert [c.chunk_id for c in ledger.pending_chunks(10)] == ["new-c2"]


def test_find_moved_from_does_not_cross_connections(tmp_path):
    """Identical content under a different account is a different record, not
    a rename -- treating it as one would hand another tenant's node id over."""
    ledger = _ledger(tmp_path)
    ledger.commit("bitbucket:conn-OTHER:source_file:{repo}:a.py", "same-bytes",
                  primary_node_uid="uid-other")

    assert ledger.find_moved_from(
        "bitbucket:conn-1:source_file:{repo}:a.py", "same-bytes"
    ) is None
