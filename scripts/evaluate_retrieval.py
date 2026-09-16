"""Run a versioned golden set against the unified graph retrieval/chat path.

Example:
    uv run python -m scripts.evaluate_retrieval eval/golden.jsonl --k 8
    uv run python -m scripts.evaluate_retrieval eval/golden.jsonl --with-chat
    uv run python -m scripts.evaluate_retrieval eval/less_token_golden.jsonl --graph less_token

Each JSONL row supports:
  query (required), expected_uids, expected_citation_record_keys, providers.
Empty expectation lists are excluded from that metric rather than counted as
perfect, so placeholder cases cannot inflate a score.

`--graph` is not optional sugar: once multi-graph landed, a run without it
silently measured the `default` graph's data and its own Qdrant collection,
so a golden set written against another graph scored ~0 for reasons that had
nothing to do with retrieval quality.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any

from openai import OpenAI

from graph import multigraph, vector_store
from graph.access import AccessScope
from graph.chat import retrieve, run_chat_turn
from graph.falkor_client import get_graph
from util import paths as _paths  # noqa: F401 — load repo .env
from util.paths import DATA_DIR


def resolve_target(graph_name: str) -> multigraph.GraphTarget:
    """Same resolution the API routes use, so a benchmark and the product
    never disagree about which physical graph/collection a name means."""
    return multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store.COLLECTION,
    )


def _set_metric(actual: list[str], expected: list[str]) -> tuple[float, float]:
    actual_set, expected_set = set(actual), set(expected)
    if not expected_set:
        return 0.0, 0.0
    overlap = len(actual_set & expected_set)
    precision = overlap / len(actual_set) if actual_set else 0.0
    recall = overlap / len(expected_set)
    return precision, recall


def score_case(
    case: dict[str, Any], retrieved_uids: list[str], citation_keys: list[str] | None = None,
) -> dict[str, Any]:
    expected_uids = list(case.get("expected_uids") or [])
    expected_citations = list(case.get("expected_citation_record_keys") or [])
    retrieval_precision, retrieval_recall = _set_metric(retrieved_uids, expected_uids)
    ranks = [retrieved_uids.index(uid) + 1 for uid in expected_uids if uid in retrieved_uids]
    result: dict[str, Any] = {
        "query": case["query"],
        "retrieved_uids": retrieved_uids,
        "expected_uids": expected_uids,
        "retrieval_precision": retrieval_precision,
        "retrieval_recall": retrieval_recall,
        "reciprocal_rank": 1.0 / min(ranks) if ranks else 0.0,
        "retrieval_scored": bool(expected_uids),
    }
    if citation_keys is not None:
        citation_precision, citation_recall = _set_metric(citation_keys, expected_citations)
        result.update({
            "citation_keys": citation_keys,
            "expected_citation_record_keys": expected_citations,
            "citation_precision": citation_precision,
            "citation_recall": citation_recall,
            "citation_scored": bool(expected_citations),
        })
    return result


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = [item for item in results if item["retrieval_scored"]]
    citations = [item for item in results if item.get("citation_scored")]
    return {
        "cases": len(results),
        "retrieval_cases_scored": len(retrieval),
        "recall_at_k": mean(item["retrieval_recall"] for item in retrieval) if retrieval else None,
        "precision_at_k": mean(item["retrieval_precision"] for item in retrieval) if retrieval else None,
        "mrr": mean(item["reciprocal_rank"] for item in retrieval) if retrieval else None,
        "citation_cases_scored": len(citations),
        "citation_precision": mean(item["citation_precision"] for item in citations) if citations else None,
        "citation_recall": mean(item["citation_recall"] for item in citations) if citations else None,
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        if not str(case.get("query") or "").strip():
            raise ValueError(f"{path}:{line_number}: query is required")
        cases.append(case)
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--with-chat", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--graph", default=multigraph.DEFAULT_GRAPH_NAME)
    args = parser.parse_args()

    target = resolve_target(args.graph)
    graph = get_graph(name=target.falkor_name)
    client = OpenAI()
    scope = AccessScope.trusted_internal()
    results = []
    for case in load_cases(args.dataset):
        providers = case.get("providers")
        # `retrieve`, not `hybrid_search`: the product composes the ranker with
        # the structured resolver, the name matcher and the time window. Scoring
        # the ranker alone measured a narrower path than ships -- three nilus
        # cases read 0 while answering correctly in chat.
        _structured, hits = retrieve(
            graph, client, case["query"], limit=args.k,
            providers=providers, scope=scope,
            collection=target.qdrant_collection,
        )
        citations = None
        if args.with_chat:
            answer = run_chat_turn(
                graph, client, case["query"], search_limit=args.k,
                providers=providers, scope=scope,
                collection=target.qdrant_collection,
            )
            citations = [item.record_key for item in answer.citations]
        results.append(score_case(case, [hit.uid for hit in hits], citations))

    payload = {"graph": target.name, "summary": summarize(results), "results": results}
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)


if __name__ == "__main__":
    main()
