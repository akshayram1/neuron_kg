"""Stated-date extraction for text evidence — Phase 5.1 (25-plan.md §5.1).

`stated_dates(evidence, reference_time)` reads a span of evidence text (the
verbatim quote a fact was extracted from) and pulls out, if present:

- a **start** date: the point a fact is stated to hold from,
- an **end** date: the point a fact is stated to stop holding, and
- whether an end was *mentioned* in the text but could not be *resolved*
  to a concrete date (`end_stated_but_unresolved`) — e.g. "valid until the
  migration" or "was replaced" with no date attached. This is exactly the
  `ended_unknown` signal the caller (`semantic_pass.py`, not this module)
  needs; this module only reports the signal, it never decides policy.

This module does no writing and no LLM calls — it is a pure parsing helper.
Wiring it into `_write_extraction` is a separate, later task.

## No LLM, no `dateparser`

The plan's original framing (§5.1) called for `dateparser` for relative
expressions. At the time this module was written, `dateparser` was **not**
a project dependency (checked `pyproject.toml` / `uv.lock` — absent), and
adding a new dependency was out of scope for this change. Relative dates
are therefore resolved with a narrow, documented regex approach covering
exactly the patterns named in the plan (see "Relative patterns supported"
below) rather than general natural-language date parsing. If broader
relative-date coverage is needed later, swapping in `dateparser` (anchored
via `RELATIVE_BASE = reference_time`) is the natural upgrade path — see
`stated_dates`'s docstring for the exact seam.

## Explicit patterns supported

- ISO 8601: `2026-03-12`, `2026-03-12T10:00:00Z`, `2026-03-12T10:00:00+05:30`,
  `2026-03-12 10:00`.
- Day-month-year / month-day-year, long or short month names, optional
  ordinal suffix and comma: `12 March 2026`, `March 12, 2026`, `12 Mar 2026`,
  `March 12th 2026`.
- Month + year only (no day): `Mar 2026`, `March 2026` — resolved to the
  **first of the month** (`2026-03-01`). Chosen because (a) it matches the
  ISO 8601 convention that a truncated date implies the start of the
  smallest omitted unit, and (b) it is the same value regardless of whether
  the date ends up classified as a start or an end, so the choice does not
  quietly bias facts toward looking longer- or shorter-lived than stated.
  This is a judgment call flagged to the repo owner (see QUERIES in the
  handoff report); it is easy to change in one place (`_resolve_month_year`)
  if a different anchor is preferred later.

## Relative patterns supported (regex-based, anchored to `reference_time`)

- `yesterday`, `today`, `last week`, `next week`, `this week`.
- `N day(s)/week(s)/month(s) ago`. "Month" is approximated as 30 days
  (documented, not calendar-accurate — no calendar-month arithmetic is
  attempted without a real date library doing the heavy lifting).
- `from next sprint` / `next sprint` / `this sprint`. Sprint length is not
  knowable from text alone; this module assumes a **14-day sprint**
  (`_SPRINT_LENGTH_DAYS`) as a documented, conservative placeholder. This is
  a genuine judgment call flagged to the repo owner — a real sprint length
  (per team/project) would need to come from configuration, not a constant
  here.
- `since Q2`, `in Q3 2026` — quarters map Q1=Jan-Mar, Q2=Apr-Jun, Q3=Jul-Sep,
  Q4=Oct-Dec, resolved to the quarter's first day. When no year is stated,
  the year is taken from `reference_time` (documented assumption, not a
  blocking query — the plan's own example, "since Q2", omits a year).

## Not supported

No general free-text NLP (no "the following Tuesday", "a fortnight from
signing", "Q2 next year", fiscal-year quarters, or non-English phrasings).
No timezone-aware arithmetic beyond passing through an explicit ISO offset.
No multi-date disambiguation beyond simple start/end keyword proximity
within the same sentence (see `_classify`) — evidence spans are expected to
be short (a single extracted quote), where this is sufficient.

## False positives

Per §5.9's risk note ("`dateparser` false positives -> only accept dates
inside the evidence span"), the same discipline is applied here even
without `dateparser`: patterns are anchored with word boundaries and a
negative lookbehind that rejects a year immediately preceded by a
letter/digit/hyphen, so ticket-style identifiers (`DS-2026`, `DATAOS-4346`)
are not misread as years. Version strings (`v2.0.3`) and most filenames
never contain a 4-digit-year-dash-month-dash-day run in the first place, so
they are excluded structurally rather than by a special-cased guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timedelta

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
# Longest names first so the alternation's greedy left-to-right try order
# prefers "september" over "sep" without relying on backtracking quirks.
_MONTH_NAMES_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

# A year is not a date fragment of a ticket key or similar identifier: do
# not match when the digits are immediately preceded by a letter, digit, or
# hyphen (rejects "DS-2026", "DATAOS-4346-03-12", "v2026").
_NOT_IDENTIFIER = r"(?<![A-Za-z0-9-])"

_ISO_RE = re.compile(
    _NOT_IDENTIFIER + r"\b(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(Z|[+-]\d{2}:?\d{2})?)?\b"
)
_DMY_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_NAMES_ALT})\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_MDY_RE = re.compile(
    rf"\b({_MONTH_NAMES_ALT})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    rf"\b({_MONTH_NAMES_ALT})\s+(\d{{4}})\b",
    re.IGNORECASE,
)
_REL_N_UNITS_AGO_RE = re.compile(
    r"\b(\d+)\s+(day|week|month)s?\s+ago\b", re.IGNORECASE
)
_REL_SIMPLE_RE = re.compile(
    r"\b(yesterday|today|last week|next week|this week)\b", re.IGNORECASE
)
_REL_SPRINT_RE = re.compile(
    r"\b(?:from\s+)?(next|this)\s+sprint\b", re.IGNORECASE
)
_QUARTER_RE = re.compile(r"\bQ([1-4])(?:\s+(\d{4}))?\b")

_START_KEYWORD_RE = re.compile(
    r"\b(since|from|starting|started|starts|begins?|began|effective|as of)\b",
    re.IGNORECASE,
)
_END_KEYWORD_RE = re.compile(
    r"\b(until|till|through|ends?|ended|ending|expires?|expired|replaced|"
    r"supersedes?|superseded|terminates?|terminated)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;]+")

_SPRINT_LENGTH_DAYS = 14  # documented assumption -- see module docstring
_QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
_SIMPLE_OFFSET_DAYS = {
    "yesterday": -1,
    "today": 0,
    "last week": -7,
    "next week": 7,
    "this week": 0,
}


@dataclass(frozen=True)
class StatedDates:
    """Result of parsing an evidence span for stated dates.

    - `start`: ISO date (`YYYY-MM-DD`) or datetime if a start was found,
      else `None`.
    - `end`: ISO date/datetime if an end was found **and resolved**, else
      `None`.
    - `end_stated_but_unresolved`: an end was mentioned in the text (e.g.
      "until the migration", "was replaced") but no date could be resolved
      for it. This is the `ended_unknown` signal from §5.1 -- the caller
      decides what to do with it, this module only reports it.
    """

    start: str | None
    end: str | None
    end_stated_but_unresolved: bool


@dataclass(frozen=True)
class _Candidate:
    start: int
    end: int
    iso: str


def _coerce_reference(reference_time: datetime | _date | str | None) -> _date | None:
    if reference_time is None:
        return None
    if isinstance(reference_time, datetime):
        return reference_time.date()
    if isinstance(reference_time, _date):
        return reference_time
    if isinstance(reference_time, str):
        text = reference_time.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _resolve_iso(m: re.Match[str]) -> str | None:
    year, month, day, hour, minute, second, tz = m.groups()
    y, mo, d = int(year), int(month), int(day)
    try:
        if hour is None:
            _date(y, mo, d)
            return f"{y:04d}-{mo:02d}-{d:02d}"
        h, mi = int(hour), int(minute)
        s = int(second) if second else 0
        datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
    suffix = tz or ""
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}{suffix}"


def _resolve_dmy(m: re.Match[str]) -> str | None:
    day, month_name, year = m.groups()
    mo = _MONTHS[month_name.lower()]
    d, y = int(day), int(year)
    try:
        _date(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _resolve_mdy(m: re.Match[str]) -> str | None:
    month_name, day, year = m.groups()
    mo = _MONTHS[month_name.lower()]
    d, y = int(day), int(year)
    try:
        _date(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _resolve_month_year(m: re.Match[str]) -> str | None:
    month_name, year = m.groups()
    mo = _MONTHS[month_name.lower()]
    y = int(year)
    return f"{y:04d}-{mo:02d}-01"


def _resolve_days_ago(m: re.Match[str], ref: _date | None) -> str | None:
    if ref is None:
        return None
    n_str, unit = m.groups()
    n = int(n_str)
    unit = unit.lower()
    if unit == "day":
        delta = timedelta(days=n)
    elif unit == "week":
        delta = timedelta(weeks=n)
    else:  # "month" -- approximated, see module docstring
        delta = timedelta(days=30 * n)
    return (ref - delta).isoformat()


def _resolve_simple(m: re.Match[str], ref: _date | None) -> str | None:
    if ref is None:
        return None
    phrase = " ".join(m.group(0).lower().split())
    offset = _SIMPLE_OFFSET_DAYS[phrase]
    return (ref + timedelta(days=offset)).isoformat()


def _resolve_sprint(m: re.Match[str], ref: _date | None) -> str | None:
    if ref is None:
        return None
    which = m.group(1).lower()
    if which == "this":
        return ref.isoformat()
    return (ref + timedelta(days=_SPRINT_LENGTH_DAYS)).isoformat()


def _resolve_quarter(m: re.Match[str], ref: _date | None) -> str | None:
    q_str, year = m.groups()
    if year is None and ref is None:
        return None
    y = int(year) if year else ref.year  # type: ignore[union-attr]
    mo = _QUARTER_START_MONTH[int(q_str)]
    return f"{y:04d}-{mo:02d}-01"


def _scan(text: str, ref: _date | None) -> list[_Candidate]:
    claimed: list[tuple[int, int]] = []
    candidates: list[_Candidate] = []

    def overlaps(s: int, e: int) -> bool:
        return any(s < ce and e > cs for cs, ce in claimed)

    def apply(pattern: re.Pattern[str], resolver) -> None:
        for m in pattern.finditer(text):
            s, e = m.span()
            if overlaps(s, e):
                continue
            iso = resolver(m)
            if iso is None:
                continue
            claimed.append((s, e))
            candidates.append(_Candidate(s, e, iso))

    # Explicit formats first (unambiguous), most specific to least.
    apply(_ISO_RE, _resolve_iso)
    apply(_DMY_RE, _resolve_dmy)
    apply(_MDY_RE, _resolve_mdy)
    apply(_MONTH_YEAR_RE, _resolve_month_year)
    # Relative formats, anchored to `ref`.
    apply(_REL_N_UNITS_AGO_RE, lambda m: _resolve_days_ago(m, ref))
    apply(_REL_SIMPLE_RE, lambda m: _resolve_simple(m, ref))
    apply(_REL_SPRINT_RE, lambda m: _resolve_sprint(m, ref))
    apply(_QUARTER_RE, lambda m: _resolve_quarter(m, ref))

    candidates.sort(key=lambda c: c.start)
    return candidates


def _sentence_span(text: str, index: int) -> tuple[int, int]:
    pos = 0
    for m in _SENTENCE_SPLIT_RE.finditer(text):
        if pos <= index < m.end():
            return pos, m.end()
        pos = m.end()
    return pos, len(text)


def _classify(text: str, candidates: list[_Candidate]) -> tuple[str | None, str | None]:
    """Assign each resolved candidate date to `start` or `end`.

    A candidate is classified by the nearest start/end keyword that
    precedes it within its own sentence (e.g. "from 12 March 2026" ->
    start; "until 4 August 2026" -> end). A candidate with no keyword in
    front of it is left unclassified; unclassified candidates are then
    assigned in textual order to fill whichever of start/end is still
    empty (handles bare ranges like "12 March 2026 to 4 August 2026" and
    single bare dates, which default to `start`).
    """
    start: str | None = None
    end: str | None = None
    unclassified: list[str] = []

    for cand in candidates:
        s_sent, _ = _sentence_span(text, cand.start)
        preceding = text[s_sent:cand.start]
        start_matches = list(_START_KEYWORD_RE.finditer(preceding))
        end_matches = list(_END_KEYWORD_RE.finditer(preceding))
        last_start = start_matches[-1].start() if start_matches else -1
        last_end = end_matches[-1].start() if end_matches else -1
        if last_end >= 0 and last_end > last_start:
            if end is None:
                end = cand.iso
        elif last_start >= 0 and last_start > last_end:
            if start is None:
                start = cand.iso
        else:
            unclassified.append(cand.iso)

    for iso in unclassified:
        if start is None:
            start = iso
        elif end is None:
            end = iso

    return start, end


def stated_dates(
    evidence: str | None,
    reference_time: datetime | _date | str | None,
) -> StatedDates:
    """Parse `evidence` for a stated start/end date, anchored to `reference_time`.

    `reference_time` is the record/chunk's `source_time` (accepts a
    `datetime`, a `date`, an ISO string, or `None`). Relative expressions
    ("last week", "from next sprint", ...) resolve against it; explicit
    dates ("12 March 2026") do not need it and still resolve when it is
    `None`.

    Returns a `StatedDates` with three independent signals -- resolved
    start, resolved end, and "an end was mentioned but not resolvable" --
    and makes no policy decision about `valid_at_basis` / `ended_unknown`;
    that belongs to the caller (`_write_extraction` in `semantic_pass.py`,
    wired in a later task).

    To upgrade relative-date coverage later with `dateparser` (per the
    plan's original framing), the seam is `_scan`'s relative-pattern block:
    replace the four `apply(_REL_*, ...)` calls with a single call into
    `dateparser.parse(text, settings={"RELATIVE_BASE": ref})` per
    candidate span, keeping the explicit-format block (ISO/DMY/MDY/month-
    year) as-is since those are unambiguous and do not need it.
    """
    if not evidence or not evidence.strip():
        return StatedDates(start=None, end=None, end_stated_but_unresolved=False)

    ref = _coerce_reference(reference_time)
    candidates = _scan(evidence, ref)
    start, end = _classify(evidence, candidates)
    end_stated_but_unresolved = end is None and bool(_END_KEYWORD_RE.search(evidence))
    return StatedDates(start=start, end=end, end_stated_but_unresolved=end_stated_but_unresolved)
