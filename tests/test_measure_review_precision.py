"""Tests for `scripts/measure_review_precision.py` (25-plan.md §10.4
promotion-rule measurement: reviewed precision >= 0.95, ECE <= 0.05).

Pure ledger analytics -- a real temp-file `ConnectorLedger` seeded with
synthetic reviews, no live FalkorDB needed, same convention
`tests/test_reviews.py` documents for its own ledger-level tests.
"""

from __future__ import annotations

import math

import pytest

from connectors.core.ledger import ConnectorLedger
from scripts.measure_review_precision import (
    ECE_BAR,
    PRECISION_BAR,
    REVIEW_TYPE_SPECS,
    compute_ece,
    discover_types,
    fetch_decided_reviews,
    score_type,
)


def _seed(ledger: ConnectorLedger, review_type: str, *, approved: int, rejected: int, **payload_extra):
    """Create `approved` + `rejected` decided reviews of `review_type`, each
    with a distinct identity so none collide/dedupe against each other."""
    for i in range(approved):
        rid = ledger.create_review(review_type, dict(payload_extra), identity=f"{review_type}:approved:{i}")
        ledger.approve_review(rid, "akshay.chame@tmdc.io")
    for i in range(rejected):
        rid = ledger.create_review(review_type, dict(payload_extra), identity=f"{review_type}:rejected:{i}")
        ledger.reject_review(rid, "akshay.chame@tmdc.io")


def _seed_with_confidences(ledger: ConnectorLedger, review_type: str, rows: list[tuple[float, bool]]):
    """`rows` is `(confidence, was_approved)`; writes payload['score'] =
    confidence for each, matching possibly_same_as/duplicate_pair's real
    payload key."""
    for i, (confidence, approved) in enumerate(rows):
        rid = ledger.create_review(review_type, {"score": confidence}, identity=f"{review_type}:{i}")
        if approved:
            ledger.approve_review(rid, "akshay.chame@tmdc.io")
        else:
            ledger.reject_review(rid, "akshay.chame@tmdc.io")


# --------------------------------------------------------------- precision


