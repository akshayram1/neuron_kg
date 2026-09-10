"""Sweep RRF fusion parameters against the real golden set, keep the winner.

Raw fulltext/vector hits are fetched ONCE per case (at the largest
per_method_limit in the grid) and re-combined in pure Python for every
parameter combination -- the expensive part (graph queries, one embedding
call per question) happens once, not once per combination.

Usage:
    uv run python -m scripts.sweep_retrieval_params eval/argus_golden.jsonl
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
from graph.schema import FULLTEXT_LABELS, VECTOR_LABELS
from graph.search import _fulltext_search, _vector_search, embed_query
from util import paths as _paths  # noqa: F401 - load repo .env

SEARCH_LABELS = sorted(set(FULLTEXT_LABELS) & set(VECTOR_LABELS))

RRF_K_GRID = [10, 30, 60]
VECTOR_WEIGHT_GRID = [1.0, 1.5, 2.0, 3.0]
PER_METHOD_LIMIT_GRID = [10, 20, 30]
MAX_LIMIT = max(PER_METHOD_LIMIT_GRID)


def load_cases(path: Path) -> list[dict]:
    cases = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def fetch_raw_hits(graph, client, case: dict, scope: AccessScope) -> dict:
    """One fulltext + one vector fetch per label, at MAX_LIMIT -- reused for
    every (rrf_k, vector_weight, per_method_limit) combination below."""
    providers = case.get("providers")
    embedding = embed_query(client, case["query"])
    per_label = {}
    for label in SEARCH_LABELS:
        fulltext_hits = _fulltext_search(graph, label, case["query"], MAX_LIMIT, scope, providers)
        vector_hits = _vector_search(graph, label, embedding, MAX_LIMIT, scope, providers)
        per_label[label] = {
            "fulltext": [uid for uid, *_ in fulltext_hits],
            "vector": [uid for uid, *_ in vector_hits],
        }
    return per_label


def rank_for_params(per_label: dict, rrf_k: int, vector_weight: float, per_method_limit: int, limit: int) -> list[str]:
    scores: dict[str, float] = {}
    for label, hits in per_label.items():
        for rank, uid in enumerate(hits["fulltext"][:per_method_limit]):
            scores[uid] = scores.get(uid, 0.0) + 1.0 / (rrf_k + rank + 1)
        for rank, uid in enumerate(hits["vector"][:per_method_limit]):
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
    args = parser.parse_args()

    graph = get_graph()
    client = OpenAI()
    scope = AccessScope.trusted_internal()
    cases = load_cases(args.dataset)

    print(f"Fetching raw hits for {len(cases)} cases (one embedding + fulltext/vector fetch each)...")
    raw_by_case = [fetch_raw_hits(graph, client, case, scope) for case in cases]

    grid = list(itertools.product(RRF_K_GRID, VECTOR_WEIGHT_GRID, PER_METHOD_LIMIT_GRID))
    results = []
    for rrf_k, vector_weight, per_method_limit in grid:
        mrrs, recalls = [], []
        for case, per_label in zip(cases, raw_by_case):
            retrieved = rank_for_params(per_label, rrf_k, vector_weight, per_method_limit, args.limit)
            expected = case.get("expected_uids") or []
            if not expected:
                continue
            mrrs.append(reciprocal_rank(retrieved, expected))
            recalls.append(recall(retrieved, expected))
        results.append({
            "rrf_k": rrf_k, "vector_weight": vector_weight, "per_method_limit": per_method_limit,
            "mrr": mean(mrrs) if mrrs else 0.0, "recall": mean(recalls) if recalls else 0.0,
        })

    results.sort(key=lambda r: (r["mrr"], r["recall"]), reverse=True)
    print(f"\n{'rrf_k':>6} {'vec_wt':>7} {'pm_limit':>9}   {'MRR':>6} {'recall':>7}")
    for r in results[:15]:
        print(f"{r['rrf_k']:>6} {r['vector_weight']:>7} {r['per_method_limit']:>9}   {r['mrr']:.4f} {r['recall']:.4f}")

    # Baseline (current production config) for comparison.
    baseline = next(r for r in results if r["rrf_k"] == 60 and r["vector_weight"] == 1.0 and r["per_method_limit"] == 20)
    best = results[0]
    print(f"\nCurrent production config (k=60, vec_weight=1.0, per_method_limit=20): MRR={baseline['mrr']:.4f} recall={baseline['recall']:.4f}")
    print(f"Best found: k={best['rrf_k']}, vec_weight={best['vector_weight']}, per_method_limit={best['per_method_limit']}: MRR={best['mrr']:.4f} recall={best['recall']:.4f}")


if __name__ == "__main__":
    main()
