"""ConnectorLedger core (records, chunks, versions, drops, ontology, settings,
edges, sync coverage) on both SQLite and Postgres via ``storage.sql_backend``."""

from __future__ import annotations

import pytest

from connectors.core.actions import RecordAction
from connectors.core.ledger import (
    ChunkWrite,
    ConnectorLedger,
    DropReason,
    ExtractionDrop,
    RecordEdgeRef,
    SemanticStatus,
)
from storage import sql_backend as backend_module

pytestmark = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


def _ledger(tmp_path) -> ConnectorLedger:
    return ConnectorLedger(tmp_path / "core_ledger.sqlite3")


def test_commit_plan_get(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    key = "jira:c1:issue:ABC-1"
    assert ledger.get(key) is None
    assert ledger.plan(key, "h1") == RecordAction.INSERT

    ledger.commit(key, "h1", primary_node_uid="uid-1")
    entry = ledger.get(key)
    assert entry.content_hash == "h1"
    assert entry.primary_node_uid == "uid-1"
    assert entry.semantic_status == "pending"
    assert entry.semantic_priority == 100
    assert entry.update_count == 0
    assert ledger.plan(key, "h1") == RecordAction.KEEP
    assert ledger.plan(key, "h2") == RecordAction.UPDATE

    ledger.commit(key, "h2", semantic_status=SemanticStatus.DONE)
    entry = ledger.get(key)
    assert (entry.content_hash, entry.update_count, entry.semantic_priority) == ("h2", 1, 200)
    for _ in range(150):  # priority is capped at 300 via min()/LEAST()
        ledger.commit(key, "h2")
    assert ledger.get(key).semantic_priority == 300

    ledger.set_semantic_status(key, SemanticStatus.PENDING)
    assert ledger.pending_semantic_records(10) == [key]
    assert ledger.record_keys_with_prefix("jira:c1:") == [key]
    assert ledger.record_keys_with_prefix("jira:c2:") == []
    assert ledger.count_present([key, "missing"]) == 1

    ledger.commit("jira:c1:issue:ABC-2", "h2")
    moved = ledger.find_moved_from("jira:c1:issue:ABC-2", "h2")
    assert moved is not None and moved.record_key == key

    ledger.commit_delete(key)
    assert ledger.get(key) is None


def test_chunks_roundtrip(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    key = "notion:c1:page:p1"
    ledger.commit(key, "h1")
    diff = ledger.save_chunks(key, [("c1", 0, "alpha"), ChunkWrite("c2", 1, "beta", llm_text="b")])
    assert (diff.kept, diff.added, diff.superseded, diff.reused_done) == (0, 2, 0, 0)

    pending = ledger.pending_chunks(10)
    assert [(p.chunk_id, p.text, p.source_text) for p in pending] == [
        ("c1", "alpha", "alpha"), ("c2", "b", "beta"),
    ]
    assert [p.chunk_id for p in ledger.pending_chunks(10, record_prefix="notion:c1:")] == ["c1", "c2"]
    assert ledger.pending_chunks(10, record_prefix="notion:c9:") == []

    ledger.commit_chunk(key, "c1")
    assert ledger.chunk_status(key, "c1") == "done"
    assert ledger.record_fully_processed(key) is False
    ledger.commit_chunk(key, "c2")
    assert ledger.record_fully_processed(key) is True

    diff = ledger.save_chunks(key, [("c1", 0, "alpha"), ("c3", 1, "gamma")])
    assert (diff.kept, diff.added, diff.superseded, diff.reused_done) == (1, 1, 1, 1)
    assert [p.chunk_id for p in ledger.pending_chunks(10)] == ["c3"]

    ledger.record_triage(key, "c3", "noise", 0.1, "m")
    ledger.record_triage_yield(key, "c3", 2)
    assert ledger.triage_report() == {"scored": 1, "would_skip": 1, "lost_facts": 2}


def test_versions(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.record_version("k", "a") == 1
    assert ledger.record_version("k", "a") == 1
    assert ledger.record_version("k", "b") == 2
    assert [(v, h) for v, h, _ in ledger.versions("k")] == [(1, "a"), (2, "b")]


def test_drops(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    drops = [
        ExtractionDrop(DropReason.RELATION_NOT_ALLOWED, "Document", "Doc A", "DEFINES", "System", "S1"),
        ExtractionDrop(DropReason.ENDPOINT_UNRESOLVED, detail="x"),
    ]
    ledger.record_drops("gh:c1:file:a", "ch1", drops)
    ledger.record_drops("gh:c1:file:a", "ch1", drops)  # rewritten, not appended
    ledger.record_drops("gh:c2:file:b", "ch2", drops[:1])
    assert ledger.drop_counts() == {"relation_not_allowed": 2, "endpoint_unresolved": 1}
    assert ledger.drop_counts("gh:c1:") == {"relation_not_allowed": 1, "endpoint_unresolved": 1}
    assert len(ledger.drops(DropReason.RELATION_NOT_ALLOWED)) == 2


def test_axioms_misses_adoptions(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    row = {
        "relation": "OWNS", "subject_kind": "Person", "object_kind": "System",
        "extractable": True, "functional": False, "is_transitive": False,
        "is_symmetric": False, "is_asymmetric": True, "inverse_of": None,
        "sub_property_of": None, "temporal": "state",
    }
    assert ledger.seed_axioms_if_empty([row]) == 1
    assert ledger.seed_axioms_if_empty([row]) == 0
    [stored] = ledger.axiom_rows()
    assert stored["extractable"] == 1 and stored["adopted_batch"] is None

    ledger.record_miss("relation_type", "Doc -X-> Sys", "ex1")
    ledger.record_miss("relation_type", "Doc -X-> Sys", "ex2")
    ledger.record_miss("relation_type", "b-lower", None)
    assert ledger.misses() == [
        ("relation_type", "Doc -X-> Sys", 2, "ex1"), ("relation_type", "b-lower", 1, None),
    ]
    ledger.dismiss_miss("relation_type", "b-lower")
    assert len(ledger.misses()) == 1 and len(ledger.misses(include_dismissed=True)) == 2

    drop = ExtractionDrop(DropReason.RELATION_NOT_ALLOWED, "Document", "Doc A", "DEFINES", "System", "S1")
    ledger.record_drops("gh:c1:file:a", "ch1", [drop])
    ledger.record_drops("gh:c1:file:b", "ch1", [drop])
    ledger.commit("gh:c1:file:a", "h")
    ledger.save_chunks("gh:c1:file:a", [("ch1", 0, "t")])
    ledger.commit_chunk("gh:c1:file:a", "ch1")

    [candidate] = ledger.adoption_candidates(min_docs=2)
    assert candidate == {
        "subject_kind": "Document", "relation": "DEFINES", "object_kind": "System",
        "facts": 2, "docs": 2, "example": "Doc A -> S1",
    }
    assert ledger.adoption_candidates(min_docs=3) == []

    batch = ledger.adopt_shapes([candidate], min_docs=2)
    assert len(ledger.axiom_rows()) == 2
    [adoption] = ledger.adoptions()
    assert adoption["batch_id"] == batch and adoption["shapes"] == [candidate]
    assert ledger.requeue_chunks_for_shapes([candidate]) == 1
    assert ledger.chunk_status("gh:c1:file:a", "ch1") == "pending"

    assert ledger.unadopt(batch) == [candidate]
    assert ledger.unadopt(batch) == []
    assert len(ledger.axiom_rows()) == 1
    assert ledger.adoptions() == [] and len(ledger.adoptions(include_undone=True)) == 1


def test_settings(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    assert ledger.setting("mode", "dflt") == "dflt"
    ledger.set_setting("mode", "a")
    ledger.set_setting("mode", "b")
    assert ledger.setting("mode") == "b"


def test_record_edges(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_edge("k", "OWNS", "u1", "u2")
    ledger.record_edges_batch("k", [RecordEdgeRef("OWNS", "u1", "u2"), RecordEdgeRef("USES", "u1", "u3")])
    assert sorted(ledger.edges_for_record("k"), key=lambda e: e.rel_type) == [
        RecordEdgeRef("OWNS", "u1", "u2"), RecordEdgeRef("USES", "u1", "u3"),
    ]
    ledger.clear_edges("k")
    assert ledger.edges_for_record("k") == []


def test_sync_coverage(sql_backend, tmp_path):
    ledger = _ledger(tmp_path)
    ledger.record_sync_coverage("r1", "jira", fetched_count=3, ledger_count=3)
    ledger.record_sync_coverage(
        "r2", "jira", connection_id="c1", provider_reported_total=10, fetched_count=4,
        ledger_count=4, skipped_by_rule_count=1, extension_filtered_count=2, commits_capped=True,
    )
    ledger.record_sync_coverage("r3", "github", fetched_count=1, ledger_count=1)
    latest = ledger.latest_sync_coverage()
    assert [(row.provider, row.run_id) for row in latest] == [("github", "r3"), ("jira", "r2")]
    [jira] = ledger.latest_sync_coverage("jira")
    assert jira.commits_capped is True and jira.extension_filtered_count == 2
    assert jira.provider_reported_total == 10 and jira.connection_id == "c1"
    first = ledger.sync_coverage_for_run("r1")
    assert first.commits_capped is False and first.provider_reported_total is None
    assert ledger.sync_coverage_for_run("nope") is None


def test_reopen_is_idempotent(sql_backend, tmp_path):
    path = tmp_path / "core_ledger.sqlite3"
    ledger = ConnectorLedger(path)
    ledger.commit("k", "h")
    ledger.save_chunks("k", [("c1", 0, "t")])
    reopened = ConnectorLedger(path)
    assert reopened.get("k").content_hash == "h"
    assert reopened.chunk_status("k", "c1") == "pending"
    with backend_module.connect(path) as connection:
        stoplist = connection.execute("SELECT COUNT(*) AS n FROM mention_stoplist").fetchone()["n"]
        columns = backend_module.table_columns(connection, "source_chunks")
    assert int(stoplist) == 10  # seed inserted once, not per open
    assert {"superseded_at", "llm_text", "triage_facts_written"} <= columns


def test_reopen_migrates_legacy_tables(sql_backend, tmp_path):
    path = tmp_path / "legacy_core.sqlite3"
    with backend_module.connect(path) as connection:
        connection.execute(
            "CREATE TABLE source_records (record_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
            "primary_node_uid TEXT, semantic_status TEXT NOT NULL DEFAULT 'pending', "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE source_chunks (record_key TEXT NOT NULL, chunk_id TEXT NOT NULL, "
            "chunk_index INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL DEFAULT '', "
            "status TEXT NOT NULL DEFAULT 'pending', committed_at TEXT NOT NULL, "
            "PRIMARY KEY(record_key, chunk_id))"
        )
        connection.execute(
            "INSERT INTO source_records(record_key, content_hash, updated_at) VALUES ('old', 'h0', 'x')"
        )
    ledger = ConnectorLedger(path)
    entry = ledger.get("old")
    assert (entry.semantic_priority, entry.update_count) == (100, 0)
    ledger.save_chunks("old", [ChunkWrite("c1", 0, "t", llm_text="L")])
    assert ledger.pending_chunks(5)[0].text == "L"
    ConnectorLedger(path)  # second migration pass is a no-op
    assert ledger.get("old").content_hash == "h0"
