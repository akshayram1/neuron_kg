"""Measure Laya's real serving latency for the reranker gate (25-plan.md
§2.3 "Serving and latency").

That section calls for: "Measure first-pass pool size, candidates scored per
retry, retry rate, cold start, warm p50/p90, batch throughput and peak
memory on deployment-class hardware", plus verifying `predict_batch`
ordering/equality and the scorer-failure -> RRF fallback path. This script
answers the still-open evidence QUERIES.md flags under "Laya promotion
remains measurement-gated": "reranking default-on threshold and deployment
p50/p90" -- with real numbers from the real checkpoint, not the plan's own
prose estimate ("a pool of 20 still costs about 7.4 seconds...").

Usage:
    LAYA_MODEL_DIR=/path/to/laya-ingest uv run python -m scripts.benchmark_laya
    LAYA_MODEL_DIR=... uv run python -m scripts.benchmark_laya \\
        --pool-sizes 10,20,40 --runs 5 --results-md eval/results.md

Candidate sourcing (judgment call -- see QUERIES.md and this run's final
report): synthetic-but-realistic `RerankCandidate`s, built through the real
`graph.rerank.candidates_from_hits` -> `graph.text_window.best_window` path
(so windowing behaves exactly as it does in production), over long
synthetic `SearchHit` summaries rather than a live FalkorDB/Qdrant graph.
Chosen for faster, no-data-dependency iteration; a live-graph variant would
mostly change candidate *content*; latency is dominated by token count and
batch size, both of which this script controls directly and sizes to this
plan's own numbers (`graph/chat.py`'s `POOL_SIZE = 40`,
`graph/expand.py`'s `MAX_SEEDS, PER_SEED = 8, 4`, `NEURON_RERANK_BRIDGE_LIMIT
= 4`, so a single bridge-expansion round scores at most `4 * 4 = 16` new
candidates -- comfortably inside the default `--pool-sizes 10,20,40` sweep).

Memory measurement: stdlib-only (`resource.getrusage`), not `psutil` --
`pyproject.toml` has no `psutil` dependency today and this script does not
add one just for a peak-RSS number `resource` already provides.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import resource
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from graph.rerank import (
    DEFAULT_CANDIDATE_WINDOW_TOKENS,
    LayaReranker,
    RerankCandidate,
    candidates_from_hits,
)
from graph.search import SearchHit
from util import paths as _paths  # noqa: F401 -- load repo .env, same as evaluate_retrieval.py

DEFAULT_QUESTION = "What changed in the billing connector last sprint, and who owns the fix?"

# Long enough (rotated/repeated) that a candidate summary comfortably
# exceeds DEFAULT_CANDIDATE_WINDOW_TOKENS (300) once serialized, so
# `best_window` performs real windowing instead of passing text through
# unchanged -- matching `graph/chat.py::_rerank_candidates`, which
# serializes node summary + linked/temporal facts before windowing to the
# same 300-token budget (see its own docstring).
_SENTENCE_BANK = [
    "The billing connector's webhook handler was updated to retry failed "
    "Stripe charge events with exponential backoff after three consecutive "
    "5xx responses from the downstream ledger service.",
    "Ticket BILL-482 tracks the regression where duplicate invoices were "
    "created for annual subscriptions renewing across a month boundary.",
    "Priya Nandakumar is the current owner of the billing connector after "
    "the ownership transfer completed during the September sprint rotation.",
    "The fix touches connectors/billing/webhook.py and adds an idempotency "
    "key derived from the Stripe event id before the charge is persisted.",
    "QA verified the fix against the staging ledger using replayed webhook "
    "payloads captured from the incident, confirming no duplicate rows.",
    "The previous owner, Diego Alvarez, filed the original bug report after "
    "a customer complained about being charged twice in the same billing "
    "cycle.",
    "Release notes for v2.14.0 mention the billing connector fix alongside "
    "unrelated changes to the notifications service and the Jira sync job.",
    "A follow-up task was opened to add a monitoring alert on invoice "
    "duplication rate, owned by the data platform team.",
]


def _synthetic_summary(index: int, sentences: int = 14) -> str:
    """A long, realistic-shaped node summary text -- long enough that
    `best_window` must actually pick a sub-window rather than pass the
    whole thing through unchanged (real `SourceRecord`/`WorkItem` summaries
    in this repo routinely exceed 300 tokens)."""
    picked = [_SENTENCE_BANK[(index + i) % len(_SENTENCE_BANK)] for i in range(sentences)]
    return f"[candidate {index}] " + " ".join(picked)


def synthetic_hits(n: int) -> list[SearchHit]:
    """`n` distinct, realistic-shaped `SearchHit`s standing in for a real
    graph candidate pool -- see module docstring for why synthetic rather
    than a live graph was used here."""
    return [
        SearchHit(
            uid=f"bench-{i}",
            label="WorkItem",
            name=f"BILL-{480 + i}",
            summary=_synthetic_summary(i),
            score=1.0 - i * 0.001,
            methods=["vector"],
        )
        for i in range(n)
    ]


def build_candidates(question: str, n: int) -> list[RerankCandidate]:
    """Real `RerankCandidate`s with real token-bounded windows, via the
    exact `candidates_from_hits` helper `graph/rerank.py` exposes for this
    purpose (25-plan.md §2.1's "freeze the candidate windows before either
    model runs" step) -- not reimplemented here."""
    return candidates_from_hits(
        question, synthetic_hits(n), tokens=DEFAULT_CANDIDATE_WINDOW_TOKENS,
    )


# ---------------------------------------------------------------------------
# Stats helpers -- pure, independent of any model, so they are unit-testable
# without loading Laya (see tests/test_benchmark_laya.py).
# ---------------------------------------------------------------------------


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Matches `scripts/evaluate_retrieval.py`'s own
    `_p90` convention (`math.ceil`-based nearest rank), generalized to an
    arbitrary `pct` so one helper serves both p50 and p90 here."""
    if not values:
        raise ValueError("percentile() requires at least one value")
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil(pct * len(ordered)) - 1)
    return ordered[max(idx, 0)]


def peak_memory_mb() -> float:
    """Peak resident set size of this process so far, in MB.

    `ru_maxrss` units differ by platform: bytes on Darwin, KiB on Linux
    (both are documented `getrusage(2)` behavior, not a bug) -- convert
    both to MB so the report is platform-independent.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return peak / (1024 * 1024)
    return peak / 1024


@dataclass
class PoolMeasurement:
    pool_size: int
    runs: int
    warm_p50_ms: float
    warm_p90_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    throughput_candidates_per_sec: float


def summarize_latencies(pool_size: int, latencies_ms: list[float]) -> PoolMeasurement:
    """Turn one pool size's raw per-call latencies into the reported
    aggregate fields. Separated from `measure_pool` so it can be unit
    tested with fabricated latency lists, no model required."""
    mean_ms = statistics.mean(latencies_ms)
    return PoolMeasurement(
        pool_size=pool_size,
        runs=len(latencies_ms),
        warm_p50_ms=percentile(latencies_ms, 0.5),
        warm_p90_ms=percentile(latencies_ms, 0.9),
        mean_ms=mean_ms,
        min_ms=min(latencies_ms),
        max_ms=max(latencies_ms),
        throughput_candidates_per_sec=(pool_size / (mean_ms / 1000)) if mean_ms else 0.0,
    )


# ---------------------------------------------------------------------------
# Measurement against the real Laya scorer
# ---------------------------------------------------------------------------


def measure_cold_start(reranker: LayaReranker, question: str, pool_size: int) -> float:
    """Time the first-ever `score()` call, including lazy model load
    (`LayaReranker._load`) -- 25-plan.md §2.3's "cold start" metric. Must be
    called exactly once, before any `measure_pool` call, on a fresh
    (never-scored) `reranker` instance."""
    candidates = build_candidates(question, pool_size)
    t0 = time.monotonic()
    reranker.score(question, candidates)
    return (time.monotonic() - t0) * 1000


def measure_pool(
    reranker: LayaReranker, question: str, pool_size: int, runs: int,
) -> PoolMeasurement:
    """Warm p50/p90/throughput at one pool size. Assumes the model is
    already loaded (call `measure_cold_start` once, first)."""
    latencies_ms = []
    for _ in range(runs):
        candidates = build_candidates(question, pool_size)
        t0 = time.monotonic()
        reranker.score(question, candidates)
        latencies_ms.append((time.monotonic() - t0) * 1000)
    return summarize_latencies(pool_size, latencies_ms)


# ---------------------------------------------------------------------------
# Fallback-path verification (25-plan.md §2.3: "On scorer failure or
# timeout, log the model/version and fall back to the Phase 1 RRF order.")
# ---------------------------------------------------------------------------


def verify_fallback_path(question: str) -> dict[str, Any]:
    """Exercise the REAL `graph/chat.py` fallback wiring with a genuinely
    broken (real, non-mocked) `LayaReranker` -- an invalid `LAYA_MODEL_DIR`
    that has no checkpoint files -- rather than re-reading the code and
    asserting the fallback exists.

    Monkeypatches the same minimal set of `graph.chat` internals as
    `tests/test_chat_laya_search.py::
    test_laya_failure_falls_back_to_original_rrf_pool` (the parts that would
    otherwise need a live FalkorDB/Qdrant: structured resolution, embedding,
    hybrid search, the wisdom/finding/pair/person/expansion lanes), but
    supplies a real `LayaReranker` pointed at a nonexistent checkpoint
    directory instead of an always-raising fake scorer -- so the exception
    caught by `graph/chat.py`'s fallback `try/except` is Laya's own real
    `RuntimeError` from `LayaReranker._load()`, not a stand-in for one.
    Restores every patched attribute in `finally`, even on failure.
    """
    from graph import chat

    general_hits = [SearchHit("g1", "WorkItem", "g1", "summary g1", 0.5, ["vector"])]

    def _fake_rerank_candidates(_graph, _question, hits, **_kwargs):
        return [
            RerankCandidate(hit.uid, hit.summary, label=hit.label, name=hit.name, methods=list(hit.methods))
            for hit in hits
        ]

    patches = {
        "resolve_structured": lambda *a, **k: None,
        "embed_query": lambda *a, **k: [0.0],
        "hybrid_search": lambda *a, labels=None, **k: [] if labels else general_hits,
        "_actionable_wisdom_hits": lambda _graph, hits: hits,
        "_linked_finding_hits": lambda *a, **k: [],
        "_two_entity_lane": lambda *a, **k: [],
        "find_named_persons": lambda *a, **k: [],
        "expand_neighbors": lambda *a, **k: [],
        "_rerank_candidates": _fake_rerank_candidates,
    }
    saved = {name: getattr(chat, name) for name in patches}
    for name, fn in patches.items():
        setattr(chat, name, fn)

    try:
        broken_reranker = LayaReranker(model_dir="/nonexistent/laya-checkpoint-does-not-exist")
        trace = chat.RetrievalTrace()
        _structured, hits = chat.retrieve(
            object(), object(), question, limit=2, providers=None,
            scope=object(), reranker=broken_reranker, trace=trace,
        )
        return {
            "fallback_triggered": trace.fallback,
            "reranker_after_fallback": trace.reranker,
            "fallback_reason": trace.fallback_reason,
            "hits_after_fallback": [hit.uid for hit in hits],
        }
    finally:
        for name, fn in saved.items():
            setattr(chat, name, fn)


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _git_sha() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
            cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
    except Exception:
        return "unknown"


def render_summary(report: dict[str, Any]) -> str:
    lines = [
        f"# scripts/benchmark_laya.py -- {report['timestamp']}",
        "",
        f"- git sha: `{report['git_sha']}`",
        f"- model_dir: `{report['model_dir']}`",
        f"- device: `{report['device']}`",
        f"- batch_size: `{report['batch_size']}`",
        f"- model_version: `{report['model_version']}`",
        f"- cold start: {report['cold_start_ms']:.1f} ms (pool size {report['cold_start_pool_size']})",
        f"- peak memory: {report['peak_memory_mb']:.1f} MB",
        "",
        "| pool size | warm p50 (ms) | warm p90 (ms) | mean (ms) | throughput (cand/s) | runs |",
        "|---|---|---|---|---|---|",
    ]
    for pool in report["pools"]:
        lines.append(
            f"| {pool['pool_size']} | {pool['warm_p50_ms']:.1f} | {pool['warm_p90_ms']:.1f} "
            f"| {pool['mean_ms']:.1f} | {pool['throughput_candidates_per_sec']:.2f} | {pool['runs']} |"
        )
    lines += [
        "",
        "## Fallback-path verification",
        "",
        f"- fallback triggered: `{report['fallback']['fallback_triggered']}`",
        f"- reranker after fallback: `{report['fallback']['reranker_after_fallback']}`",
        f"- fallback reason: `{report['fallback']['fallback_reason']}`",
        "",
    ]
    return "\n".join(lines)


def append_results_md(path: Path, section: str) -> None:
    header = (
        "# eval/results.md -- Phase 0 baselines\n\n"
        "One section per harness run, oldest first. Never averaged/combined\n"
        "across datasets or across control/target subsets into one headline\n"
        "number -- each run and each tagged subset gets its own rows.\n\n"
        "---\n\n"
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    with path.open("a") as f:
        f.write(section)
        f.write("\n---\n\n")


def run_benchmark(
    *,
    model_dir: str,
    device: str,
    batch_size: int,
    pool_sizes: list[int],
    runs: int,
    question: str = DEFAULT_QUESTION,
    check_fallback: bool = True,
) -> dict[str, Any]:
    reranker = LayaReranker(model_dir=model_dir, device=device, batch_size=batch_size)
    cold_pool_size = pool_sizes[0]
    cold_start_ms = measure_cold_start(reranker, question, cold_pool_size)

    pools = [measure_pool(reranker, question, size, runs) for size in pool_sizes]

    fallback = verify_fallback_path(question) if check_fallback else None

    return {
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "git_sha": _git_sha(),
        "model_dir": model_dir,
        "device": device,
        "batch_size": batch_size,
        "model_version": reranker._model_version,
        "cold_start_ms": cold_start_ms,
        "cold_start_pool_size": cold_pool_size,
        "pools": [asdict(p) for p in pools],
        "peak_memory_mb": peak_memory_mb(),
        "fallback": fallback,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--pool-sizes", default="10,20,40",
        help="Comma-separated candidate-pool sizes to sweep (default: 10,20,40, "
        "matching graph/chat.py's POOL_SIZE=40 first pass and typical smaller "
        "retry-round sizes).",
    )
    parser.add_argument("--runs", type=int, default=5, help="Warm repetitions per pool size (default: 5).")
    parser.add_argument("--model-dir", default=os.getenv("LAYA_MODEL_DIR"), help="Defaults to $LAYA_MODEL_DIR.")
    parser.add_argument("--device", default=os.getenv("LAYA_DEVICE", "cpu"))
    parser.add_argument("--batch-size", type=int, default=8, help="Matches LayaReranker's own default.")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--skip-fallback-check", action="store_true",
        help="Skip the real-fallback-path verification (it is otherwise always run).",
    )
    parser.add_argument("--output", type=Path, help="Write the JSON report here as well as stdout.")
    parser.add_argument(
        "--results-md", type=Path, default=None,
        help="Append a rendered summary section to this file (e.g. eval/results.md), "
        "matching scripts/evaluate_retrieval.py's --results-md append convention. "
        "Not written unless explicitly passed (conservative default -- see QUERIES).",
    )
    args = parser.parse_args()

    if not args.model_dir:
        parser.error("--model-dir or $LAYA_MODEL_DIR is required (a real Laya checkpoint directory)")

    pool_sizes = [int(x) for x in args.pool_sizes.split(",") if x.strip()]

    report = run_benchmark(
        model_dir=args.model_dir,
        device=args.device,
        batch_size=args.batch_size,
        pool_sizes=pool_sizes,
        runs=args.runs,
        question=args.question,
        check_fallback=not args.skip_fallback_check,
    )

    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)

    summary = render_summary(report)
    print()
    print(summary)

    if args.results_md:
        append_results_md(args.results_md, summary)
        print(f"results.md: {args.results_md}")


if __name__ == "__main__":
    main()
