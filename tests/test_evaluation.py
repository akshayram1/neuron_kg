import argparse

import pytest

from graph.token_usage import TokenUsage
from scripts.evaluate_retrieval import (
    _estimate_usd,
    _parse_edge_triple,
    _p90,
    score_case,
    stage_case_metrics,
    summarize,
    summarize_stage_metrics,
)


def test_retrieval_and_citation_metrics():
    result = score_case(
        {
            "query": "Which commit implements EAPD-1?",
            "expected_uids": ["commit-2"],
            "expected_citation_record_keys": ["github:1:commit:2"],
        },
        ["commit-1", "commit-2"],
        ["github:1:commit:2", "jira:1:work_item:1"],
    )
    summary = summarize([result])
    assert summary["recall_at_k"] == 1.0
    assert summary["precision_at_k"] == 0.5
    assert summary["mrr"] == 0.5
    assert summary["citation_precision"] == 0.5
    assert summary["citation_recall"] == 1.0


def test_empty_expectations_do_not_inflate_metrics():
    summary = summarize([score_case({"query": "placeholder"}, ["x"], [])])
    assert summary["retrieval_cases_scored"] == 0
    assert summary["recall_at_k"] is None
    assert summary["citation_cases_scored"] == 0


# --------------------------------------------------------------------------
# 0.5 — stage metrics
# --------------------------------------------------------------------------


def test_stage_case_metrics_candidate_equals_final_pre_phase2():
    """No wide pool (1.1) and no reranker (2) exist yet, so candidate_uids
    and final_uids are the same cut -- this is the documented pre-Phase-2
    baseline, not a bug."""
    case = {"query": "q", "expected_uids": ["a"]}
    fields = stage_case_metrics(
        case, candidate_uids=["a", "b"], final_uids=["a", "b"], packed_uids=None,
        token_usage=TokenUsage(input_tokens=10, output_tokens=0), usd=0.0002, latency_ms=12.5,
    )
    assert fields["candidate_uids"] == fields["final_uids"] == ["a", "b"]
    assert fields["gold_in_candidates"] is True
    assert fields["gold_in_final"] is True
    assert fields["gold_rank_final"] == 1
    assert fields["packed_uids"] is None
    assert fields["gold_evidence_in_pack"] is None  # not computable without a chat turn


def test_stage_case_metrics_gold_missing_from_final():
    case = {"query": "q", "expected_uids": ["z"]}
    fields = stage_case_metrics(
        case, candidate_uids=["a", "b"], final_uids=["a", "b"], packed_uids=["a", "b"],
        token_usage=TokenUsage(), usd=None, latency_ms=1.0,
    )
    assert fields["gold_in_candidates"] is False
    assert fields["gold_in_final"] is False
    assert fields["gold_rank_final"] is None
    assert fields["gold_evidence_in_pack"] is False


def test_stage_case_metrics_no_gold_is_not_scored():
    """Mirrors score_case's own rule: an empty expectation list is excluded
    from scoring, not counted as a pass."""
    case = {"query": "placeholder"}
    fields = stage_case_metrics(
        case, candidate_uids=["a"], final_uids=["a"], packed_uids=None,
        token_usage=TokenUsage(), usd=None, latency_ms=1.0,
    )
    assert fields["stage_scored"] is False
    assert fields["gold_in_candidates"] is False
    assert fields["gold_in_final"] is False


def test_chain_coverage_defaults_to_zero_without_chain_golden_set():
    case = {"query": "q", "expected_uids": ["a"]}
    fields = stage_case_metrics(
        case, candidate_uids=["a"], final_uids=["a"], packed_uids=None,
        token_usage=TokenUsage(), usd=None, latency_ms=1.0,
    )
    assert fields["chain_coverage"] == 0.0

    case_with_chain = {"query": "q", "expected_uids": ["a"], "chain_uids": ["a", "b", "c"]}
    fields = stage_case_metrics(
        case_with_chain, candidate_uids=["a"], final_uids=["a", "b"], packed_uids=None,
        token_usage=TokenUsage(), usd=None, latency_ms=1.0,
    )
    assert fields["chain_coverage"] == pytest.approx(2 / 3)


def test_summarize_stage_metrics_returns_none_without_stage_data():
    assert summarize_stage_metrics([score_case({"query": "q"}, [])]) is None


def test_summarize_stage_metrics_reports_candidate_and_final_recall_separately():
    case = {"query": "q", "expected_uids": ["a"]}
    hit_row = score_case(case, ["a"])
    hit_row.update(stage_case_metrics(
        case, candidate_uids=["a"], final_uids=["a"], packed_uids=["a"],
        token_usage=TokenUsage(input_tokens=100, output_tokens=20), usd=0.001, latency_ms=50.0,
    ))
    miss_case = {"query": "q2", "expected_uids": ["z"]}
    miss_row = score_case(miss_case, ["a"])
    miss_row.update(stage_case_metrics(
        miss_case, candidate_uids=["a"], final_uids=["a"], packed_uids=["a"],
        token_usage=TokenUsage(input_tokens=200, output_tokens=0), usd=0.002, latency_ms=150.0,
    ))
    stage_summary = summarize_stage_metrics([hit_row, miss_row])
    assert stage_summary["candidate_recall"] == 0.5
    assert stage_summary["final_recall"] == 0.5
    assert stage_summary["median_input_tokens"] == 150
    assert stage_summary["median_latency_ms"] == 100.0


def test_estimate_usd_known_and_unknown_model():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert _estimate_usd(usage, "gpt-5.6-luna") == pytest.approx(0.2 + 1.2)
    assert _estimate_usd(usage, "some-future-model") is None


def test_p90_uses_nearest_rank():
    assert _p90([]) is None
    assert _p90([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]) == 9.0


def test_parse_edge_triple_accepts_valid_and_rejects_malformed():
    assert _parse_edge_triple("uid-1:IMPLEMENTS:uid-2") == "uid-1:IMPLEMENTS:uid-2"
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_edge_triple("not-a-triple")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_edge_triple("uid-1::uid-2")
