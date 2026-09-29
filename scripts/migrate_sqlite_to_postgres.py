"""Copy the operational SQLite stores into Postgres (one schema per file).

    uv run python -m scripts.migrate_sqlite_to_postgres                 # dry run
    uv run python -m scripts.migrate_sqlite_to_postgres --apply
    uv run python -m scripts.migrate_sqlite_to_postgres --apply --only graphs
    uv run python -m scripts.migrate_sqlite_to_postgres --data-dir /srv/neuron --database-url postgresql://...

Every ``*.sqlite3`` in the data dir (the same ``DATA_DIR`` the backend uses,
``util.paths.DATA_DIR``) is copied into the Postgres schema
``storage.sql_backend.schema_for_path(file)`` -- the schema the app itself
reads once ``NEURON_SQL_BACKEND=postgres`` is set.

Safety properties:

* Dry run by default: it only reads (SQLite opened ``mode=ro``; Postgres only
  SELECTs) and prints per-table row counts on both sides.
* SQLite files are opened read-only and never modified or deleted.
* The ``public`` schema is never touched; every statement is schema-qualified.
* A table that already has rows in Postgres makes the whole file refuse,
  unless ``--replace-existing``, which truncates only that file's target
  tables inside its own schema.
* One transaction per file: rows, sequence resets and the count check commit
  together or not at all.

Target tables are created by instantiating the store class that owns the file
(ConnectorLedger, ConnectorJobStore, OAuthConnectorStore, GraphRegistry,
GitHubStore, NotionStore, runtime settings) against Postgres, so DDL and
column migrations match the app exactly. A store that is not Postgres-aware
yet (it still writes SQLite) is detected, and its tables -- like every table
of an unknown file -- are mirrored generically from ``sqlite_master``.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from storage import sql_backend

BATCH_SIZE = 500

# Same DDL as demo_ui/backend/app.py's _runtime_setting (importing app.py
# would boot the whole API).
RUNTIME_SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS runtime_settings "
    "(name TEXT PRIMARY KEY, value TEXT NOT NULL)"
)


# ------------------------------------------------------------------ owners


def _fernet_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def _owner_ledger(path: Path) -> None:
    from connectors.core.ledger import ConnectorLedger

    ConnectorLedger(path)


def _owner_jobs(path: Path) -> None:
    from connectors.core.jobs import ConnectorJobStore

    ConnectorJobStore(path)


def _owner_oauth(path: Path) -> None:
    from connectors.core.oauth_store import OAuthConnectorStore

    OAuthConnectorStore(path, "jira", _fernet_key())


def _owner_graphs(path: Path) -> None:
    from graph.storage.multigraph import GraphRegistry

    GraphRegistry(path)


def _owner_github(path: Path) -> None:
    from connectors.github_app.store import GitHubStore

    GitHubStore(path)


def _owner_notion(path: Path) -> None:
    from connectors.notion.oauth import NotionStore

    NotionStore(path, _fernet_key())


def _owner_runtime_settings(path: Path) -> None:
    with sql_backend.connect(path) as db:
        db.execute(RUNTIME_SETTINGS_DDL)
    db.close()


def _stem(name: str) -> str:
    return Path(name).name.split(".", 1)[0]


def owner_for(path: Path) -> tuple[str, Callable[[Path], None]] | None:
    stem = _stem(path.name)
    jobs = _stem(os.getenv("CONNECTOR_JOB_DB", "connector_jobs.sqlite3"))
    oauth = _stem(os.getenv("OAUTH_CONNECTOR_STATE_DB", "oauth_connectors.sqlite3"))
    github = _stem(os.getenv("GITHUB_STATE_DB", "") or "github_connector.sqlite3")
    if stem == "connector_ledger" or stem.startswith("connector_ledger__"):
        return "ConnectorLedger", _owner_ledger
    table = {
        jobs: ("ConnectorJobStore", _owner_jobs),
        oauth: ("OAuthConnectorStore", _owner_oauth),
        "graphs": ("GraphRegistry", _owner_graphs),
        github: ("GitHubStore", _owner_github),
        "notion_connector": ("NotionStore", _owner_notion),
        "runtime_settings": ("runtime_settings", _owner_runtime_settings),
    }
    return table.get(stem)


@contextmanager
def _postgres_env(url: str) -> Iterator[None]:
    keys = ("NEURON_SQL_BACKEND", "DATABASE_URL")
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["NEURON_SQL_BACKEND"] = "postgres"
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def bootstrap_with_owner(path: Path, url: str) -> tuple[bool, str]:
    """Run the owning store's constructor against Postgres.

    It is pointed at a throwaway path with the same file name (so the same
    schema) -- a store that still writes SQLite then only creates a scratch
    file there, never touching the real one. Returns (postgres_aware, note).
    """
    owner = owner_for(path)
    if owner is None:
        return False, "unknown file: generic mirror"
    name, build = owner
    with tempfile.TemporaryDirectory(prefix="neuron-migrate-") as scratch:
        scratch_path = Path(scratch) / path.name
        try:
            with _postgres_env(url):
                build(scratch_path)
        except Exception as exc:  # noqa: BLE001 - reported, then generic mirror
            return False, f"{name} bootstrap failed ({type(exc).__name__}: {exc}); generic mirror"
        if scratch_path.exists():
            return False, f"{name} is not Postgres-aware yet (wrote SQLite); generic mirror"
    return True, f"tables created by {name}"


# ---------------------------------------------------------------- inspection


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = None
    return connection


def _q(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sqlite_tables(source: sqlite3.Connection) -> list[tuple[str, str | None]]:
    rows = source.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(str(name), sql) for name, sql in rows]


def sqlite_indexes(source: sqlite3.Connection, table: str) -> list[str]:
    rows = source.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def sqlite_count(source: sqlite3.Connection, table: str) -> int:
    return int(source.execute(f"SELECT COUNT(*) FROM {_q(table)}").fetchone()[0])


def fk_order(source: sqlite3.Connection, tables: list[str]) -> list[str]:
    """Parents before children so FOREIGN KEYs hold during the copy."""
    names = set(tables)
    deps = {
        table: {
            str(row[2]) for row in source.execute(f"PRAGMA foreign_key_list({_q(table)})")
            if str(row[2]) in names and str(row[2]) != table
        }
        for table in tables
    }
    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(table: str) -> None:
        if table in ordered or table in visiting:
            return
        visiting.add(table)
        for parent in sorted(deps[table]):
            visit(parent)
        visiting.discard(table)
        ordered.append(table)

    for table in sorted(tables):
        visit(table)
    return ordered


def pg_schema_exists(pg: Any, schema: str) -> bool:
    return pg.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone() is not None


def pg_columns(pg: Any, schema: str, table: str) -> dict[str, str]:
    rows = pg.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
        (schema, table.lower()),
    ).fetchall()
    return {str(name): str(kind) for name, kind in rows}


def pg_count(pg: Any, schema: str, table: str) -> int | None:
    if not pg_columns(pg, schema, table):
        return None
    return int(pg.execute(f"SELECT COUNT(*) FROM {_q(schema)}.{_q(table.lower())}").fetchone()[0])


# ------------------------------------------------------------ generic DDL

_CREATE_TABLE = re.compile(r"^\s*CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?", re.IGNORECASE)
_CREATE_INDEX = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?", re.IGNORECASE)


def _pg_type(sqlite_type: str) -> str:
    kind = (sqlite_type or "").upper()
    if "INT" in kind:
        return "BIGINT"
    if any(token in kind for token in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in kind:
        return "BYTEA"
    if "NUMERIC" in kind or "DECIMAL" in kind:
        return "NUMERIC"
    return "TEXT"


def _fallback_table_ddl(source: sqlite3.Connection, table: str) -> str:
    """Column names, affinity-mapped types, NOT NULL and the primary key.

    Defaults and CHECKs are dropped (they are SQLite expressions); a lone
    ``INTEGER PRIMARY KEY`` (SQLite's rowid alias) becomes ``BIGSERIAL``."""
    info = list(source.execute(f"PRAGMA table_info({_q(table)})"))
    pk = [str(row[1]) for row in sorted(info, key=lambda r: r[5]) if row[5]]
    rowid_alias = len(pk) == 1 and next(
        str(row[2]).upper() == "INTEGER" for row in info if str(row[1]) == pk[0]
    )
    columns = []
    for row in info:
        name = str(row[1])
        if rowid_alias and name == pk[0]:
            columns.append(f"{_q(name)} BIGSERIAL PRIMARY KEY")
            continue
        columns.append(f"{_q(name)} {_pg_type(str(row[2]))}" + (" NOT NULL" if row[3] else ""))
    if pk and not rowid_alias:
        columns.append("PRIMARY KEY (" + ", ".join(_q(name) for name in pk) + ")")
    return f"CREATE TABLE IF NOT EXISTS {_q(table)} (" + ", ".join(columns) + ")"


def mirror_table(pg: Any, source: sqlite3.Connection, table: str, sql: str | None, notes: list[str]) -> None:
    """Create ``table`` in the current (search_path) schema from SQLite's DDL."""
    statement = None
    if sql:
        statement = sql_backend.translate(_CREATE_TABLE.sub("CREATE TABLE IF NOT EXISTS ", sql, count=1), False)
    if statement:
        try:
            with pg.transaction():
                pg.execute(statement)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{table}: translated DDL failed ({type(exc).__name__}); column-level mirror")
            statement = None
    if statement is None:
        with pg.transaction():
            pg.execute(_fallback_table_ddl(source, table))
    for index_sql in sqlite_indexes(source, table):
        translated = _CREATE_INDEX.sub(
            lambda m: f"CREATE {m.group(1) or ''}INDEX IF NOT EXISTS ", index_sql, count=1
        )
        try:
            with pg.transaction():
                pg.execute(translated)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{table}: index skipped ({type(exc).__name__}: {str(exc).splitlines()[0]})")


# -------------------------------------------------------------------- copy


def _coerce(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type == "bytea":
        return value.encode() if isinstance(value, str) else value
    if pg_type in {"bigint", "integer", "smallint"}:
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return value
        return value
    if pg_type == "boolean":
        return bool(value) if isinstance(value, int) else value
    if pg_type in {"text", "character varying", "character"}:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value if isinstance(value, str) else str(value)
    return value


def copy_table(pg: Any, source: sqlite3.Connection, schema: str, table: str, batch_size: int) -> int:
    target = pg_columns(pg, schema, table)
    source_cols = [str(row[1]) for row in source.execute(f"PRAGMA table_info({_q(table)})")]
    columns = [name for name in source_cols if name.lower() in target]
    if not columns:
        return 0
    kinds = [target[name.lower()] for name in columns]
    insert = (
        f"INSERT INTO {_q(schema)}.{_q(table.lower())} ("
        + ", ".join(_q(name.lower()) for name in columns)
        + ") VALUES (" + ", ".join(["%s"] * len(columns)) + ")"
    )
    cursor = source.execute("SELECT " + ", ".join(_q(name) for name in columns) + f" FROM {_q(table)}")
    copied = 0
    with pg.cursor() as out:
        while batch := cursor.fetchmany(batch_size):
            rows = [tuple(_coerce(v, k) for v, k in zip(row, kinds)) for row in batch]
            out.executemany(insert, rows)
            copied += len(rows)
    return copied


def reset_sequences(pg: Any, schema: str, table: str) -> list[str]:
    rows = pg.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = %s AND column_default LIKE 'nextval(%%'",
        (schema, table.lower()),
    ).fetchall()
    reset = []
    qualified = f"{_q(schema)}.{_q(table.lower())}"
    for (column,) in rows:
        sequence = pg.execute("SELECT pg_get_serial_sequence(%s, %s)", (qualified, column)).fetchone()[0]
        if not sequence:
            continue
        pg.execute(
            f"SELECT setval(%s, COALESCE(MAX({_q(column)}), 1), MAX({_q(column)}) IS NOT NULL) "
            f"FROM {qualified}",
            (sequence,),
        )
        reset.append(f"{table}.{column}")
    return reset


# ---------------------------------------------------------------- driver


@dataclass
class FileResult:
    path: Path
    schema: str
    status: str = "pending"
    tables: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _print_table_counts(result: FileResult, after: dict[str, int | None] | None = None) -> None:
    header = f"   {'table':<32} {'sqlite':>8} {'postgres':>10}"
    if after is not None:
        header += f" {'after':>8}"
    print(header)
    for table, (lite, pgc) in result.tables.items():
        line = f"   {table:<32} {lite:>8} {('absent' if pgc is None else pgc):>10}"
        if after is not None:
            value = after.get(table)
            line += f" {('absent' if value is None else value):>8}"
        print(line)


def migrate_file(
    path: Path,
    *,
    url: str | None,
    apply: bool,
    replace_existing: bool,
    batch_size: int = BATCH_SIZE,
) -> FileResult:
    schema = sql_backend.schema_for_path(path)
    result = FileResult(path=path, schema=schema)
    if schema == "public":
        result.status = "refused: maps to the public schema"
        return result
    owner = owner_for(path)
    print(f"\n== {path.name} -> schema {schema!r} (owner: {owner[0] if owner else 'unknown'})")

    source = open_sqlite_readonly(path)
    try:
        tables = [name for name, _ in sqlite_tables(source)]
        ddl = dict(sqlite_tables(source))
        pg = None
        if url:
            import psycopg

            pg = psycopg.connect(url, autocommit=False)
        try:
            for table in tables:
                before = None
                if pg is not None and pg_schema_exists(pg, schema):
                    before = pg_count(pg, schema, table)
                result.tables[table] = (sqlite_count(source, table), before)
            if pg is not None:
                pg.rollback()
            _print_table_counts(result)
            if not apply:
                result.status = "dry-run" if url else "dry-run (no DATABASE_URL: postgres not inspected)"
                return result
            assert pg is not None and url

            occupied = [t for t, (_, pgc) in result.tables.items() if pgc]
            if occupied and not replace_existing:
                result.status = (
                    "refused: postgres already has rows in " + ", ".join(occupied)
                    + " (pass --replace-existing to overwrite this schema's tables)"
                )
                return result

            aware, note = bootstrap_with_owner(path, url)
            result.notes.append(note)
            sql_backend.close_pool()

            with pg.transaction():
                pg.execute(f"CREATE SCHEMA IF NOT EXISTS {_q(schema)}")
                pg.execute(f"SET LOCAL search_path TO {_q(schema)}")
                for table in tables:
                    if not pg_columns(pg, schema, table):
                        mirror_table(pg, source, table, ddl.get(table), result.notes)
                existing = [t for t in tables if pg_columns(pg, schema, t)]
                if replace_existing and occupied:
                    pg.execute(
                        "TRUNCATE " + ", ".join(f"{_q(schema)}.{_q(t.lower())}" for t in existing)
                    )
                else:
                    # Only rows the owner's constructor just seeded (e.g. the
                    # registry's 'default' graph): the pre-check saw these empty.
                    for table in existing:
                        pg.execute(f"DELETE FROM {_q(schema)}.{_q(table.lower())}")
                for table in fk_order(source, tables):
                    copy_table(pg, source, schema, table, batch_size)
                reset: list[str] = []
                for table in tables:
                    reset += reset_sequences(pg, schema, table)
                after = {t: pg_count(pg, schema, t) for t in tables}
                mismatched = [t for t in tables if after[t] != result.tables[t][0]]
                if mismatched:
                    raise RuntimeError("row counts differ after copy: " + ", ".join(mismatched))
            if reset:
                result.notes.append("sequences reset: " + ", ".join(reset))
            print("   copied:")
            _print_table_counts(result, after)
            result.tables = {t: (result.tables[t][0], after[t]) for t in tables}
            result.status = "copied"
            return result
        except Exception as exc:  # noqa: BLE001 - one file failing must not hide the others
            result.status = f"failed (rolled back): {type(exc).__name__}: {exc}"
            return result
        finally:
            if pg is not None:
                pg.close()
    finally:
        source.close()
        for note in result.notes:
            print(f"   note: {note}")
        print(f"   status: {result.status}")


def discover(data_dir: Path, only: list[str] | None) -> list[Path]:
    files = sorted(p for p in data_dir.glob("*.sqlite3") if p.is_file())
    if only:
        wanted = {_stem(name) for name in only}
        files = [p for p in files if _stem(p.name) in wanted]
    return files


def run(
    data_dir: Path,
    *,
    url: str | None,
    apply: bool = False,
    only: list[str] | None = None,
    replace_existing: bool = False,
    batch_size: int = BATCH_SIZE,
) -> list[FileResult]:
    if apply and not url:
        raise SystemExit("--apply needs a Postgres URL (DATABASE_URL or --database-url)")
    files = discover(data_dir, only)
    mode = "APPLY" if apply else "DRY RUN (no writes; pass --apply to copy)"
    print(f"{mode}: {len(files)} SQLite file(s) in {data_dir}")
    results = [
        migrate_file(path, url=url, apply=apply, replace_existing=replace_existing, batch_size=batch_size)
        for path in files
    ]
    print("\nSummary:")
    for result in results:
        rows = sum(lite for lite, _ in result.tables.values())
        print(f"  {result.path.name:<40} {result.schema:<32} {rows:>7} rows  {result.status}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data-dir", type=Path, default=None, help="default: util.paths.DATA_DIR")
    parser.add_argument("--database-url", default=None, help="default: DATABASE_URL / POSTGRES_URL")
    parser.add_argument("--apply", action="store_true", help="copy rows (default is a dry run)")
    parser.add_argument("--only", action="append", metavar="STEM", help="only this file stem (repeatable)")
    parser.add_argument(
        "--replace-existing", action="store_true",
        help="truncate this file's target tables in its own schema when they already have rows",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)

    from util.paths import DATA_DIR  # also loads .env (DATABASE_URL)

    data_dir = args.data_dir if args.data_dir is not None else DATA_DIR
    url = args.database_url or sql_backend.database_url()
    results = run(
        data_dir, url=url, apply=args.apply, only=args.only,
        replace_existing=args.replace_existing, batch_size=args.batch_size,
    )
    failed = [r for r in results if r.status.startswith(("failed", "refused"))]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