def test_precision_from_known_counts(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(ledger, "fact_update", approved=8, rejected=2)

    decided = fetch_decided_reviews(ledger, "fact_update")
    assert len(decided) == 10
    result = score_type("fact_update", decided, pending_count=0, min_samples=1)
    assert result.approved == 8
    assert result.rejected == 2
    assert result.decided_total == 10
    assert result.precision == pytest.approx(0.8)


def test_precision_reports_raw_counts_not_bare_percentage(tmp_path):
    """A precision of 1.0 from 2 reviews must be reported with its
    denominator, never as if it were meaningful evidence on its own."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(ledger, "fact_update", approved=2, rejected=0)

    decided = fetch_decided_reviews(ledger, "fact_update")
    result = score_type("fact_update", decided, pending_count=0, min_samples=30)
    assert result.precision == pytest.approx(1.0)
    assert result.decided_total == 2
    # Verdict must not be a bare PASS on 2 samples -- see the min-samples
    # gating tests below; this test only pins down that the raw count is
    # preserved on the result object for the caller/report to surface.
    assert result.verdict == "INSUFFICIENT_DATA"


# --------------------------------------------------------------------- ECE


def test_compute_ece_hand_verified_two_bins():
    """Confidences 0.95 (x3: 2 approved, 1 rejected) and 0.55 (x2: 1
    approved, 1 rejected) land in bins 9 and 5 of 10.

    bin 9: mean_confidence=0.95, approval_rate=2/3, gap=0.95-2/3=0.283333...,
           weight=3/5=0.6 -> contributes 0.17
    bin 5: mean_confidence=0.55, approval_rate=0.5, gap=0.05, weight=2/5=0.4
           -> contributes 0.02
    ECE = 0.17 + 0.02 = 0.19
    """
    pairs = [
        (0.95, True), (0.95, True), (0.95, False),
        (0.55, True), (0.55, False),
    ]
    ece, bins = compute_ece(pairs, n_bins=10)
    assert ece == pytest.approx(0.19)

    bin9 = bins[9]
    assert bin9["count"] == 3
    assert bin9["mean_confidence"] == pytest.approx(0.95)
    assert bin9["approval_rate"] == pytest.approx(2 / 3)
    assert bin9["gap"] == pytest.approx(0.95 - 2 / 3)

    bin5 = bins[5]
    assert bin5["count"] == 2
    assert bin5["mean_confidence"] == pytest.approx(0.55)
    assert bin5["approval_rate"] == pytest.approx(0.5)
    assert bin5["gap"] == pytest.approx(0.05)

    empty_bins = [b for i, b in enumerate(bins) if i not in (5, 9)]
    assert all(b["count"] == 0 and b["gap"] is None for b in empty_bins)


def test_compute_ece_perfect_calibration_is_zero():
    # Every prediction exactly matches the bin's actual approval rate.
    pairs = [(0.9, True), (0.9, True), (0.9, False), (0.9, True)]  # 3/4 = 0.75, not 0.9 -- not perfect
    ece, _bins = compute_ece(pairs, n_bins=10)
    assert ece == pytest.approx(0.15)  # |0.9 - 0.75|

    perfect = [(0.5, True), (0.5, False)]  # mean=0.5, approval_rate=0.5
    ece_perfect, _ = compute_ece(perfect, n_bins=10)
    assert ece_perfect == pytest.approx(0.0)


def test_score_type_computes_ece_for_model_probability_field(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    rows = [(0.95, True), (0.95, True), (0.95, False), (0.55, True), (0.55, False)]
    _seed_with_confidences(ledger, "possibly_same_as", rows)

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=1)
    assert result.ece_status == "computed"
    assert result.ece_value == pytest.approx(0.19)
    assert result.ece_scored == 5
    assert result.ece_skipped_missing == 0


def test_score_type_marks_heuristic_score_as_heuristic_only(tmp_path):
    """duplicate_pair's payload['score'] is a hand-weighted combiner, not a
    Laya-trained probability -- ECE is computed for visibility but flagged,
    and must never produce a PASS verdict."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    # Well-calibrated AND high precision on paper -- should still not PASS.
    rows = [(0.99, True)] * 95 + [(0.99, False)] * 5  # precision 0.95, ece small
    _seed_with_confidences(ledger, "duplicate_pair", rows)

    decided = fetch_decided_reviews(ledger, "duplicate_pair")
    result = score_type("duplicate_pair", decided, pending_count=0, min_samples=30)
    assert result.ece_status == "heuristic_only"
    assert result.ece_value is not None
    assert result.precision == pytest.approx(0.95)
    assert result.verdict == "ECE_NOT_COMPUTABLE"


# ------------------------------------------------------- honest no-field path


def test_fact_update_has_no_confidence_field_and_says_so(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(
        ledger, "fact_update", approved=190, rejected=10,
        action="newer_state_close", old_fact_uid="f1", new_from_uid="a", new_rel_type="R", new_to_uid="b",
    )

    decided = fetch_decided_reviews(ledger, "fact_update")
    result = score_type("fact_update", decided, pending_count=0, min_samples=200)
    assert result.ece_status == "no_confidence_field"
    assert result.ece_value is None
    assert result.spec is REVIEW_TYPE_SPECS["fact_update"]
    assert result.spec.confidence_key is None
    # Explicit, not silent: the verdict says ECE could not be computed,
    # never a bare PASS/FAIL that would imply the ECE bar was checked.
    assert result.verdict == "ECE_NOT_COMPUTABLE"


def test_unrecognized_review_type_gets_precision_only(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(ledger, "some_future_type", approved=5, rejected=5)

    decided = fetch_decided_reviews(ledger, "some_future_type")
    result = score_type("some_future_type", decided, pending_count=0, min_samples=1)
    assert result.spec is None
    assert result.ece_status == "no_confidence_field"
    assert result.precision == pytest.approx(0.5)


def test_confidence_field_present_but_missing_on_every_row(tmp_path):
    """A type configured with a confidence_key whose payload happens not to
    carry it on any decided row (e.g. old data written before the field
    existed) reports insufficient_confidence_values, not a fabricated 0.0."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(ledger, "possibly_same_as", approved=3, rejected=1)  # no "score" key at all

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=1)
    assert result.ece_status == "insufficient_confidence_values"
    assert result.ece_value is None
    assert result.ece_skipped_missing == 4


# --------------------------------------------------------------- gating


def test_min_samples_gating_below_threshold_is_insufficient_data(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed_with_confidences(ledger, "possibly_same_as", [(0.99, True)] * 29)  # precision 1.0, perfect calibration

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=30)
    assert result.decided_total == 29
    assert result.verdict == "INSUFFICIENT_DATA"


def test_min_samples_gating_at_threshold_is_scored(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed_with_confidences(ledger, "possibly_same_as", [(0.99, True)] * 30)

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=30)
    assert result.decided_total == 30
    assert result.verdict == "PASS"


# --------------------------------------------------------------- verdicts


def test_verdict_pass_requires_both_precision_and_ece_bars(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    # precision exactly at bar, near-perfect calibration.
    rows = [(0.97, True)] * 194 + [(0.97, False)] * 6  # precision = 194/200 = 0.97
    _seed_with_confidences(ledger, "possibly_same_as", rows)

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=200)
    assert result.precision == pytest.approx(0.97)
    assert result.precision >= PRECISION_BAR
    assert result.ece_value <= ECE_BAR
    assert result.verdict == "PASS"


def test_verdict_fail_on_low_precision_even_with_perfect_calibration(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    rows = [(0.5, True)] * 100 + [(0.5, False)] * 100  # precision 0.5, perfectly calibrated at 0.5
    _seed_with_confidences(ledger, "possibly_same_as", rows)

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=200)
    assert result.precision == pytest.approx(0.5)
    assert result.ece_value == pytest.approx(0.0)
    assert result.verdict == "FAIL"


def test_verdict_fail_on_poor_calibration_even_with_high_precision(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    # precision 0.96 (high) but every prediction claims 0.5 confidence while
    # actually approving 96% of the time -- badly miscalibrated.
    rows = [(0.5, True)] * 192 + [(0.5, False)] * 8
    _seed_with_confidences(ledger, "possibly_same_as", rows)

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=200)
    assert result.precision == pytest.approx(0.96)
    assert result.ece_value == pytest.approx(0.46)
    assert result.ece_value > ECE_BAR
    assert result.verdict == "FAIL"


def test_decision_label_is_flagged_but_not_dropped_from_the_math(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    for i in range(200):
        rid = ledger.create_review(
            "possibly_same_as", {"score": 0.99, "label": "Decision"}, identity=f"d:{i}",
        )
        ledger.approve_review(rid, "akshay.chame@tmdc.io")

    decided = fetch_decided_reviews(ledger, "possibly_same_as")
    result = score_type("possibly_same_as", decided, pending_count=0, min_samples=200)
    assert result.decision_label_decided == 200
    assert result.decided_total == 200
    # The math still runs (this script never silently drops real decided
    # outcomes) -- callers/report text carry the "permanently excluded"
    # warning instead.
    assert result.precision == pytest.approx(1.0)


# ----------------------------------------------------------- discovery


def test_discover_types_finds_every_decided_type(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    _seed(ledger, "fact_update", approved=1, rejected=1)
    _seed(ledger, "possibly_same_as", approved=1, rejected=0)
    # A pending-only type must not show up (nothing decided yet).
    ledger.create_review("duplicate_pair", {"score": 0.7}, identity="pending-only")

    types = discover_types(ledger)
    assert types == ["fact_update", "possibly_same_as"]


def test_link_candidate_is_not_in_review_type_specs_or_reachable_via_list_reviews(tmp_path):
    """link_candidate proposals never go through ledger.create_review, so
    they can never appear in discover_types/list_reviews regardless of how
    many exist in the separate link_candidates table."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.create_link_candidate("a", "b", "RELATES_TO", derived_rule="two_hop", confidence=0.5)

    assert "link_candidate" not in REVIEW_TYPE_SPECS
    assert discover_types(ledger) == []
    assert fetch_decided_reviews(ledger, "link_candidate") == []
