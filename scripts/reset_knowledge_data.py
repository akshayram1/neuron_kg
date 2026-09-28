"""Clear Neuron knowledge data while preserving connector credentials/config.

This intentionally removes rebuildable graph/vector/ledger state.  It never
touches oauth_connectors.sqlite3, github_connector.sqlite3,
notion_connector.sqlite3, graphs.sqlite3, .env, or .secrets.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from falkordb import FalkorDB
from qdrant_client import QdrantClient

from util import paths as _paths  # noqa: F401 -- load repository .env
from storage.postgres import PostgresStore, database_url

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PREFIX = "neuron"
VECTOR_PREFIXES = ("neuron_entities", "nilus_cmp_", "atlas_cmp_")


def _falkor(port: int, apply: bool) -> dict:
    client = FalkorDB(host=os.getenv("FALKOR_HOST", "localhost"), port=port)
    names = sorted(name for name in client.list_graphs() if name.startswith(GRAPH_PREFIX))
    if apply:
        for name in names:
            client.select_graph(name).delete()
    return {"port": port, "graphs": names}


def _qdrant(apply: bool) -> list[str]:
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    names = sorted(
        item.name for item in client.get_collections().collections
        if item.name.startswith(VECTOR_PREFIXES)
    )
    if apply:
        for name in names:
            client.delete_collection(name)
    return names


def _postgres(apply: bool) -> dict:
    url = database_url() or "postgresql://neuron:neuron@localhost:55432/neuron"
    store = PostgresStore(url)
    store.bootstrap()
    with store.connect() as connection:
        graph_count = connection.execute("SELECT count(*) FROM knowledge_graphs").fetchone()[0]
        vector_count = connection.execute("SELECT count(*) FROM entity_embeddings").fetchone()[0]
        chunk_count = connection.execute("SELECT count(*) FROM source_chunks").fetchone()[0]
        if apply:
            connection.execute("TRUNCATE TABLE knowledge_graphs CASCADE")
            connection.execute("TRUNCATE TABLE entity_embeddings")
            connection.execute("DROP INDEX IF EXISTS idx_entity_embeddings_content_hnsw")
            connection.execute("DROP INDEX IF EXISTS idx_entity_embeddings_name_hnsw")
            connection.execute("DROP INDEX IF EXISTS idx_source_chunks_embedding_hnsw")
            connection.execute(
                "ALTER TABLE entity_embeddings ALTER COLUMN content_embedding TYPE vector(1024)"
            )
            connection.execute(
                "ALTER TABLE entity_embeddings ALTER COLUMN name_embedding TYPE vector(1024)"
            )
            connection.execute(
                "ALTER TABLE source_chunks ALTER COLUMN embedding TYPE vector(1024)"
            )
            connection.commit()
    if apply:
        store.bootstrap()
    return {
        "configured": True, "graphs": int(graph_count),
        "entity_vectors": int(vector_count), "chunks": int(chunk_count),
    }


def _local_state(apply: bool) -> list[str]:
    paths = sorted(ROOT.glob("connector_ledger*.sqlite3"))
    jobs = ROOT / "connector_jobs.sqlite3"
    if jobs.exists():
        paths.append(jobs)
    if apply:
        for path in paths:
            for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
                candidate.unlink(missing_ok=True)
    return [path.name for path in paths]


def reset(*, apply: bool) -> dict:
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": "apply" if apply else "dry-run",
        "falkordb": [], "qdrant": [], "postgres": {}, "local_state": [],
        "preserved": [
            "oauth_connectors.sqlite3", "github_connector.sqlite3",
            "notion_connector.sqlite3", "graphs.sqlite3", ".env", ".secrets/",
        ],
    }
    ports = sorted({6379, 6380, int(os.getenv("FALKOR_PORT", "6379"))})
    for port in ports:
        try:
            report["falkordb"].append(_falkor(port, apply))
        except Exception as exc:
            report["falkordb"].append({"port": port, "error": str(exc)})
    try:
        report["qdrant"] = _qdrant(apply)
    except Exception as exc:
        report["qdrant_error"] = str(exc)
    try:
        report["postgres"] = _postgres(apply)
    except Exception as exc:
        report["postgres"] = {"error": str(exc)}
    report["local_state"] = _local_state(apply)
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the reset")
    args = parser.parse_args()
    print(json.dumps(reset(apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
