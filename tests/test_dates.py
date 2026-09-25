from datetime import date, datetime

from graph.dates import stated_dates

REF = datetime(2026, 6, 15)  # a Monday-ish anchor, mid-Q2 2026


# ---------------------------------------------------------------------------
# Explicit ISO 8601
# ---------------------------------------------------------------------------

def test_iso_date_only():
    r = stated_dates("Deployed on 2026-03-12.", REF)
    assert r.start == "2026-03-12"
    assert r.end is None
    assert r.end_stated_but_unresolved is False


def test_iso_datetime_with_z():
    r = stated_dates("Recorded at 2026-03-12T10:00:00Z in the log.", REF)
    assert r.start == "2026-03-12T10:00:00Z"


def test_iso_datetime_with_offset():
    r = stated_dates("Timestamp 2026-03-12T10:00:00+05:30 confirmed.", REF)
    assert r.start == "2026-03-12T10:00:00+05:30"


def test_iso_datetime_no_seconds():
    r = stated_dates("Effective 2026-03-12T10:00 sharp.", REF)
    assert r.start == "2026-03-12T10:00:00"


def test_iso_invalid_date_is_ignored():
    # Not a real calendar date -- must not crash, must not be returned.
    r = stated_dates("See entry 2026-13-40 in the archive.", REF)
    assert r.start is None
    assert r.end is None


# ---------------------------------------------------------------------------
# Explicit day-month-year / month-day-year
# ---------------------------------------------------------------------------

def test_dmy_long_month():
    r = stated_dates("Signed on 12 March 2026.", REF)
    assert r.start == "2026-03-12"


def test_dmy_short_month():
    r = stated_dates("Signed on 12 Mar 2026.", REF)
    assert r.start == "2026-03-12"


def test_mdy_with_comma():
    r = stated_dates("Signed March 12, 2026.", REF)
    assert r.start == "2026-03-12"


def test_mdy_without_comma():
    r = stated_dates("Signed Mar 12 2026.", REF)
    assert r.start == "2026-03-12"


def test_dmy_with_ordinal_suffix():
    r = stated_dates("Effective 12th March 2026.", REF)
    assert r.start == "2026-03-12"


# ---------------------------------------------------------------------------
# Explicit month + year (anchored to first of month -- documented choice)
# ---------------------------------------------------------------------------

def test_month_year_short_form_anchors_to_first_of_month():
    r = stated_dates("Planned for Mar 2026.", REF)
    assert r.start == "2026-03-01"


def test_month_year_long_form_anchors_to_first_of_month():
    r = stated_dates("Planned for March 2026.", REF)
    assert r.start == "2026-03-01"


def test_month_year_anchor_is_first_of_month_not_reference_day():
    # Anchor must be the 1st regardless of what day-of-month reference_time is.
    ref = datetime(2026, 3, 27)
    r = stated_dates("Rolling out in March 2026.", ref)
    assert r.start == "2026-03-01"


# ---------------------------------------------------------------------------
# Relative patterns
# ---------------------------------------------------------------------------

def test_last_week():
    r = stated_dates("Reported last week.", REF)
    assert r.start == "2026-06-08"


def test_next_week():
    r = stated_dates("Scheduled next week.", REF)
    assert r.start == "2026-06-22"


def test_this_week():
    r = stated_dates("Happening this week.", REF)
    assert r.start == "2026-06-15"


def test_yesterday():
    r = stated_dates("Filed yesterday.", REF)
    assert r.start == "2026-06-14"


def test_today():
    r = stated_dates("Filed today.", REF)
    assert r.start == "2026-06-15"


def test_n_days_ago():
    r = stated_dates("Filed 3 days ago.", REF)
    assert r.start == "2026-06-12"


def test_n_weeks_ago():
    r = stated_dates("Filed 2 weeks ago.", REF)
    assert r.start == "2026-06-01"


def test_n_months_ago_is_approximated_30_days():
    r = stated_dates("Filed 1 month ago.", REF)
    assert r.start == "2026-05-16"  # REF - 30 days, documented approximation


def test_from_next_sprint_uses_14_day_assumption():
    r = stated_dates("Work starts from next sprint.", REF)
    assert r.start == "2026-06-29"  # REF + 14 days, documented sprint length


def test_this_sprint_anchors_to_reference_time():
    r = stated_dates("Landing this sprint.", REF)
    assert r.start == "2026-06-15"


def test_since_q2_uses_reference_year_when_year_omitted():
    r = stated_dates("Live since Q2.", REF)
    assert r.start == "2026-04-01"


def test_in_q3_2026_explicit_year():
    r = stated_dates("Targeted in Q3 2026.", REF)
    assert r.start == "2026-07-01"


def test_quarter_boundaries_q1_q4():
    assert stated_dates("since Q1", REF).start == "2026-01-01"
    assert stated_dates("since Q4", REF).start == "2026-10-01"


# ---------------------------------------------------------------------------
# reference_time actually anchors relative dates (two different reference
# times must give two different, correct answers)
# ---------------------------------------------------------------------------

