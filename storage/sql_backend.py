"""One connection factory for the small operational stores (ledger, jobs, oauth, registry).

Every such store was written against SQLite and is constructed with a file
path. ``connect(path)`` keeps that contract and returns either:

* a plain ``sqlite3.Connection`` (``NEURON_SQL_BACKEND=sqlite``, the default), or
* a ``PgConnection`` that speaks the same small DB-API subset against Postgres
  (``NEURON_SQL_BACKEND=postgres`` plus ``DATABASE_URL``).

Each SQLite file maps to its own Postgres *schema* named after the file stem
(``connector_ledger__nilus.sqlite3`` -> schema ``connector_ledger__nilus``), so
store SQL keeps unqualified table names and never collides with the
graph-scoped tables ``storage/postgres.py`` owns in ``public``.

The shim only translates what cannot be written portably:

* ``?`` placeholders -> ``%s`` (and literal ``%`` -> ``%%`` when params exist)
* DDL types: ``INTEGER PRIMARY KEY [AUTOINCREMENT]`` -> ``BIGSERIAL PRIMARY KEY``,
  ``INTEGER`` -> ``BIGINT``, ``REAL`` -> ``DOUBLE PRECISION``, ``BLOB`` -> ``BYTEA``
* ``PRAGMA table_info(t)`` -> same row shape from ``information_schema``;
  other PRAGMAs and ``BEGIN [IMMEDIATE]`` are no-ops
* bool/datetime params are adapted the way sqlite3 stores them

Everything else must be written in SQL both engines accept: ``ON CONFLICT ...
DO NOTHING / DO UPDATE`` instead of ``INSERT OR IGNORE/REPLACE``, ``COALESCE``
instead of ``IFNULL``, ``RETURNING id`` instead of ``cursor.lastrowid``,
``GREATEST``/``LEAST`` guarded by ``is_postgres`` where SQLite needs ``MAX``/``MIN``.

Each ``execute`` runs inside a savepoint on Postgres, so a caught
``IntegrityError`` leaves the transaction usable -- matching SQLite, where a
failed statement does not poison the connection.
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

try:  # psycopg is only required for the postgres backend
    import psycopg
    from psycopg import errors as _pg_errors
except ImportError:  # pragma: no cover - deployment configuration
    psycopg = None  # type: ignore[assignment]
    _pg_errors = None  # type: ignore[assignment]


if _pg_errors is not None:
    IntegrityError: tuple[type[BaseException], ...] = (
        sqlite3.IntegrityError,
        _pg_errors.IntegrityError,
    )
    OperationalError: tuple[type[BaseException], ...] = (
        sqlite3.OperationalError,
        _pg_errors.OperationalError,
    )
else:  # pragma: no cover
    IntegrityError = (sqlite3.IntegrityError,)
    OperationalError = (sqlite3.OperationalError,)


def backend() -> str:
    value = os.getenv("NEURON_SQL_BACKEND", "sqlite").strip().lower() or "sqlite"
    if value not in {"sqlite", "postgres"}:
        raise ValueError("NEURON_SQL_BACKEND must be 'sqlite' or 'postgres'")
    return value


def database_url() -> str | None:
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


def schema_for_path(path: str | Path) -> str:
    """Postgres schema that stands in for one SQLite file."""
    stem = Path(path).name.split(".", 1)[0] or "store"
    name = re.sub(r"[^a-z0-9_]", "_", stem.lower())
    if name[0].isdigit():
        name = f"s_{name}"
    prefix = os.getenv("NEURON_SQL_SCHEMA_PREFIX", "")
    return f"{prefix}{name}"[:63]


def connect(path: str | Path, *, timeout: float = 30.0):
    """Open the store behind ``path`` on the configured backend.

    SQLite connections get ``sqlite3.Row`` rows, exactly like the stores set
    up themselves before; callers may still override ``row_factory``.
    """
    if backend() == "postgres":
        url = database_url()
        if not url:
            raise RuntimeError("NEURON_SQL_BACKEND=postgres requires DATABASE_URL")
        return PgConnection(url, schema_for_path(path))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    return connection


def is_postgres(connection: Any) -> bool:
    return isinstance(connection, PgConnection)


def table_columns(connection: Any, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------- rows


class Row(Sequence):
    """sqlite3.Row look-alike: index, key (case-insensitive), ``keys()``, ``dict(row)``."""

    __slots__ = ("_values", "_keys", "_index")

    def __init__(self, keys: tuple[str, ...], index: dict[str, int], values: tuple):
        self._keys = keys
        self._index = index
        self._values = values

    def __getitem__(self, item):  # type: ignore[override]
        if isinstance(item, str):
            try:
                return self._values[self._index[item.lower()]]
            except KeyError:
                raise IndexError(f"No item with that key: {item}") from None
        return self._values[item]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def keys(self) -> list[str]:
        return list(self._keys)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Row):
            return self._keys == other._keys and self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._values)

    def __repr__(self) -> str:
        return f"Row({dict(zip(self._keys, self._values))!r})"


# ------------------------------------------------------------ translation

_STRING_OR_PLACEHOLDER = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\?|%")
_DDL_START = re.compile(r"^\s*(CREATE\s+TABLE|ALTER\s+TABLE)\b", re.IGNORECASE)
_NOOP = re.compile(r"^\s*(PRAGMA\s+(?!table_info)\w+(\s*=\s*[\w'\"-]+|\s*\([^)]*\))?|BEGIN(\s+(IMMEDIATE|DEFERRED|EXCLUSIVE))?(\s+TRANSACTION)?)\s*;?\s*$", re.IGNORECASE)
_READ_ONLY = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_TABLE_INFO = re.compile(r"^\s*PRAGMA\s+table_info\(\s*[\"']?(\w+)[\"']?\s*\)\s*;?\s*$", re.IGNORECASE)


def _translate_ddl(sql: str) -> str:
    sql = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY(\s+AUTOINCREMENT)?\b", "BIGSERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bINTEGER\b", "BIGINT", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.IGNORECASE)
    return sql


@lru_cache(maxsize=4096)
def translate(sql: str, has_params: bool) -> str:
    """SQLite-dialect statement -> Postgres statement (placeholders + DDL types)."""
    if _DDL_START.match(sql):
        sql = _translate_ddl(sql)

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "?":
            return "%s"
        if token == "%":
            return "%%" if has_params else "%"
        if has_params and "%" in token:
            # psycopg parses placeholders inside literals too.
            return token.replace("%", "%%")
        return token

    return _STRING_OR_PLACEHOLDER.sub(replace, sql)


def split_script(script: str) -> list[str]:
    """Split an ``executescript`` body on top-level semicolons (quote-aware)."""
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in script:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _adapt(value: Any) -> Any:
    # Mirror what sqlite3 stored so rows read back identically on both engines.
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat(" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _adapt_params(params: Any) -> Any:
    if params is None:
        return None
    if isinstance(params, dict):
        return {key: _adapt(value) for key, value in params.items()}
    return tuple(_adapt(value) for value in params)


# ------------------------------------------------------------------- pool

_POOL_LOCK = threading.Lock()
_POOL: dict[tuple[str, str], list[Any]] = {}
_POOL_MAX = int(os.getenv("NEURON_SQL_POOL_SIZE", "8"))
_SCHEMAS_READY: set[tuple[str, str]] = set()


def _open_raw(url: str, schema: str):
    if psycopg is None:  # pragma: no cover
        raise RuntimeError("Install psycopg[binary] to use NEURON_SQL_BACKEND=postgres")
    key = (url, schema)
    with _POOL_LOCK:
        idle = _POOL.get(key)
        while idle:
            raw = idle.pop()
            if not raw.closed and raw.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
                return raw
            raw.close()
    raw = psycopg.connect(url, autocommit=False)
    if key not in _SCHEMAS_READY:
        try:
            with raw.cursor() as cursor:
                cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            raw.commit()
        except (_pg_errors.UniqueViolation, _pg_errors.DuplicateSchema):
            # Another process/thread created it between our check and insert.
            raw.rollback()
        _SCHEMAS_READY.add(key)
    with raw.cursor() as cursor:
        cursor.execute(f'SET search_path TO "{schema}"')
    raw.commit()
    return raw


def _release_raw(url: str, schema: str, raw: Any) -> None:
    if raw.closed:
        return
    if raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raw.rollback()
    with _POOL_LOCK:
        idle = _POOL.setdefault((url, schema), [])
        if len(idle) < _POOL_MAX:
            idle.append(raw)
            return
    raw.close()


def close_pool() -> None:
    """Close every idle pooled connection (tests; process shutdown)."""
    with _POOL_LOCK:
        for idle in _POOL.values():
            for raw in idle:
                raw.close()
        _POOL.clear()
        _SCHEMAS_READY.clear()


# ------------------------------------------------------------- connection


class PgCursor:
    def __init__(self, rows: list[Row] | None, rowcount: int, description: Any):
        self._rows = rows or []
        self._pos = 0
        self.rowcount = rowcount
        self.description = description

    @property
    def lastrowid(self):  # pragma: no cover - guard rail
        raise NotImplementedError("Postgres has no lastrowid; use 'RETURNING id' and fetchone()")

    def fetchone(self) -> Row | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def fetchmany(self, size: int = 1) -> list[Row]:
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def __iter__(self) -> Iterator[Row]:
        while (row := self.fetchone()) is not None:
            yield row

    def close(self) -> None:
        self._rows = []


class PgConnection:
    """The sqlite3.Connection subset the stores use, backed by a pooled psycopg connection.

    ``with connection:`` commits on success / rolls back on error and then hands
    the socket back to the pool. The object stays usable afterwards (SQLite
    semantics): the next ``execute`` transparently checks out a connection.
    """

    row_factory: Any = None  # accepted and ignored; rows are always ``Row``

    def __init__(self, url: str, schema: str):
        self.url = url
        self.schema = schema
        self._raw: Any = None

    # -- plumbing
    def _conn(self):
        if self._raw is None:
            self._raw = _open_raw(self.url, self.schema)
        return self._raw

    @property
    def in_transaction(self) -> bool:
        return self._raw is not None and self._raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE

    # -- DB-API subset
    def execute(self, sql: str, params: Any = None) -> PgCursor:
        if _NOOP.match(sql):
            return PgCursor([], -1, None)
        info = _TABLE_INFO.match(sql)
        if info:
            return self._table_info(info.group(1))
        params = _adapt_params(params) if params else None
        statement = translate(sql, params is not None)
        with self._statement(guard=not _READ_ONLY.match(sql)) as cursor:
            cursor.execute(statement, params)
            return self._materialize(cursor)

    def executemany(self, sql: str, seq_of_params: Iterable[Any]) -> PgCursor:
        rows = [_adapt_params(params) for params in seq_of_params]
        if not rows:
            return PgCursor([], 0, None)
        statement = translate(sql, True)
        with self._statement(guard=True) as cursor:
            cursor.executemany(statement, rows)
            return PgCursor([], cursor.rowcount, None)

    @contextmanager
    def _statement(self, *, guard: bool):
        """Cursor inside the connection's open transaction.

        Writes run under a savepoint so a failed statement (typically a caught
        IntegrityError) is undone alone and the transaction stays usable.
        Nothing here commits -- that stays with ``commit()`` / ``with conn:``.
        """
        raw = self._conn()
        with raw.cursor() as cursor:
            if not guard:
                yield cursor
                return
            cursor.execute("SAVEPOINT neuron_stmt")
            try:
                yield cursor
            except BaseException:
                if not raw.closed and raw.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    raw.execute("ROLLBACK TO SAVEPOINT neuron_stmt")
                    raw.execute("RELEASE SAVEPOINT neuron_stmt")
                raise
            cursor.execute("RELEASE SAVEPOINT neuron_stmt")

    def executescript(self, script: str) -> PgCursor:
        for statement in split_script(script):
            self.execute(statement)
        return PgCursor([], -1, None)

    def cursor(self) -> PgConnection:
        # Stores only call cursor().execute(...); the connection already quacks like one.
        return self

    def commit(self) -> None:
        if self._raw is not None:
            self._raw.commit()

    def rollback(self) -> None:
        if self._raw is not None:
            self._raw.rollback()

    def close(self) -> None:
        if self._raw is not None:
            raw, self._raw = self._raw, None
            _release_raw(self.url, self.schema, raw)

    def __enter__(self) -> PgConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()

    def __del__(self):  # pragma: no cover - best effort
        try:
            self.close()
        except Exception:
            pass

    # -- helpers
    @staticmethod
    def _materialize(cursor: Any) -> PgCursor:
        if cursor.description is None:
            return PgCursor([], cursor.rowcount, None)
        keys = tuple(column.name for column in cursor.description)
        index = {key.lower(): position for position, key in enumerate(keys)}
        rows = [Row(keys, index, tuple(values)) for values in cursor.fetchall()]
        return PgCursor(rows, cursor.rowcount, cursor.description)

    def _table_info(self, table: str) -> PgCursor:
        with self._statement(guard=False) as cursor:
            cursor.execute(
                """
                SELECT ordinal_position - 1 AS cid, column_name AS name, data_type AS type,
                       (is_nullable = 'NO')::int AS notnull, column_default AS dflt_value,
                       0 AS pk
                FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table.lower(),),
            )
            return self._materialize(cursor)
