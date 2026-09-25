from datetime import timedelta

import pytest

from graph.freshness import (
    DECAY_WINDOWS,
    Freshness,
    effective_confidence,
    freshness_class,
)
from graph.time_axis import parse_iso

_NOW = "2026-06-01T00:00:00Z"


def _minus_days(days: float) -> str:
    return (parse_iso(_NOW) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# DECAY_WINDOWS table + defaulting
# ---------------------------------------------------------------------------


def test_decay_windows_table_matches_the_plan():
    assert DECAY_WINDOWS == {"fast": 21, "task": None, "slow": 180, "durable": 365}


@pytest.mark.parametrize(
    "decay_class,window_days",
    [("fast", 21), ("slow", 180), ("durable", 365)],
)
def test_each_day_windowed_class_fresh_just_inside_window(decay_class, window_days):
    last_confirmed = _minus_days(window_days - 1)
    assert freshness_class(decay_class, last_confirmed, now=_NOW) == Freshness.FRESH


@pytest.mark.parametrize(
    "decay_class,window_days",
    [("fast", 21), ("slow", 180), ("durable", 365)],
)
def test_each_day_windowed_class_stale_just_past_window(decay_class, window_days):
    last_confirmed = _minus_days(window_days + 1)
    assert freshness_class(decay_class, last_confirmed, now=_NOW) == Freshness.STALE


@pytest.mark.parametrize("decay_class", [None, "", "other", "bogus", "unknown_value"])
def test_unknown_or_missing_decay_class_defaults_to_slow_180_days(decay_class):
    # 179 days -> still within the 180-day "slow" default window.
    fresh_at = _minus_days(179)
    assert freshness_class(decay_class, fresh_at, now=_NOW) == Freshness.FRESH
    # 181 days -> past it.
    stale_at = _minus_days(181)
    assert freshness_class(decay_class, stale_at, now=_NOW) == Freshness.STALE


# ---------------------------------------------------------------------------
# Fresh/stale boundary: exactly 1.0x the window is stale (inclusive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "decay_class,window_days",
    [("fast", 21), ("slow", 180), ("durable", 365)],
)
def test_boundary_is_inclusive_stale_at_exactly_one_window(decay_class, window_days):
    exactly_at_window = _minus_days(window_days)
    assert freshness_class(decay_class, exactly_at_window, now=_NOW) == Freshness.STALE

    just_under = _minus_days(window_days - 1e-6)
    assert freshness_class(decay_class, just_under, now=_NOW) == Freshness.FRESH


# ---------------------------------------------------------------------------
# Hysteresis: staleness is driven by last_confirmed_at, not by age since
# creation. A fact reconfirmed recently is fresh again no matter how old
# some other (e.g. first_seen_at) timestamp is -- freshness_class never
# even takes such a timestamp as input, which is the point.
# ---------------------------------------------------------------------------


def test_recently_reconfirmed_fact_is_fresh_even_if_very_old():
    # Simulates: first_seen_at was years ago, but new evidence just
    # reconfirmed the fact (a `duplicate` write bumped last_confirmed_at to
    # "now"). The very old creation time plays no role in the computation.
    ancient_first_seen_at = "2015-01-01T00:00:00Z"  # not passed to the function
    just_reconfirmed = _NOW
    assert (
        freshness_class("fast", just_reconfirmed, now=_NOW) == Freshness.FRESH
    ), ancient_first_seen_at  # kept in the test only to document the scenario


def test_stale_fact_does_not_self_heal_by_time_alone():
    # A fact past its window stays stale forever under repeated "now"
    # advances unless last_confirmed_at itself moves -- there is no
    # separate clock that decays back to fresh.
    old = _minus_days(400)  # well past even the durable window
    assert freshness_class("durable", old, now=_NOW) == Freshness.STALE
    later = (parse_iso(_NOW) + timedelta(days=1000)).isoformat()
    assert freshness_class("durable", old, now=later) == Freshness.STALE


# ---------------------------------------------------------------------------
# task-class decay
# ---------------------------------------------------------------------------


def test_task_class_open_work_item_is_fresh():
    assert freshness_class("task", _NOW, now=_NOW, work_item_closed=False) == Freshness.FRESH


def test_task_class_closed_work_item_is_stale():
    assert freshness_class("task", _NOW, now=_NOW, work_item_closed=True) == Freshness.STALE


def test_task_class_unresolved_work_item_status_defaults_to_fresh():
    # No caller-supplied work_item_closed -> documented safe default:
    # treat as still open ("not yet closed until proven otherwise").
    assert freshness_class("task", _NOW, now=_NOW, work_item_closed=None) == Freshness.FRESH


def test_task_class_ignores_last_confirmed_at_age():
    # task-class freshness never falls back to a day-count, however old
    # last_confirmed_at is, as long as the WorkItem is still open.
    assert (
        freshness_class("task", _minus_days(5000), now=_NOW, work_item_closed=False)
        == Freshness.FRESH
    )


# ---------------------------------------------------------------------------
# missing last_confirmed_at
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", [None, ""])
def test_missing_last_confirmed_at_is_stale_not_guessed_fresh(missing):
    assert freshness_class("fast", missing, now=_NOW) == Freshness.STALE


# ---------------------------------------------------------------------------
# effective_confidence
# ---------------------------------------------------------------------------


def test_effective_confidence_fresh_is_unchanged():
    assert effective_confidence(0.8, Freshness.FRESH) == pytest.approx(0.8)


def test_effective_confidence_stale_non_pinned_applies_0_7_factor():
    assert effective_confidence(0.8, Freshness.STALE) == pytest.approx(0.8 * 0.7)


def test_effective_confidence_stale_pinned_is_exempted():
    assert effective_confidence(0.8, Freshness.STALE, pinned=True) == pytest.approx(0.8)


def test_effective_confidence_accepts_plain_strings():
    assert effective_confidence(0.5, "fresh") == pytest.approx(0.5)
    assert effective_confidence(0.5, "stale") == pytest.approx(0.5 * 0.7)


def test_effective_confidence_rejects_unknown_freshness_value():
    with pytest.raises(ValueError):
        effective_confidence(0.5, "kinda_fresh")


# ---------------------------------------------------------------------------
# edge cases: confidence 0.0, 1.0
# ---------------------------------------------------------------------------


def test_effective_confidence_zero_confidence_stays_zero():
    assert effective_confidence(0.0, Freshness.FRESH) == 0.0
    assert effective_confidence(0.0, Freshness.STALE) == 0.0
    assert effective_confidence(0.0, Freshness.STALE, pinned=True) == 0.0


def test_effective_confidence_full_confidence():
    assert effective_confidence(1.0, Freshness.FRESH) == pytest.approx(1.0)
    assert effective_confidence(1.0, Freshness.STALE) == pytest.approx(0.7)
    assert effective_confidence(1.0, Freshness.STALE, pinned=True) == pytest.approx(1.0)
