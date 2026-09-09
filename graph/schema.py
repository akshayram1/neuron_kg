"""Index/constraint bootstrap. Must run before any bulk load (plan.md §2.5) —
without these, every `MERGE` degrades to a full label scan.

DDL syntax verified against the running FalkorDB (8.6.3 module 42004):
  - `CREATE INDEX IF NOT EXISTS` is NOT supported by this version's Cypher.
  - Re-running any of the three CREATE...INDEX statements against an
    already-indexed attribute raises `redis.exceptions.ResponseError` with
    "already indexed" in the message — that is how idempotency is detected
    here, not by a Cypher-level guard.
"""

from __future__ import annotations

import logging

from falkordb import Graph
from redis.exceptions import ResponseError

logger = logging.getLogger("neuron.schema")

# uid range index — every entity label carries a deterministic `uid`
# (plan.md §2.2) that MERGE keys on.
ENTITY_LABELS = [
    "Project",
    "Workspace",
    "WorkItem",
    "Person",
    "Repository",
    "SourceFile",
    "Commit",
    "PullRequest",
    "Document",
    "Workspace",
    "Decision",
    "Term",
    "System",
]

# fulltext (BM25) — only content-bearing labels need keyword search.
FULLTEXT_LABELS = ["WorkItem", "Document", "Decision", "Term", "Commit"]

# vector (cosine) — only content-bearing labels get embeddings (plan.md §4:
# embedding every structural node would be a real RAM cost at millions of rows).
VECTOR_LABELS = ["WorkItem", "Document", "Decision", "Term"]
EMBEDDING_DIMENSION = 1536  # text-embedding-3-small


def _run_idempotent(graph: Graph, cypher: str, description: str) -> None:
    try:
        graph.query(cypher)
        logger.info("created: %s", description)
    except ResponseError as exc:
        if "already indexed" in str(exc):
            logger.debug("already exists, skipping: %s", description)
            return
        raise


def bootstrap_schema(graph: Graph) -> None:
    """Create every index this project relies on. Safe to call every startup."""
    _run_idempotent(
        graph,
        "CREATE INDEX FOR (n:SourceRecord) ON (n.record_key)",
        "range index SourceRecord.record_key",
    )
    for label in ENTITY_LABELS:
        _run_idempotent(
            graph,
            f"CREATE INDEX FOR (n:{label}) ON (n.uid)",
            f"range index {label}.uid",
        )

    for label in FULLTEXT_LABELS:
        _run_idempotent(
            graph,
            f"CALL db.idx.fulltext.createNodeIndex('{label}', 'search_text')",
            f"fulltext index {label}.search_text",
        )

    for label in VECTOR_LABELS:
        _run_idempotent(
            graph,
            (
                f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding) "
                f"OPTIONS {{dimension: {EMBEDDING_DIMENSION}, similarityFunction: 'cosine'}}"
            ),
            f"vector index {label}.embedding",
        )


if __name__ == "__main__":
    from graph.falkor_client import get_graph

    logging.basicConfig(level=logging.INFO)
    bootstrap_schema(get_graph())
