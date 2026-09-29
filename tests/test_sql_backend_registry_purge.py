"""GraphRegistry and purge_ledger_prefix on both SQL backends."""

from __future__ import annotations

import pytest

from connectors.core.purge import purge_ledger_prefix
from graph.storage.multigraph import DEFAULT_GRAPH_NAME, GraphRegistry
from storage import sql_backend

BACKENDS = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


@BACKENDS
def test_graph_registry_round_trip(sql_backend, tmp_path):
    registry = GraphRegistry(tmp_path / "graphs.sqlite3")
    # Re-opening must not duplicate the seeded default row (ON CONFLICT DO NOTHING).
    registry = GraphRegistry(tmp_path / "graphs.sqlite3")
    assert [g["name"] for g in registry.list()] == [DEFAULT_GRAPH_NAME]

    registry.create("alpha", "Alpha")
    assert registry.exists("alpha")
    with pytest.raises(ValueError):
        registry.create("alpha")
    names = {g["name"]: g["displayName"] for g in registry.list()}
    assert names == {"default": "Default", "alpha": "Alpha"}

    assert registry.delete("alpha") is True
    assert registry.delete("alpha") is False
    with pytest.raises(ValueError):
        registry.delete(DEFAULT_GRAPH_NAME)
    assert not registry.exists("alpha")


def _seed_ledger(path):
    db = sql_backend.connect(path)
    with db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_records (
                record_key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_chunks (
                record_key TEXT NOT NULL, chunk_id TEXT NOT NULL, text TEXT,
                PRIMARY KEY(record_key, chunk_id)
            );
            """
        )
        keys = [
            "jira:c1:A", "jira:c1:B", "jira:c10:X", "jira:c2:A",
            "bitbucket:c_1:a", "bitbucket:cX1:a",
        ]
        for key in keys:
            db.execute(
                "INSERT INTO source_records(record_key, content_hash, updated_at) VALUES (?, 'h', 't')",
                (key,),
            )
            db.execute(
                "INSERT INTO source_chunks(record_key, chunk_id, text) VALUES (?, 'k0', 'x')", (key,),
            )
    db.close()


def _keys(path, table):
    db = sql_backend.connect(path)
    try:
        return sorted(row[0] for row in db.execute(f"SELECT record_key FROM {table}"))
    finally:
        db.close()


@BACKENDS
def test_purge_ledger_prefix_is_scoped(sql_backend, tmp_path):
    path = tmp_path / "connector_ledger.sqlite3"
    _seed_ledger(path)

    assert purge_ledger_prefix(path, "jira:c1:") == 2
    expected = sorted(["bitbucket:c_1:a", "bitbucket:cX1:a", "jira:c10:X", "jira:c2:A"])
    assert _keys(path, "source_records") == expected
    assert _keys(path, "source_chunks") == expected

    # '_' is literal, not a single-character wildcard.
    assert purge_ledger_prefix(path, "bitbucket:c_1:") == 1
    assert _keys(path, "source_records") == ["bitbucket:cX1:a", "jira:c10:X", "jira:c2:A"]

    assert purge_ledger_prefix(path, "notion:") == 0
    with pytest.raises(ValueError):
        purge_ledger_prefix(path, "")


@BACKENDS
def test_purge_ledger_prefix_missing_store(sql_backend, tmp_path):
    assert purge_ledger_prefix(tmp_path / "connector_ledger__nothing.sqlite3", "jira:c1:") == 0