def test_reference_time_changes_relative_resolution():
    ref_a = datetime(2026, 1, 10)
    ref_b = datetime(2026, 9, 25)
    r_a = stated_dates("Reported last week.", ref_a)
    r_b = stated_dates("Reported last week.", ref_b)
    assert r_a.start == "2026-01-03"
    assert r_b.start == "2026-09-18"
    assert r_a.start != r_b.start


def test_no_reference_time_means_relative_dates_cannot_resolve():
    r = stated_dates("Reported last week.", None)
    assert r.start is None
    assert r.end is None


# ---------------------------------------------------------------------------
# start-only, end-only, both, neither
# ---------------------------------------------------------------------------

def test_start_only():
    r = stated_dates("Effective from 12 March 2026, the API uses OAuth2.", REF)
    assert r.start == "2026-03-12"
    assert r.end is None
    assert r.end_stated_but_unresolved is False


def test_end_only():
    r = stated_dates("This policy was in effect until 4 August 2026.", REF)
    assert r.start is None
    assert r.end == "2026-08-04"
    assert r.end_stated_but_unresolved is False


def test_both_start_and_end():
    r = stated_dates(
        "The rate limit was in effect from 1 January 2026 until 30 June 2026.",
        REF,
    )
    assert r.start == "2026-01-01"
    assert r.end == "2026-06-30"
    assert r.end_stated_but_unresolved is False


def test_bare_range_without_keywords_defaults_start_then_end():
    r = stated_dates("12 March 2026 to 4 August 2026.", REF)
    assert r.start == "2026-03-12"
    assert r.end == "2026-08-04"


def test_neither_present():
    r = stated_dates("The team discussed the rollout plan in the meeting.", REF)
    assert r.start is None
    assert r.end is None
    assert r.end_stated_but_unresolved is False


# ---------------------------------------------------------------------------
# ended_unknown signal: end mentioned, not resolvable
# ---------------------------------------------------------------------------

def test_until_the_migration_is_unresolved_end():
    r = stated_dates("This config was valid until the migration.", REF)
    assert r.end is None
    assert r.end_stated_but_unresolved is True


def test_was_replaced_with_no_date_is_unresolved_end():
    r = stated_dates("The old endpoint was replaced.", REF)
    assert r.end is None
    assert r.end_stated_but_unresolved is True


def test_start_resolved_end_mentioned_but_unresolved_together():
    r = stated_dates(
        "Feature X was live from 1 January 2026 until the migration.", REF
    )
    assert r.start == "2026-01-01"
    assert r.end is None
    assert r.end_stated_but_unresolved is True


def test_replaced_on_a_date_resolves_and_is_not_flagged_unresolved():
    r = stated_dates("The old endpoint was replaced on 4 August 2026.", REF)
    assert r.end == "2026-08-04"
    assert r.end_stated_but_unresolved is False


# ---------------------------------------------------------------------------
# False-positive traps
# ---------------------------------------------------------------------------

def test_ticket_id_is_not_a_date():
    r = stated_dates("See DS-2026 for the full discussion.", REF)
    assert r.start is None
    assert r.end is None


def test_ticket_id_with_full_date_shape_is_not_a_date():
    r = stated_dates("Filed under DATAOS-2026-03-12 in the tracker.", REF)
    assert r.start is None
    assert r.end is None


def test_version_string_is_not_a_date():
    r = stated_dates("Upgrade to v2.0.3 before continuing.", REF)
    assert r.start is None
    assert r.end is None


def test_file_path_with_numbers_is_not_a_date():
    r = stated_dates(
        "Manifest lives at /exports/2026/03/12/manifest.json on disk.", REF
    )
    assert r.start is None
    assert r.end is None


def test_underscored_filename_with_digits_is_not_a_date():
    r = stated_dates("Backup saved as config_20260312_final.yaml.", REF)
    assert r.start is None
    assert r.end is None


def test_traps_do_not_suppress_a_real_date_elsewhere_in_the_text():
    r = stated_dates(
        "See DS-2026 and v2.0.3 -- deployed on 12 March 2026.", REF
    )
    assert r.start == "2026-03-12"


# ---------------------------------------------------------------------------
# Empty / missing evidence
# ---------------------------------------------------------------------------

def test_empty_string_evidence():
    r = stated_dates("", REF)
    assert r == stated_dates("", REF)
    assert r.start is None
    assert r.end is None
    assert r.end_stated_but_unresolved is False


def test_whitespace_only_evidence():
    r = stated_dates("   \n\t  ", REF)
    assert r.start is None
    assert r.end is None
    assert r.end_stated_but_unresolved is False


def test_none_evidence():
    r = stated_dates(None, REF)
    assert r.start is None
    assert r.end is None
    assert r.end_stated_but_unresolved is False


# ---------------------------------------------------------------------------
# reference_time input flexibility
# ---------------------------------------------------------------------------

def test_reference_time_accepts_plain_date():
    r = stated_dates("Filed yesterday.", date(2026, 6, 15))
    assert r.start == "2026-06-14"


def test_reference_time_accepts_iso_string():
    r = stated_dates("Filed yesterday.", "2026-06-15T00:00:00Z")
    assert r.start == "2026-06-14"
