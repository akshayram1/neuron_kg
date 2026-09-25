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

`--stage-metrics` (plan.md §0.5) additionally records, per question:
candidate_uids/final_uids (pre/post rerank -- identical until Phase 2's
reranker exists), packed_uids (evidence that reached the answer prompt,
`--with-chat` only), gold_in_candidates/gold_in_final/gold_evidence_in_pack,
gold_rank_final, chain_coverage, and token/cost/latency. It writes a
per-question JSONL next to the dataset and rolls medians/aggregates into
`--results-md` (default `eval/results.md`).

`--exclude-edges` is accepted but currently a no-op -- see its help text.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from openai import OpenAI

from graph import multigraph, vector_store
from graph.access import AccessScope
from graph.chat import RetrievalTrace, retrieve, run_chat_turn
from graph.falkor_client import get_graph
from graph.token_usage import TokenUsage
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


# --------------------------------------------------------------------------
# 0.5 — stage metrics (plan.md §0.5)
# --------------------------------------------------------------------------

# Local to the harness: graph/*.py has no pricing table today and this
# file's scope forbids adding one there. Source: cost.md's "Pricing (fetched
# from developers.openai.com, 2026-09-10)" table. (input $/1M, output $/1M).
_TOKEN_PRICE_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gpt-5.6-sol": (4.0, 20.0),
    "gpt-5.6-luna": (0.2, 1.2),
    "text-embedding-3-small": (0.02, 0.0),
}


def _estimate_usd(usage: TokenUsage, model: str) -> float | None:
    prices = _TOKEN_PRICE_USD_PER_1M.get(model)
    if prices is None:
        return None
    input_rate, output_rate = prices
    return usage.input_tokens * input_rate / 1_000_000 + usage.output_tokens * output_rate / 1_000_000


def stage_case_metrics(
    case: dict[str, Any],
    *,
    candidate_uids: list[str],
    final_uids: list[str],
    packed_uids: list[str] | None,
    token_usage: TokenUsage,
    usd: float | None,
    latency_ms: float,
    packed_blocks: list | None = None,
) -> dict[str, Any]:
    """Per-question stage fields (plan.md §0.5).

    `candidate_uids` is everything `retrieve()` returned before any cut;
    `final_uids` is what reached the answer prompt. As of Phase 1.1's wide
    pool + Phase 1.3's expansion, these now genuinely differ: `retrieve()`
    returns the full pool (default 40, plus any expansion/pair-lane
    additions), and the caller (this harness, mirroring `run_chat_turn`)
    cuts to the top `k`. Before Phase 1 landed they were identical, per
    plan.md §8 Phase 1.1's explicit note that behaviour would not change
    until then.

    `packed_uids`/`packed_blocks` are only meaningful with `--with-chat` (no
    chat turn, no pack). `packed_blocks` is `ChatResult.packed_blocks`
    (`graph/chat.py`'s `PackedBlockInfo` list, plan.md §1.2) when available:
    real per-block packing honesty (`truncated`, `gold_support_preserved`),
    not just a node-presence proxy. When it's not available (older callers,
    or `--with-chat` not set), `gold_evidence_in_pack` falls back to "gold
    uid is present in `packed_uids`" -- a coarser proxy that can't tell a
    fully-packed block from one whose evidence was truncated to fit.
    """
    expected = set(case.get("expected_uids") or [])
    stage_scored = bool(expected)
    ranks = [final_uids.index(uid) + 1 for uid in expected if uid in final_uids]
    chain_uids = list(case.get("chain_uids") or [])
    chain_coverage = (
        len(set(chain_uids) & set(final_uids)) / len(chain_uids) if chain_uids else 0.0
    )
    if packed_blocks is not None:
        # A gold node's evidence only counts as preserved if its block both
        # made it into the pack AND wasn't truncated to fit the budget --
        # `PackedBlockInfo.gold_support_preserved` is exactly that signal,
        # just not yet compared against this case's own `expected_uids`
        # (graph/chat.py has no access to gold data -- see its docstring).
        preserved_uids = {b.uid for b in packed_blocks if b.gold_support_preserved}
        gold_evidence_in_pack = bool(expected & preserved_uids) if stage_scored else None
    else:
        gold_evidence_in_pack = bool(expected & set(packed_uids)) if packed_uids is not None else None
    return {
        "candidate_uids": candidate_uids,
        "final_uids": final_uids,
        "packed_uids": packed_uids,
        "stage_scored": stage_scored,
        "gold_in_candidates": bool(expected & set(candidate_uids)) if stage_scored else False,
        "gold_in_final": bool(expected & set(final_uids)) if stage_scored else False,
        "gold_evidence_in_pack": gold_evidence_in_pack,
        "gold_rank_final": min(ranks) if ranks else None,
        "chain_coverage": chain_coverage,
        "input_tokens": token_usage.input_tokens,
        "output_tokens": token_usage.output_tokens,
        "usd": usd,
        "latency_ms": latency_ms,
    }


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)
    return ordered[idx]


