"""Fast, offline unit tests for scripts/benchmark_laya.py's measurement and
report-formatting logic. No real Laya model is loaded here -- that requires
a real ~800MB checkpoint and is exercised manually via the script's CLI
(see the module docstring), never as part of the default `pytest` run.
"""

from __future__ import annotations

import math

import pytest

from graph.rerank import DEFAULT_CANDIDATE_WINDOW_TOKENS
from scripts.benchmark_laya import (
    PoolMeasurement,
    build_candidates,
    peak_memory_mb,
    percentile,
    render_summary,
    summarize_latencies,
    synthetic_hits,
)


# --- candidate generation ----------------------------------------------------

def test_synthetic_hits_produces_distinct_uids_and_long_summaries():
    hits = synthetic_hits(5)
    assert len(hits) == 5
    assert len({hit.uid for hit in hits}) == 5
    assert all(len(hit.summary) > 200 for hit in hits)


def test_build_candidates_windows_real_long_text_via_best_window():
    candidates = build_candidates("what changed in billing?", 3)
    assert len(candidates) == 3
    # Windowed text must fit the same token budget used in production
    # (graph/chat.py's _rerank_candidates), not just be shorter than input.
    import tiktoken

    encoding = tiktoken.get_encoding("cl100k_base")
    for candidate in candidates:
        assert len(encoding.encode(candidate.window)) <= DEFAULT_CANDIDATE_WINDOW_TOKENS


# --- percentile / stats ------------------------------------------------------

def test_percentile_matches_nearest_rank_p50_and_p90():
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert percentile(values, 0.5) == 50.0
    idx = min(len(values) - 1, math.ceil(0.9 * len(values)) - 1)
    assert percentile(values, 0.9) == sorted(values)[idx]


def test_percentile_is_order_independent():
    values = [30.0, 10.0, 20.0]
    assert percentile(values, 0.5) == percentile(sorted(values), 0.5)


def test_percentile_rejects_empty_input():
    with pytest.raises(ValueError):
        percentile([], 0.5)


def test_summarize_latencies_computes_expected_aggregate_fields():
    measurement = summarize_latencies(20, [100.0, 200.0, 300.0, 400.0, 500.0])
    assert isinstance(measurement, PoolMeasurement)
    assert measurement.pool_size == 20
    assert measurement.runs == 5
    assert measurement.mean_ms == 300.0
    assert measurement.min_ms == 100.0
    assert measurement.max_ms == 500.0
    # throughput = pool_size / (mean_seconds)
    assert measurement.throughput_candidates_per_sec == pytest.approx(20 / 0.3)


def test_summarize_latencies_single_run_has_equal_p50_p90_mean():
    measurement = summarize_latencies(10, [123.0])
    assert measurement.warm_p50_ms == measurement.warm_p90_ms == measurement.mean_ms == 123.0


# --- peak memory --------------------------------------------------------------

def test_peak_memory_mb_returns_a_positive_number():
    # Any running Python process has nonzero RSS -- this just checks the
    # unit conversion produces a sane positive MB figure, not an exact value.
    assert peak_memory_mb() > 0


# --- report rendering ----------------------------------------------------------

def _fake_report() -> dict:
    return {
        "timestamp": "2026-09-28 00:00 UTC",
        "git_sha": "abc1234",
        "model_dir": "/fake/model/dir",
        "device": "cpu",
        "batch_size": 8,
        "model_version": "deadbeefcafe",
        "cold_start_ms": 1234.5,
        "cold_start_pool_size": 10,
        "pools": [
            {
                "pool_size": 10, "runs": 3, "warm_p50_ms": 100.0, "warm_p90_ms": 150.0,
                "mean_ms": 110.0, "min_ms": 90.0, "max_ms": 150.0,
                "throughput_candidates_per_sec": 90.9,
            },
        ],
        "peak_memory_mb": 512.3,
        "fallback": {
            "fallback_triggered": True,
            "reranker_after_fallback": "rrf",
            "fallback_reason": "RuntimeError: LAYA_MODEL_DIR is required when NEURON_RERANK=laya",
            "hits_after_fallback": ["g1"],
        },
    }


def test_render_summary_includes_cold_start_and_pool_rows():
    summary = render_summary(_fake_report())
    assert "1234.5 ms" in summary
    assert "| 10 | 100.0 | 150.0 | 110.0 | 90.90 | 3 |" in summary


def test_render_summary_includes_fallback_verification_section():
    summary = render_summary(_fake_report())
    assert "fallback triggered: `True`" in summary
    assert "reranker after fallback: `rrf`" in summary
