"""Named graph resolution + registry.

One physical dataset used to be implicit: one FalkorDB graph, one Qdrant
collection, one ledger file, all named by fixed constants. This module makes
"which dataset" an explicit, small string (a `graph_name` slug) that resolves
to those three physical names.

`"default"` is not a special case grafted on top — it deliberately resolves to
the ORIGINAL bare names (`neuron`, `neuron_entities`,
`connector_ledger.sqlite3`), so the graph that already has real ingested data
keeps working with zero migration and shows up in a graph picker for free.
Every other slug gets its own suffixed resource names, fully isolated.

OAuth connections are NOT scoped by graph_name (see
`connectors/core/oauth_store.py`'s own docstring) -- only the ledger, the
FalkorDB graph, and the Qdrant collection are. Deliberate: switching graphs
should never require re-authenticating a connector.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_GRAPH_NAME = "default"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class InvalidGraphName(ValueError):
    pass


def validate_graph_name(name: str) -> str:
    if not _SLUG_RE.match(name):
        raise InvalidGraphName(
            "graph name must be 1-40 chars, lowercase letters/digits/-/_, "
            "starting with a letter or digit"
        )
    return name


@dataclass(frozen=True)
class GraphTarget:
    name: str
    falkor_name: str
    qdrant_collection: str
    ledger_path: Path


def resolve(graph_name: str, *, data_dir: Path, base_falkor_name: str, base_collection: str) -> GraphTarget:
    """Pure name mapping -- no I/O, no side effects."""
    name = validate_graph_name(graph_name or DEFAULT_GRAPH_NAME)
    if name == DEFAULT_GRAPH_NAME:
        return GraphTarget(
            name=name,
            falkor_name=base_falkor_name,
            qdrant_collection=base_collection,
            ledger_path=data_dir / "connector_ledger.sqlite3",
        )
    return GraphTarget(
        name=name,
        falkor_name=f"{base_falkor_name}__{name}",
        qdrant_collection=f"{base_collection}__{name}",
        ledger_path=data_dir / f"connector_ledger__{name}.sqlite3",
    )


class GraphRegistry:
    """Just a name registry (display name + created_at) for the graph picker --
    FalkorDB/Qdrant/the ledger are the actual source of truth for each
    graph's data; this only remembers that a name was deliberately created,
    so the UI has something to list before any data exists."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS graphs (
                    name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO graphs(name, display_name, created_at) VALUES (?, ?, ?)",
                (DEFAULT_GRAPH_NAME, "Default", datetime.now(UTC).isoformat()),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def list(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT name, display_name, created_at FROM graphs ORDER BY created_at"
            ).fetchall()
        return [{"name": r[0], "displayName": r[1], "createdAt": r[2]} for r in rows]

    def exists(self, name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM graphs WHERE name = ?", (name,)).fetchone()
        return row is not None

    def create(self, name: str, display_name: str | None = None) -> None:
        name = validate_graph_name(name)
        if self.exists(name):
            raise ValueError(f"graph {name!r} already exists")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO graphs(name, display_name, created_at) VALUES (?, ?, ?)",
                (name, display_name or name, datetime.now(UTC).isoformat()),
            )
