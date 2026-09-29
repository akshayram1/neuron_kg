"""Suite-wide isolation from developer-machine runtime configuration."""

from __future__ import annotations

import os

import pytest
import sys


@pytest.fixture(autouse=True)
def disable_runtime_reranker_by_default(monkeypatch):
    """Unit tests opt into a scorer explicitly; local .env must not do it."""
    monkeypatch.setenv("NEURON_RERANK", "off")
    chat = sys.modules.get("graph.retrieval.chat")
    if chat is not None:
        monkeypatch.setattr(chat, "_reranker_enabled_override", None)


def _drop_prefixed_schemas(url: str, prefix: str) -> None:
    import psycopg

    from storage import sql_backend as backend_module

    backend_module.close_pool()
    with psycopg.connect(url, autocommit=True) as connection:
        schemas = connection.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s", (prefix + "%",)
        ).fetchall()
        for (schema,) in schemas:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture(autouse=True)
def suite_sql_backend(monkeypatch):
    """``NEURON_TEST_SQL_BACKEND=postgres`` runs every store test on Postgres.

    Each test gets its own schema prefix (tests reuse tmp file stems like
    ``l.sqlite3``), dropped afterwards. Needs ``NEURON_TEST_DATABASE_URL``.
    Without the switch every test is pinned to SQLite so a developer's
    ``NEURON_SQL_BACKEND`` never leaks into the suite.
    """
    import uuid

    url = os.getenv("NEURON_TEST_DATABASE_URL")
    if os.getenv("NEURON_TEST_SQL_BACKEND") != "postgres" or not url:
        monkeypatch.setenv("NEURON_SQL_BACKEND", "sqlite")
        yield
        return
    prefix = f"t{uuid.uuid4().hex[:10]}_"
    monkeypatch.setenv("NEURON_SQL_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NEURON_SQL_SCHEMA_PREFIX", prefix)
    yield
    _drop_prefixed_schemas(url, prefix)


@pytest.fixture
def sql_backend(request, monkeypatch):
    """Run a store test on SQLite and (when reachable) Postgres.

    Parametrize with ``@pytest.mark.parametrize("sql_backend", ["sqlite",
    "postgres"], indirect=True)``. The postgres leg needs
    ``NEURON_TEST_DATABASE_URL`` and gets a unique schema prefix, dropped after.
    """
    import uuid

    name = getattr(request, "param", "sqlite")
    if name == "sqlite":
        monkeypatch.setenv("NEURON_SQL_BACKEND", "sqlite")
        yield name
        return
    url = os.getenv("NEURON_TEST_DATABASE_URL")
    if not url:
        pytest.skip("NEURON_TEST_DATABASE_URL not set")
    try:
        import psycopg

        psycopg.connect(url, connect_timeout=2).close()
    except Exception as exc:  # pragma: no cover - environment
        pytest.skip(f"Postgres unreachable: {exc}")
    prefix = f"t{uuid.uuid4().hex[:10]}_"
    monkeypatch.setenv("NEURON_SQL_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("NEURON_SQL_SCHEMA_PREFIX", prefix)
    yield name
    _drop_prefixed_schemas(url, prefix)
