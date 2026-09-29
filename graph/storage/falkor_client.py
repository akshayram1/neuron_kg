"""FalkorDB connection — no Graphiti.

Every script gets its graph handle through here so connection settings live in
one place. One process-wide `FalkorDB` client; callers select the graph.
"""

from __future__ import annotations

import os

from falkordb import FalkorDB, Graph

from util import paths as _paths  # noqa: F401 — loads .env from repo root


def build_client() -> FalkorDB:
    return FalkorDB(
        host=os.getenv("FALKOR_HOST", "localhost"),
        port=int(os.getenv("FALKOR_PORT", "6379")),
    )


def get_graph(client: FalkorDB | None = None, name: str | None = None) -> Graph:
    """Select the single unified graph (plan.md §1 — one graph, not per-source)."""
    client = client or build_client()
    return client.select_graph(name or os.getenv("FALKOR_GRAPH", "neuron"))
