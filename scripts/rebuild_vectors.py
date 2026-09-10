"""Rebuild the Qdrant collection from the graph.

This script is what makes "the graph is the source of truth, Qdrant is a
rebuildable projection" an actual guarantee rather than a slogan. Any drift
— a failed upsert, a changed embedding model, a different quantization
config, a wiped Qdrant volume — is repaired by running this: it re-embeds
every embeddable node straight from the graph's own `search_text` and
replaces the collection's contents.

    uv run python -m scripts.rebuild_vectors            # rebuild in place
    uv run python -m scripts.rebuild_vectors --recreate # drop collection first

`--recreate` is the one to use after changing vector params (dimension,
distance, quantization), since those are fixed at collection creation.
"""

from __future__ import annotations

import argparse
import logging

from openai import OpenAI

from util import paths as _paths  # noqa: F401 — loads .env from repo root
from util.logging import configure_logging

from graph import vector_store
from graph.falkor_client import get_graph
from graph.schema import VECTOR_LABELS

logger = logging.getLogger("neuron.rebuild_vectors")

BATCH = 64


def rebuild(*, recreate: bool) -> None:
    graph = get_graph()
    client = vector_store.build_client()
    openai_client = OpenAI()

    if recreate and client.collection_exists(vector_store.COLLECTION):
        client.delete_collection(vector_store.COLLECTION)
        logger.info("dropped collection %s", vector_store.COLLECTION)
    vector_store.ensure_collection(client)

    total = 0
    for label in VECTOR_LABELS:
        rows = graph.query(
            f"MATCH (n:{label}) WHERE n.search_text IS NOT NULL AND n.search_text <> '' "
            "RETURN n.uid, n.search_text"
        ).result_set
        logger.info("%s: %d nodes with text", label, len(rows))

        for start in range(0, len(rows), BATCH):
            batch = rows[start : start + BATCH]
            texts = [vector_store.truncate_for_embedding(row[1]) for row in batch]
            vectors = openai_client.embeddings.create(
                model=vector_store.EMBEDDING_MODEL, input=texts
            ).data
            vector_store.upsert_vectors(
                client,
                [
                    {"uid": row[0], "label": label, "embedding": item.embedding}
                    for row, item in zip(batch, vectors)
                ],
            )
            total += len(batch)
            logger.info("%s: embedded %d/%d", label, min(start + BATCH, len(rows)), len(rows))

    logger.info("rebuild done: %d vectors, collection now holds %d", total, vector_store.count(client))


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recreate", action="store_true",
        help="drop and recreate the collection first (required after changing vector params)",
    )
    args = parser.parse_args()
    rebuild(recreate=args.recreate)


if __name__ == "__main__":
    main()
