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

from graph import multigraph, vector_store
from graph.falkor_client import get_graph
from graph.schema import VECTOR_LABELS
from scripts.evaluate_retrieval import resolve_target

logger = logging.getLogger("neuron.rebuild_vectors")

BATCH = 64


def rebuild(*, recreate: bool, graph_name: str = multigraph.DEFAULT_GRAPH_NAME) -> None:
    target = resolve_target(graph_name)
    collection = target.qdrant_collection
    graph = get_graph(name=target.falkor_name)
    client = vector_store.build_client()
    openai_client = OpenAI()

    if recreate and client.collection_exists(collection):
        client.delete_collection(collection)
        logger.info("dropped collection %s", collection)
    vector_store.ensure_collection(client, collection)

    total = 0
    for label in VECTOR_LABELS:
        rows = graph.query(
            f"MATCH (n:{label}) WHERE n.search_text IS NOT NULL AND n.search_text <> '' "
            "RETURN n.uid, n.search_text, n.name"
        ).result_set
        logger.info("%s: %d nodes with text", label, len(rows))

        for start in range(0, len(rows), BATCH):
            batch = rows[start : start + BATCH]
            texts = [vector_store.truncate_for_embedding(row[1]) for row in batch]
            # Both channels in one request per batch: content first, then the
            # bare names, so `data[i]` and `data[len+i]` pair up by position.
            names = [
                vector_store.truncate_for_embedding((row[2] or row[1]).strip() or row[1])
                for row in batch
            ]
            vectors = openai_client.embeddings.create(
                model=vector_store.EMBEDDING_MODEL, input=texts + names
            ).data
            vector_store.upsert_vectors(
                client,
                [
                    {
                        "uid": row[0], "label": label,
                        "embedding": vectors[index].embedding,
                        "name_embedding": vectors[len(batch) + index].embedding,
                        "embedded_text": texts[index][:400],
                        "embedded_model": vector_store.EMBEDDING_MODEL,
                    }
                    for index, row in enumerate(batch)
                ],
                collection=collection,
            )
            total += len(batch)
            logger.info("%s: embedded %d/%d", label, min(start + BATCH, len(rows)), len(rows))

    logger.info(
        "rebuild done: %d vectors, collection %s now holds %d",
        total, collection, vector_store.count(client, collection),
    )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--recreate", action="store_true",
        help="drop and recreate the collection first (required after changing vector params)",
    )
    parser.add_argument("--graph", default=multigraph.DEFAULT_GRAPH_NAME)
    args = parser.parse_args()
    rebuild(recreate=args.recreate, graph_name=args.graph)


if __name__ == "__main__":
    main()
