"""Sweep RRF fusion parameters against the real golden set, keep the winner.

Raw fulltext/vector hits are fetched ONCE per case (at the largest
per_method_limit in the grid) and re-combined in pure Python for every
parameter combination -- the expensive part (graph queries, one embedding
call per question) happens once, not once per combination.

Usage:
    uv run python -m scripts.sweep_retrieval_params eval/argus_golden.jsonl
    uv run python -m scripts.sweep_retrieval_params eval/less_token_golden.jsonl --graph less_token
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from statistics import mean

from openai import OpenAI

from graph.access import AccessScope
from graph.falkor_client import get_graph
from graph.schema import FULLTEXT_LABELS
from graph import vector_store
from graph.search import (
    _fulltext_search, _vector_search_global, embed_query, fuse_fulltext_labels, interleave,
)
from scripts.evaluate_retrieval import resolve_target
from util import paths as _paths  # noqa: F401 - load repo .env

# Must mirror `hybrid_search`'s own default exactly. This used to be
# `FULLTEXT_LABELS & VECTOR_LABELS`, the same intersection that was removed
# from production for excluding whole labels from search -- a sweep measuring
# a different label set than the thing it is tuning reports numbers the
# product can never reproduce.
SEARCH_LABELS = list(FULLTEXT_LABELS)

RRF_K_GRID = [10, 30, 60]
VECTOR_WEIGHT_GRID = [1.0, 1.5, 2.0, 3.0]
PER_METHOD_LIMIT_GRID = [10, 20, 30]
FULLTEXT_STRATEGY_GRID = ["per_label_rank", "global_score", "normalized_merge"]
MAX_LIMIT = max(PER_METHOD_LIMIT_GRID)


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def fetch_raw_hits(
    graph, client, case: dict, scope: AccessScope, collection: str,
) -> dict:
    """One fulltext + one vector fetch per label, at MAX_LIMIT -- reused for
    every (rrf_k, vector_weight, per_method_limit) combination below.

    Scores are kept alongside the uids, not discarded: a fusion strategy that
    normalizes across labels needs the raw score, and re-fetching per strategy
    would defeat the whole point of caching here."""
    providers = case.get("providers")
    embedding = embed_query(client, case["query"])
    fulltext_by_label = {
        label: _fulltext_search(graph, label, case["query"], MAX_LIMIT, scope, providers)
        for label in SEARCH_LABELS
    }
    # Global vector fetches, one per named channel, interleaved exactly as
    # production does -- a sweep that fuses differently tunes a system that
    # does not exist.
    budget = MAX_LIMIT * len(SEARCH_LABELS)
    content_hits = _vector_search_global(
        graph, embedding, budget, scope, providers, labels=SEARCH_LABELS,
        collection=collection, using=vector_store.CONTENT_VECTOR,
    )
    name_hits = _vector_search_global(
        graph, embedding, budget, scope, providers, labels=SEARCH_LABELS,
        collection=collection, using=vector_store.NAME_VECTOR,
    )
    return {
        "fulltext_by_label": fulltext_by_label,
        "vector": [uid for uid, *_rest in interleave(content_hits, name_hits)],
    }


def rank_for_params(
    raw: dict, rrf_k: int, vector_weight: float, per_method_limit: int,
    fulltext_strategy: str, limit: int,
) -> list[str]:
    """Must stay a faithful replica of `hybrid_search`'s fusion, or the sweep
    tunes something the product does not run."""
    scores: dict[str, float] = {}
    trimmed = {
        label: hits[:per_method_limit]
        for label, hits in raw["fulltext_by_label"].items()
    }
    for rank, (uid, _label, _name, _summary) in enumerate(
        fuse_fulltext_labels(trimmed, fulltext_strategy)
    ):
        scores[uid] = scores.get(uid, 0.0) + 1.0 / (rrf_k + rank + 1)
    for rank, uid in enumerate(raw["vector"][: per_method_limit * len(SEARCH_LABELS)]):
        scores[uid] = scores.get(uid, 0.0) + vector_weight / (rrf_k + rank + 1)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [uid for uid, _ in ranked[:limit]]


def reciprocal_rank(retrieved: list[str], expected: list[str]) -> float:
    ranks = [retrieved.index(uid) + 1 for uid in expected if uid in retrieved]
    return 1.0 / min(ranks) if ranks else 0.0


def recall(retrieved: list[str], expected: list[str]) -> float:
    if not expected:
        return 0.0
    return len(set(retrieved) & set(expected)) / len(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--limit", type=int, default=8, help="final top-N (matches chat's search_limit)")
    parser.add_argument("--graph", default="default")
    args = parser.parse_args()

    target = resolve_target(args.graph)
    graph = get_graph(name=target.falkor_name)
    client = OpenAI()
    scope = AccessScope.trusted_internal()
    cases = load_cases(args.dataset)

    print(f"Graph: {target.name} ({target.falkor_name} / {target.qdrant_collection})")
    print(f"Fetching raw hits for {len(cases)} cases (one embedding + fulltext/vector fetch each)...")
    raw_by_case = [
        fetch_raw_hits(graph, client, case, scope, target.qdrant_collection) for case in cases
    ]

    grid = list(itertools.product(
        RRF_K_GRID, VECTOR_WEIGHT_GRID, PER_METHOD_LIMIT_GRID, FULLTEXT_STRATEGY_GRID
    ))
    results = []
    for rrf_k, vector_weight, per_method_limit, strategy in grid:
        mrrs, recalls, passing = [], [], 0
        for case, raw in zip(cases, raw_by_case):
            retrieved = rank_for_params(
                raw, rrf_k, vector_weight, per_method_limit, strategy, args.limit
            )
            expected = case.get("expected_uids") or []
            if not expected:
                continue
            rr = reciprocal_rank(retrieved, expected)
            mrrs.append(rr)
            recalls.append(recall(retrieved, expected))
            passing += rr > 0
        results.append({
            "rrf_k": rrf_k, "vector_weight": vector_weight,
            "per_method_limit": per_method_limit, "strategy": strategy,
            "mrr": mean(mrrs) if mrrs else 0.0, "recall": mean(recalls) if recalls else 0.0,
            "passing": passing, "scored": len(mrrs),
        })

    # Rank by recall first: these cases fail by the right answer being absent
    # entirely, not by it sitting at position 3 instead of 1.
    results.sort(key=lambda r: (r["recall"], r["mrr"]), reverse=True)
    print(f"\n{'rrf_k':>6} {'vec_wt':>7} {'pm_lim':>7} {'strategy':>18}   {'recall':>7} {'MRR':>7} {'pass':>6}")
    for r in results[:15]:
        print(f"{r['rrf_k']:>6} {r['vector_weight']:>7} {r['per_method_limit']:>7} "
              f"{r['strategy']:>18}   {r['recall']:.4f} {r['mrr']:.4f} "
              f"{r['passing']:>3}/{r['scored']}")

    best = results[0]
    print(f"\nBest: k={best['rrf_k']} vec_weight={best['vector_weight']} "
          f"per_method_limit={best['per_method_limit']} strategy={best['strategy']}"
          f"  -> recall={best['recall']:.4f} MRR={best['mrr']:.4f} "
          f"passing={best['passing']}/{best['scored']}")


if __name__ == "__main__":
    main()
