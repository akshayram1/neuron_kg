"""scripts/migrate_sqlite_to_postgres.py against a real Postgres (skips without one)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid

import pytest

from connectors.core.ledger import ChunkWrite, ConnectorLedger
from graph.multigraph import GraphRegistry
from scripts import migrate_sqlite_to_postgres as migrate
from storage import sql_backend

URL = os.getenv("NEURON_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="NEURON_TEST_DATABASE_URL not set")


@pytest.fixture
def pg_prefix(monkeypatch):
    import psycopg

    try:
        psycopg.connect(URL, connect_timeout=2).close()
    except Exception as exc:  # pragma: no cover - environment
        pytest.skip(f"Postgres unreachable: {exc}")
    from tests.conftest import _drop_prefixed_schemas

    prefix = f"t{uuid.uuid4().hex[:10]}_"
    # Source stores are built on SQLite; the script targets Postgres itself.
    monkeypatch.setenv("NEURON_SQL_BACKEND", "sqlite")
    monkeypatch.setenv("NEURON_SQL_SCHEMA_PREFIX", prefix)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield prefix
    _drop_prefixed_schemas(URL, prefix)


def _pg(sql, params=None):
    import psycopg

    with psycopg.connect(URL, autocommit=True) as connection:
        return connection.execute(sql, params).fetchall()


def _schemas(prefix):
    return sorted(r[0] for r in _pg("SELECT nspname FROM pg_namespace WHERE nspname LIKE %s", (prefix + "%",)))


def _public_tables():
    return _pg(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1"
    )


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def data_dir(tmp_path, pg_prefix):
    ledger = ConnectorLedger(tmp_path / "connector_ledger.sqlite3")
    for index in range(3):
        ledger.commit(f"jira:c1:ISSUE-{index}", f"hash{index}", primary_node_uid=f"uid{index}")
    ledger.save_chunks("jira:c1:ISSUE-0", [ChunkWrite("chunk-a", 0, "hello"), ChunkWrite("chunk-b", 1, "world")])
    for index in range(2):
        ledger.create_review("merge", {"n": index}, identity=f"merge:{index}")

    GraphRegistry(tmp_path / "graphs.sqlite3").create("alpha", "Alpha")

    with sqlite3.connect(tmp_path / "misc_store.sqlite3") as db:
        db.execute(
            "CREATE TABLE things (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, "
            "weight REAL, payload BLOB, created DATETIME DEFAULT (datetime('now')))"
        )
        db.execute("CREATE INDEX things_label ON things(label)")
        db.executemany(
            "INSERT INTO things(label, weight, payload) VALUES (?, ?, ?)",
            [("a", 1.5, b"\x00\x01"), ("b", None, None), ("c", 3.0, b"z")],
        )
    return tmp_path


def _by_name(results):
    return {r.path.name: r for r in results}


def test_dry_run_apply_and_refuse(data_dir, pg_prefix):
    digests = {p.name: _digest(p) for p in data_dir.glob("*.sqlite3")}
    public_before = _public_tables()

    dry = _by_name(migrate.run(data_dir, url=URL, apply=False))
    assert set(dry) == {"connector_ledger.sqlite3", "graphs.sqlite3", "misc_store.sqlite3"}
    assert all(r.status == "dry-run" for r in dry.values())
    assert dry["connector_ledger.sqlite3"].tables["source_records"] == (3, None)
    assert _schemas(pg_prefix) == []  # a dry run writes nothing

    applied = _by_name(migrate.run(data_dir, url=URL, apply=True))
    assert all(r.status == "copied" for r in applied.values()), {k: v.status for k, v in applied.items()}
    for result in applied.values():
        for table, (lite, pgc) in result.tables.items():
            assert lite == pgc, (result.path.name, table)
    assert applied["graphs.sqlite3"].tables["graphs"] == (2, 2)
    assert applied["connector_ledger.sqlite3"].tables["source_chunks"] == (2, 2)
    assert applied["misc_store.sqlite3"].tables["things"] == (3, 3)
    assert _schemas(pg_prefix) == sorted(
        f"{pg_prefix}{name}" for name in ("connector_ledger", "graphs", "misc_store")
    )

    ledger_schema = f"{pg_prefix}connector_ledger"
    new_id = _pg(
        f'INSERT INTO "{ledger_schema}".reviews(type, payload, state, created_at) '
        "VALUES ('merge', '{}', 'pending', 'now') RETURNING id"
    )[0][0]
    assert new_id == 3  # sequence reset past the copied ids 1..2

    misc = f"{pg_prefix}misc_store"
    rows = _pg(f'SELECT id, label, weight, payload FROM "{misc}".things ORDER BY id')
    assert [(r[0], r[1], r[2], bytes(r[3]) if r[3] is not None else None) for r in rows] == [
        (1, "a", 1.5, b"\x00\x01"), (2, "b", None, None), (3, "c", 3.0, b"z"),
    ]
    assert _pg(f'INSERT INTO "{misc}".things(label) VALUES (%s) RETURNING id', ("d",))[0][0] == 4
    indexes = {r[0] for r in _pg("SELECT indexname FROM pg_indexes WHERE schemaname = %s", (misc,))}
    assert "things_label" in indexes

    # The app reads the copied rows through the shim.
    os.environ["NEURON_SQL_BACKEND"] = "postgres"
    os.environ["DATABASE_URL"] = URL
    try:
        names = [g["name"] for g in GraphRegistry(data_dir / "graphs.sqlite3").list()]
    finally:
        os.environ["NEURON_SQL_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)
        sql_backend.close_pool()
    assert sorted(names) == ["alpha", "default"]

    again = _by_name(migrate.run(data_dir, url=URL, apply=True))
    assert all(r.status.startswith("refused") for r in again.values())
    assert _pg(f'SELECT COUNT(*) FROM "{misc}".things')[0][0] == 4  # untouched by the refusal

    replaced = _by_name(migrate.run(data_dir, url=URL, apply=True, replace_existing=True))
    assert all(r.status == "copied" for r in replaced.values()), {k: v.status for k, v in replaced.items()}
    assert _pg(f'SELECT COUNT(*) FROM "{misc}".things')[0][0] == 3
    assert _pg(f'SELECT COUNT(*) FROM "{ledger_schema}".reviews')[0][0] == 2

    only = migrate.run(data_dir, url=URL, apply=False, only=["graphs"])
    assert [r.path.name for r in only] == ["graphs.sqlite3"]

    assert {p.name: _digest(p) for p in data_dir.glob("*.sqlite3")} == digests
    assert _public_tables() == public_before
