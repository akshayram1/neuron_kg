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
    "Decision",
    "Term",
    "System",
    "Api",
    "Endpoint",
    "Finding",
    "Wisdom",
    "FactHistory",
]

# fulltext (BM25) — only content-bearing labels need keyword search.
# SourceFile added here (verified live: "is Animesh mentioned in argus" --
# animeshtmdcio is a real username inside test-fixture SourceFile content,
# but SourceFile wasn't in this list at all, so hybrid_search never had a
# chance to see it, same class of gap Commit had before it). Fulltext alone
# turned out insufficient too -- see VECTOR_LABELS below.
FULLTEXT_LABELS = [
    "WorkItem", "Document", "Decision", "Term", "Api", "Endpoint",
    "Commit", "PullRequest", "SourceFile", "Finding", "Wisdom",
]

# Labels that get embeddings — only content-bearing ones (plan.md §4:
# embedding every structural node would be a real cost at millions of rows).
# The vectors themselves live in Qdrant now, not FalkorDB (see
# `graph/vector_store.py` for why), so this list no longer drives any index
# DDL here — it still defines *which* labels are embeddable, which
# `semantic_pass` and `search` both read.
# SourceFile added alongside Commit for the same reason: RediSearch tokenizes
# an identifier like `animeshtmdcio` as one atomic token, so a fulltext query
# for "Animesh" alone (verified live) returns zero SourceFile hits even
# though the fulltext index now covers the label -- only the vector leg's
# subword-aware embedding can bridge that. Verified real cost of embedding
# all 264 existing SourceFile nodes: ~435K tokens, ~$0.009 total (see
# cost.md) -- not the "264+ extra OpenAI calls" cost concern it looks like
# on paper, because text-embedding-3-small is priced at $0.02/1M tokens.
VECTOR_LABELS = [
    "WorkItem", "Document", "Decision", "Term", "Api", "Endpoint",
    "PullRequest", "Commit", "SourceFile", "Finding", "Wisdom",
]
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

    _run_idempotent(
        graph,
        "CREATE INDEX FOR (n:FactHistory) ON (n.fact_uid)",
        "range index FactHistory.fact_uid",
    )

    for label in FULLTEXT_LABELS:
        _run_idempotent(
            graph,
            f"CALL db.idx.fulltext.createNodeIndex('{label}', 'search_text')",
            f"fulltext index {label}.search_text",
        )

    # No vector indexes here on purpose: dense vectors moved to Qdrant
    # (`graph/vector_store.py`). FalkorDB's vector index has no quantization
    # and keeps float32 in RAM, which is the ceiling we moved to escape.


if __name__ == "__main__":
    from graph.falkor_client import get_graph

    logging.basicConfig(level=logging.INFO)
    bootstrap_schema(get_graph())