def summarize_stage_metrics(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Aggregate the fields `stage_case_metrics` adds. Returns None when the
    run did not collect stage metrics (`--stage-metrics` was not passed), so
    callers can tell "no stage data" apart from "stage data, all empty"."""
    staged = [r for r in results if "candidate_uids" in r]
    if not staged:
        return None
    scored = [r for r in staged if r["stage_scored"]]
    evidence_scored = [r for r in staged if r.get("gold_evidence_in_pack") is not None]
    tokens_in = [r["input_tokens"] for r in staged]
    tokens_out = [r["output_tokens"] for r in staged]
    usd_values = [r["usd"] for r in staged if r.get("usd") is not None]
    latencies = [r["latency_ms"] for r in staged]
    return {
        "stage_cases": len(staged),
        "stage_cases_scored": len(scored),
        # Presence-based (did the answer node ever appear), distinct from
        # `recall_at_k`'s fraction-of-expected-uids-found -- report both,
        # never conflated (plan.md §0.5).
        "candidate_recall": mean(1.0 if r["gold_in_candidates"] else 0.0 for r in scored) if scored else None,
        "final_recall": mean(1.0 if r["gold_in_final"] else 0.0 for r in scored) if scored else None,
        "mrr_final": mean(r["reciprocal_rank"] for r in scored) if scored else None,
        "chain_coverage_mean": mean(r["chain_coverage"] for r in staged) if staged else None,
        "gold_evidence_cases_scored": len(evidence_scored),
        "gold_evidence_recall": (
            mean(1.0 if r["gold_evidence_in_pack"] else 0.0 for r in evidence_scored)
            if evidence_scored else None
        ),
        "median_input_tokens": median(tokens_in) if tokens_in else None,
        "p90_input_tokens": _p90(tokens_in),
        "median_output_tokens": median(tokens_out) if tokens_out else None,
        "p90_output_tokens": _p90(tokens_out),
        "median_usd": median(usd_values) if usd_values else None,
        "total_usd": sum(usd_values) if usd_values else None,
        "median_latency_ms": median(latencies) if latencies else None,
        "p90_latency_ms": _p90(latencies),
    }


def _parse_edge_triple(raw: str) -> str:
    # TODO(phase 1.3): once graph/expand.py adds
    # `expand_neighbors(..., exclude_edges=...)` and `retrieve()` threads it
    # through, parse this into a real (from_uid, rel, to_uid) tuple and pass
    # it down. `retrieve()` has no `exclude_edges` parameter yet -- there is
    # no expansion step (graph/expand.py does not exist) for it to affect --
    # so today this is validated and recorded only, never applied. Kept as a
    # plain "from_uid:REL:to_uid" string (not parsed into a tuple) since
    # nothing consumes the parsed form yet.
    parts = raw.split(":")
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError(
            f"--exclude-edges expects 'from_uid:REL:to_uid', got {raw!r}"
        )
    return raw


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parent.parent,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _graph_counts(graph) -> tuple[int, int]:
    nodes = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    edges = graph.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    return nodes, edges


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _render_group(name: str, summary: dict[str, Any], stage: dict[str, Any] | None) -> str:
    lines = [f"**{name}** ({summary['cases']} cases, {summary['retrieval_cases_scored']} scored)", ""]
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append(f"| recall@k | {_fmt(summary['recall_at_k'])} |")
    lines.append(f"| precision@k | {_fmt(summary['precision_at_k'])} |")
    lines.append(f"| MRR | {_fmt(summary['mrr'])} |")
    if summary.get("citation_cases_scored"):
        lines.append(f"| citation precision | {_fmt(summary['citation_precision'])} |")
        lines.append(f"| citation recall | {_fmt(summary['citation_recall'])} |")
    if stage is not None:
        lines.append(f"| candidate recall (presence) | {_fmt(stage['candidate_recall'])} |")
        lines.append(f"| final recall (presence) | {_fmt(stage['final_recall'])} |")
        lines.append(f"| rerank gap (candidate − final) | {_fmt((stage['candidate_recall'] or 0) - (stage['final_recall'] or 0))} |")
        lines.append(f"| chain coverage (mean) | {_fmt(stage['chain_coverage_mean'])} (no chain golden set wired in yet — always 0) |")
        if stage["gold_evidence_cases_scored"]:
            lines.append(f"| gold evidence in pack (recall) | {_fmt(stage['gold_evidence_recall'])} |")
        lines.append(f"| median input tokens | {_fmt(stage['median_input_tokens'], 0)} |")
        lines.append(f"| p90 input tokens | {_fmt(stage['p90_input_tokens'], 0)} |")
        lines.append(f"| median output tokens | {_fmt(stage['median_output_tokens'], 0)} |")
        lines.append(f"| p90 output tokens | {_fmt(stage['p90_output_tokens'], 0)} |")
        lines.append(f"| median $/question | {_fmt(stage['median_usd'], 6)} |")
        lines.append(f"| total $ (this run) | {_fmt(stage['total_usd'], 4)} |")
        lines.append(f"| median latency (ms) | {_fmt(stage['median_latency_ms'], 1)} |")
        lines.append(f"| p90 latency (ms) | {_fmt(stage['p90_latency_ms'], 1)} |")
    lines.append("")
    return "\n".join(lines)


def render_results_section(
    *, dataset: Path, target: multigraph.GraphTarget, args: argparse.Namespace,
    node_count: int, edge_count: int, model_used: str,
    groups: list[tuple[str, list[dict[str, Any]]]],
) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    flags = [f"--k {args.k}", f"--graph {args.graph}"]
    if args.with_chat:
        flags.append("--with-chat")
    if args.stage_metrics:
        flags.append("--stage-metrics")
    if args.exclude_edges:
        flags.append("--exclude-edges " + " --exclude-edges ".join(args.exclude_edges))
    lines = [
        f"## {dataset.name} — {now}",
        "",
        f"- git sha: `{_git_sha()}`",
        f"- dataset: `{dataset}`",
        f"- graph: `{target.name}` → falkor=`{target.falkor_name}` ({node_count} nodes, {edge_count} edges), "
        f"qdrant=`{target.qdrant_collection}`",
        f"- flags: `{' '.join(flags)}`",
        f"- model: `{model_used}`" + (" (chat answer synthesis)" if args.with_chat else " (embedding only — --with-chat not set)"),
        "",
    ]
    for name, results in groups:
        summary = summarize(results)
        stage = summarize_stage_metrics(results) if args.stage_metrics else None
        lines.append(_render_group(name, summary, stage))
    return "\n".join(lines)


def append_results_md(path: Path, section: str) -> None:
    header = (
        "# eval/results.md — Phase 0 baselines\n\n"
        "One section per harness run, oldest first. Never averaged/combined\n"
        "across datasets or across control/target subsets into one headline\n"
        "number — each run and each tagged subset gets its own rows.\n\n"
        "---\n\n"
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    with path.open("a") as f:
        f.write(section)
        f.write("\n---\n\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--with-chat", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--graph", default=multigraph.DEFAULT_GRAPH_NAME)
    parser.add_argument(
        "--stage-metrics", action="store_true",
        help="Record candidate/final/packed uids, gold presence+rank, chain "
        "coverage, tokens/cost/latency per question (plan.md §0.5). Writes "
        "a per-question JSONL next to the dataset and appends an aggregate "
        "section to --results-md.",
    )
    parser.add_argument(
        "--stage-output", type=Path,
        help="Per-question stage-metrics JSONL path. Default: "
        "<dataset>.stage.jsonl next to the dataset.",
    )
    parser.add_argument(
        "--results-md", type=Path, default=Path("eval/results.md"),
        help="Where --stage-metrics appends its aggregate section. Created "
        "if missing.",
    )
    parser.add_argument(
        "--exclude-edges", action="append", type=_parse_edge_triple, default=None,
        metavar="from_uid:REL:to_uid",
        help="Edge(s) to hide from expansion, for the hidden-edge test "
        "(plan.md §0.2/1.3). Threaded into retrieve()/run_chat_turn() as a "
        "frozenset of (from_uid, rel, to_uid) tuples, which graph/expand.py "
        "skips during one-hop expansion.",
    )
    args = parser.parse_args()

    exclude_edges = list(dict.fromkeys(args.exclude_edges or []))
    # `_parse_edge_triple` validates shape but keeps the raw "from:REL:to"
    # string (see its docstring) since nothing consumed the parsed form
    # until graph/expand.py + graph/chat.py's Phase 1.3 wiring landed. Split
    # it into the (from_uid, rel, to_uid) tuples expand_neighbors() expects.
    exclude_edges_frozenset = frozenset(
        tuple(raw.split(":", 2)) for raw in exclude_edges
    )

    target = resolve_target(args.graph)
    graph = get_graph(name=target.falkor_name)
    node_count, edge_count = _graph_counts(graph)
    client = OpenAI()
    scope = AccessScope.trusted_internal()
    chat_model = os.getenv("CHAT_MODEL", "gpt-5.6-sol")
    embedding_model = vector_store.EMBEDDING_MODEL
    model_used = chat_model if args.with_chat else embedding_model

    results = []
    for case in load_cases(args.dataset):
        providers = case.get("providers")
        case_token_usage = TokenUsage()
        t0 = time.monotonic()
        # `retrieve`, not `hybrid_search`: the product composes the ranker with
        # the structured resolver, the name matcher and the time window. Scoring
        # the ranker alone measured a narrower path than ships -- three nilus
        # cases read 0 while answering correctly in chat.
        retrieval_trace = RetrievalTrace()
        _structured, hits = retrieve(
            graph, client, case["query"], limit=args.k,
            providers=providers, scope=scope,
            collection=target.qdrant_collection,
            token_usage=case_token_usage,
            exclude_edges=exclude_edges_frozenset,
            trace=retrieval_trace,
        )
        retrieval_ms = (time.monotonic() - t0) * 1000
        # Phase 1.1's wide pool means `retrieve()` no longer implicitly cuts
        # to `args.k` -- it now returns the full candidate pool (plus any
        # expansion/pair-lane additions) so candidate recall@k can be
        # measured. `run_chat_turn` applies its own `hits[:search_limit]` cut
        # after pool+expansion+pair-lane are all built (graph/chat.py); mirror
        # that same cut here for the non-chat path so `final_uids` means what
        # its name says instead of silently becoming the uncut pool.
        if retrieval_trace.reranker == "laya" and not retrieval_trace.fallback:
            candidate_uids = list(dict.fromkeys(
                retrieval_trace.initial_candidate_uids
                + retrieval_trace.expanded_candidate_uids
            ))
            final_uids = retrieval_trace.final_uids
        else:
            candidate_uids = [hit.uid for hit in hits]
            final_uids = candidate_uids[: args.k]

        citations = None
        packed_uids: list[str] | None = None
        packed_blocks = None
        stage_token_usage, stage_model, stage_latency_ms = case_token_usage, embedding_model, retrieval_ms
        if args.with_chat:
            t1 = time.monotonic()
            answer = run_chat_turn(
                graph, client, case["query"], search_limit=args.k,
                providers=providers, scope=scope,
                collection=target.qdrant_collection,
                exclude_edges=exclude_edges_frozenset,
            )
            chat_ms = (time.monotonic() - t1) * 1000
            citations = [item.record_key for item in answer.citations]
            # Keep insertion order but de-dup: `highlighted_nodes` is built
            # from `packed_blocks` (graph/chat.py's actual post-packing
            # survivors), so this is already the packed set, not a proxy.
            packed_uids = list(dict.fromkeys(answer.highlighted_nodes))
            packed_blocks = answer.packed_blocks
            # Whole-turn cost/latency (this already includes the internal
            # retrieve() call `run_chat_turn` makes) -- not added to
            # `case_token_usage`/`retrieval_ms` above, to avoid double
            # counting the embedding call in both numbers.
            stage_token_usage, stage_model, stage_latency_ms = answer.token_usage, chat_model, chat_ms

        result = score_case(case, final_uids, citations)
        if retrieval_trace.reranker is not None:
            result.update({
                "reranker": retrieval_trace.reranker,
                "rerank_fallback": retrieval_trace.fallback,
                "rerank_expansion_rounds": retrieval_trace.expansion_rounds,
                "rerank_bridge_uids": retrieval_trace.bridge_uids,
                "rerank_initial_candidate_uids": retrieval_trace.initial_candidate_uids,
                "rerank_expanded_candidate_uids": retrieval_trace.expanded_candidate_uids,
            })
        if args.stage_metrics:
            usd = _estimate_usd(stage_token_usage, stage_model)
            result.update(stage_case_metrics(
                case, candidate_uids=candidate_uids, final_uids=final_uids,
                packed_uids=packed_uids, packed_blocks=packed_blocks,
                token_usage=stage_token_usage, usd=usd,
                latency_ms=stage_latency_ms,
            ))
        if "kind" in case:
            result["kind"] = case["kind"]
        results.append(result)

    payload: dict[str, Any] = {
        "graph": target.name, "summary": summarize(results), "results": results,
    }
    if exclude_edges:
        payload["exclude_edges"] = exclude_edges  # recorded only -- see --exclude-edges help
    if args.stage_metrics:
        payload["stage_summary"] = summarize_stage_metrics(results)
        kinds = sorted({r["kind"] for r in results if "kind" in r})
        if kinds:
            payload["stage_summary_by_kind"] = {
                kind: summarize_stage_metrics([r for r in results if r.get("kind") == kind])
                for kind in kinds
            }

    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n")
    print(rendered)

    if args.stage_metrics:
        stage_output = args.stage_output or args.dataset.with_name(args.dataset.stem + ".stage.jsonl")
        with stage_output.open("w") as f:
            for result in results:
                f.write(json.dumps(result, sort_keys=True) + "\n")
        print(f"stage metrics: {stage_output}")

        kinds = sorted({r["kind"] for r in results if "kind" in r})
        groups: list[tuple[str, list[dict[str, Any]]]] = [("all", results)]
        for kind in kinds:
            groups.append((kind, [r for r in results if r.get("kind") == kind]))
        section = render_results_section(
            dataset=args.dataset, target=target, args=args,
            node_count=node_count, edge_count=edge_count, model_used=model_used,
            groups=groups,
        )
        append_results_md(args.results_md, section)
        print(f"results.md: {args.results_md}")


if __name__ == "__main__":
    main()
