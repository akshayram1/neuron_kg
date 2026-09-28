from __future__ import annotations

import pytest

from storage import sql_backend
from storage.sql_backend import IntegrityError, connect, is_postgres, table_columns, translate

BOTH = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


def test_translate_placeholders_skip_string_literals():
    assert translate("SELECT '?', x FROM t WHERE a = ? AND b LIKE 'x%'", True) == (
        "SELECT '?', x FROM t WHERE a = %s AND b LIKE 'x%%'"
    )
    assert translate("SELECT a % 2 FROM t WHERE a = ?", True) == "SELECT a %% 2 FROM t WHERE a = %s"
    assert translate("SELECT a % 2 FROM t", False) == "SELECT a % 2 FROM t"
    assert translate("SELECT 1 FROM t WHERE b LIKE 'x%' AND a = ?", True) == (
        "SELECT 1 FROM t WHERE b LIKE 'x%%' AND a = %s"
    )


def test_translate_ddl_types():
    sql = translate(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, n INTEGER, s REAL, b BLOB)", False
    )
    assert "BIGSERIAL PRIMARY KEY" in sql
    assert "n BIGINT" in sql and "DOUBLE PRECISION" in sql and "BYTEA" in sql
    # DML is never type-rewritten.
    assert translate("SELECT CAST(x AS INTEGER) FROM t", False) == "SELECT CAST(x AS INTEGER) FROM t"


def test_split_script_respects_quotes():
    assert sql_backend.split_script("CREATE TABLE a(x TEXT DEFAULT ';'); CREATE INDEX i ON a(x);") == [
        "CREATE TABLE a(x TEXT DEFAULT ';')",
        "CREATE INDEX i ON a(x)",
    ]


def test_schema_for_path(monkeypatch):
    monkeypatch.delenv("NEURON_SQL_SCHEMA_PREFIX", raising=False)
    assert sql_backend.schema_for_path("/x/connector_ledger__nilus.sqlite3") == "connector_ledger__nilus"
    assert sql_backend.schema_for_path("data/Graphs-1.sqlite3") == "graphs_1"


@BOTH
def test_roundtrip_rows_returning_and_upsert(tmp_path, sql_backend):
    with connect(tmp_path / "store.sqlite3") as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                n INTEGER NOT NULL DEFAULT 0,
                score REAL,
                flag INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS items_n ON items(n);
            """
        )
        first = db.execute(
            "INSERT INTO items(key, n, score, flag) VALUES (?, ?, ?, ?) RETURNING id",
            ("a", 1, 0.5, True),
        ).fetchone()
        db.execute(
            "INSERT INTO items(key, n) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET n = items.n + excluded.n",
            ("a", 2),
        )
        db.execute("INSERT INTO items(key) VALUES (?) ON CONFLICT DO NOTHING", ("a",))
    assert first[0] == 1

    db = connect(tmp_path / "store.sqlite3")
    row = db.execute("SELECT id, key, n, score, flag FROM items WHERE key = ?", ("a",)).fetchone()
    assert (row["key"], row["n"], row["score"], row["flag"]) == ("a", 3, 0.5, 1)
    assert dict(row)["key"] == "a" and row[1] == "a"
    assert {"id", "key", "n", "score", "flag"} <= table_columns(db, "items")
    assert is_postgres(db) is (sql_backend == "postgres")
    db.close()


@BOTH
def test_integrity_error_leaves_transaction_usable(tmp_path, sql_backend):
    with connect(tmp_path / "s.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS u (k TEXT PRIMARY KEY)")
        db.execute("INSERT INTO u VALUES (?)", ("x",))
        with pytest.raises(IntegrityError):
            db.execute("INSERT INTO u VALUES (?)", ("x",))
        db.execute("INSERT INTO u VALUES (?)", ("y",))
    with connect(tmp_path / "s.sqlite3") as db:
        assert [r[0] for r in db.execute("SELECT k FROM u ORDER BY k")] == ["x", "y"]


@BOTH
def test_rollback_on_exception_and_reuse_after_with(tmp_path, sql_backend):
    db = connect(tmp_path / "r.sqlite3")
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT)")
    with pytest.raises(RuntimeError):
        with db:
            db.execute("INSERT INTO t VALUES (?)", ("lost",))
            raise RuntimeError("boom")
    with db:
        db.execute("INSERT INTO t VALUES (?)", ("kept",))
    assert [r[0] for r in db.execute("SELECT k FROM t")] == ["kept"]
    db.close()


@BOTH
def test_separate_files_are_separate_schemas(tmp_path, sql_backend):
    for name in ("one", "two"):
        with connect(tmp_path / f"{name}.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT)")
            db.execute("INSERT INTO t VALUES (?)", (name,))
    with connect(tmp_path / "one.sqlite3") as db:
        assert [r[0] for r in db.execute("SELECT k FROM t")] == ["one"]


@BOTH
def test_percent_literal_with_params(tmp_path, sql_backend):
    with connect(tmp_path / "p.sqlite3") as db:
        db.execute("CREATE TABLE IF NOT EXISTS t (k TEXT, n INTEGER)")
        db.execute("INSERT INTO t VALUES (?, ?)", ("abc", 1))
        rows = db.execute("SELECT k FROM t WHERE k LIKE 'ab%' AND n = ?", (1,)).fetchall()
    assert [r[0] for r in rows] == ["abc"]
