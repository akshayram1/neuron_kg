"""A month is an interval, not its first instant.

The live bug: "What was worked on in August 2026?" set world time to the
single moment 2026-08-01T00:00 and the answer came back "the latest recorded
activity was 2026-07-28" — true, and useless, because at midnight on the 1st
nothing in August had happened yet. The same question phrased without the
month token returned 28 commits from the identical graph.
"""

from __future__ import annotations

from graph.axioms import DEFAULT_AXIOMS
from graph.time_axis import holds_at, infer_query_clocks, infer_query_window, parse_iso

AUG = parse_iso("2026-08-01T00:00:00+00:00")
SEP = parse_iso("2026-09-01T00:00:00+00:00")


def test_a_month_parses_to_the_whole_month():
    assert infer_query_window("What was worked on in August 2026?") == (
        "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00", None,
    )


def test_december_rolls_into_the_next_year():
    at, end, _ = infer_query_window("what shipped in December 2026")
    assert (at, end) == ("2026-12-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")


def test_the_way_people_actually_write_a_sprint_month():
    """`aug'26` was read as no date at all, so the question that started this
    ("what's the plan for aug'26 sprint") got no clock and no window."""
    for phrase in ("what's the plan for aug'26 sprint", "Aug '26 progress",
                   "August ’26 progress"):
        assert infer_query_window(phrase)[:2] == (
            "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00"), phrase
    assert infer_query_window("what shipped in dec'25")[:2] == (
        "2025-12-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")


def test_a_bare_month_and_number_is_not_a_year():
    """`aug 26` is the 26th, not 2026 — the apostrophe is what makes it a
    year, so the two-digit form must not match without it."""
    assert infer_query_window("meeting on aug 26") == (None, None, None)


def test_precision_sets_the_width():
    """Less precision, wider window — a bare year is a year, a bare day a day."""
    assert infer_query_window("what changed in 2026")[:2] == (
        "2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")
    assert infer_query_window("2026-08")[:2] == (
        "2026-08-01T00:00:00+00:00", "2026-09-01T00:00:00+00:00")
    assert infer_query_window("who was assigned on 2026-03-14")[:2] == (
        "2026-03-14T00:00:00+00:00", "2026-03-15T00:00:00+00:00")


def test_an_explicit_time_stays_an_instant():
    assert infer_query_window("state at 2026-03-14T09:30")[1] is None


def test_the_record_axis_stays_a_point():
    """"What did we believe in August" reads the ledger as it stood when the
    month opened. A belief window would be a different question."""
    at, end, as_of = infer_query_window("what did we know about Argus in January 2026")
    assert (at, end) == (None, None)
    assert as_of == "2026-01-01T00:00:00+00:00"


def test_a_ticket_key_is_still_not_a_date():
    assert infer_query_window("for DATAOS-4346 ticket what has been done") == (None, None, None)


def test_the_point_api_is_unchanged_for_existing_callers():
    assert infer_query_clocks("who was assigned in March 2026") == (
        "2026-03-01T00:00:00+00:00", None)


# --- the predicate itself ------------------------------------------------

def test_the_bug_an_august_event_is_invisible_to_an_august_1_snapshot():
    commit = ("2026-08-15T00:00:00+00:00", None)
    assert holds_at(*commit, AUG) is False                       # what shipped
    assert holds_at(*commit, AUG, at_end=SEP, temporal="event")  # what we want


def test_a_july_event_does_not_leak_into_an_august_window():
    """An open end says nothing about when an event happened, so an event is
    never widened to fill the window — otherwise every earlier commit would
    answer every later month."""
    july = ("2026-07-28T00:00:00+00:00", None)
    assert holds_at(*july, AUG, at_end=SEP, temporal="event") is False


def test_a_state_need_only_overlap_the_window():
    """An assignment opened in January and still open in August did hold
    during August, unlike a commit made in January."""
    ongoing = ("2026-01-13T00:00:00+00:00", None)
    assert holds_at(*ongoing, AUG, at_end=SEP, temporal="state") is True
    assert holds_at(*ongoing, AUG, at_end=SEP, temporal="event") is False


def test_a_state_that_closed_before_the_window_is_excluded():
    closed = ("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00")
    assert holds_at(*closed, AUG, at_end=SEP, temporal="state") is False


def test_a_state_that_closes_inside_the_window_is_included():
    closing = ("2026-01-01T00:00:00+00:00", "2026-08-14T00:00:00+00:00")
    assert holds_at(*closing, AUG, at_end=SEP, temporal="state") is True


def test_an_undated_fact_is_still_excluded_from_a_dated_read():
    assert holds_at(None, None, AUG, at_end=SEP, temporal="state") is False


def test_eternal_facts_hold_in_every_window():
    assert holds_at(None, None, AUG, at_end=SEP, temporal="eternal") is True


def test_the_point_predicate_is_byte_identical_without_a_window():
    """`at_end=None` must leave every existing caller exactly as it was."""
    cases = [
        ("2026-08-15T00:00:00+00:00", None),
        (None, "2026-08-15T00:00:00+00:00"),
        ("2026-01-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00"),
        (None, None),
    ]
    for valid_from, valid_to in cases:
        for moment in (AUG, SEP, None):
            assert holds_at(valid_from, valid_to, moment) == \
                holds_at(valid_from, valid_to, moment, at_end=None)


# --- the temporal class comes from the axiom store ------------------------

def test_commit_relations_are_events_and_assignment_is_a_state():
    """This is what makes the window correct rather than merely wider: the
    distinction was already in the ontology, it just had no reader."""
    assert DEFAULT_AXIOMS.temporal_of("AUTHORED_BY") == "event"
    assert DEFAULT_AXIOMS.temporal_of("MODIFIES") == "event"
    assert DEFAULT_AXIOMS.temporal_of("ASSIGNED_TO") == "state"


def test_an_unknown_relation_falls_back_to_the_wider_reading():
    assert DEFAULT_AXIOMS.temporal_of("NOT_A_RELATION") == "state"
