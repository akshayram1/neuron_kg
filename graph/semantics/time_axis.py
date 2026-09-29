"""World axis vs record axis — one place, every reader.

Utopia ADR 0019/0022: folding `at` and `as_of` into one slider answers the
wrong question and looks right. Neuron already stores both clocks
(`valid_at` / FactHistory.valid_* vs `first_seen_at` / observed_*). This
module is the only implementation of the two predicates so chat, history,
entity detail and graph view cannot drift.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Do not treat `DATAOS-4346` as year 4346 (verified: chat set
# world-time at=4346-01-01 and then said the ticket was absent).
# A leading project-key hyphen, or a year outside 19xx/20xx, is not a date.
_ISO_RE = re.compile(
    r"(?<![A-Za-z0-9]-)\b((?:19|20)\d{2})"
    r"(?:-(\d{2})(?:-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:Z|[+-]\d{2}:?\d{2})?)?)?)?\b"
)
# `aug'26` and `Aug '26` are how people actually write a sprint month, and a
# four-digit-only rule silently read them as no date at all. The apostrophe is
# required for the two-digit form: a bare `aug 26` is a day, not a year.
_MONTH_YEAR_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\s*['’]\s*(\d{2})\b"
    r"|\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b",
    re.IGNORECASE,
)
_RECORD_HINT = re.compile(
    r"\b(what did we (know|believe|have on record|learn)|before .+ arrived|"
    r"on record|as we (understood|believed)|did we get wrong)\b",
    re.IGNORECASE,
)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def holds_at(
    valid_from: str | None,
    valid_to: str | None,
    at: datetime | None,
    *,
    at_end: datetime | None = None,
    temporal: str = "state",
    attested_from: str | None = None,
    ended_unknown: bool = False,
) -> bool:
    """World axis: did this fact hold at `at` — or anywhere in `[at, at_end)`?

    `at is None` means every moment (show the full timeline).
    A missing start is anchored to `attested_from` (the document/source time),
    never to -infinity — an unknown date is not an open one.
    A missing end means still holds, unless `ended_unknown` (the text said it
    ended but not when), in which case the end is the attesting moment.

    `at_end` turns the read into a window, because a month is an interval and
    collapsing it to its first instant answers a question nobody asked: a
    snapshot at 2026-08-01T00:00 excludes everything that happened *during*
    August by construction. `temporal` decides what "in the window" means —
    an `event` must fall inside it, a `state` need only overlap it.
    """
    if at is None:
        return True
    start = parse_iso(valid_from) or parse_iso(attested_from)
    if ended_unknown:
        end = parse_iso(valid_to) or parse_iso(attested_from)
    else:
        end = parse_iso(valid_to)
    if at_end is not None:
        if temporal == "eternal":
            return True
        if temporal == "event":
            # An event's instant is its start; an open end says nothing about
            # when it happened, so never widen an event to the whole window.
            return start is not None and at <= start < at_end
        if start is None and end is None:
            return False
        return (end is None or end > at) and (start is None or start < at_end)
    if start is not None and at < start:
        return False
    if end is not None and at >= end:
        return False
    if start is None and end is None:
        # Undated event: holds at no particular moment. Still listed on the
        # entity when `at` is omitted; excluded from a dated world-axis read.
        return False
    return True


def held_at(
    observed_from: str | None,
    observed_to: str | None,
    as_of: datetime | None,
) -> bool:
    """Record axis: did *we* hold this belief at `as_of`?

    `as_of is None` means now — only still-open observations.
    """
    start = parse_iso(observed_from)
    end = parse_iso(observed_to)
    if as_of is None:
        return end is None
    if start is not None and start > as_of:
        return False
    if end is not None and end <= as_of:
        return False
    return True


def infer_query_clocks(question: str) -> tuple[str | None, str | None]:
    """Best-effort `at` / `as_of` from a natural-language question.

    A record-axis hint ("what did we believe") puts the date on `as_of`;
    otherwise a dated question is world time. The caller may still override
    either from the API.

    Point form, kept for callers that read a single moment. A reader that can
    honour an interval should call `infer_query_window` instead — see there
    for why "August 2026" is not the instant 2026-08-01T00:00.
    """
    at, _, as_of = infer_query_window(question)
    return at, as_of


def infer_query_window(question: str) -> tuple[str | None, str | None, str | None]:
    """`(at, at_end, as_of)` — the world-time *interval* a question names.

    The precision of the phrase sets the width: "August 2026" and "2026-08"
    are a month, "2026" a year, "2026-08-14" a day, and only an explicit time
    is an instant (`at_end is None`). The record axis (`as_of`) stays a point
    — "what did we believe in August" reads the ledger as it stood when the
    month opened, which is a moment, not a span.
    """
    window = _first_window(question)
    if window is None:
        return None, None, None
    start, end = window
    if _RECORD_HINT.search(question):
        return None, None, start
    return start, end, None


def _first_window(text: str) -> tuple[str, str | None] | None:
    """`(start, end)` for the first date in `text`; `end is None` = instant."""
    month = _MONTH_YEAR_RE.search(text)
    if month:
        if month.group(1):                      # aug'26 — this century
            year, mon = 2000 + int(month.group(2)), _MONTHS[month.group(1).lower()]
        else:                                   # August 2026
            year, mon = int(month.group(4)), _MONTHS[month.group(3).lower()]
        return _iso(year, mon, 1), _month_after(year, mon)
    iso = _ISO_RE.search(text)
    if not iso:
        return None
    year, mon, day = int(iso.group(1)), iso.group(2), iso.group(3)
    if iso.group(4):
        hour, minute, second = iso.group(4), iso.group(5), iso.group(6) or "00"
        return f"{year:04d}-{mon}-{day}T{hour}:{minute}:{second}+00:00", None
    if mon is None:
        return _iso(year, 1, 1), _iso(year + 1, 1, 1)
    if day is None:
        return _iso(year, int(mon), 1), _month_after(year, int(mon))
    start = _iso(year, int(mon), int(day))
    return start, _add_day(start)


def _iso(year: int, month: int, day: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}T00:00:00+00:00"


def _month_after(year: int, month: int) -> str:
    return _iso(year + 1, 1, 1) if month == 12 else _iso(year, month + 1, 1)


def _add_day(start: str) -> str:
    return (parse_iso(start) + timedelta(days=1)).isoformat()


def interval_note(valid_from: str | None, valid_to: str | None, *, ended_unknown: bool = False) -> str | None:
    start = _ymd(valid_from)
    end = _ymd(valid_to)
    if ended_unknown and not end:
        return f"{start} → ended, date unknown" if start else "ended, date unknown"
    if start and end:
        return f"{start} → {end}"
    if start:
        return f"from {start} · ongoing"
    if end:
        return f"until {end}"
    return None


def _ymd(value: str | None) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def as_fact_dict(
    *,
    source: str,
    target: str,
    relation: str,
    evidence: str | None,
    valid_from: str | None,
    valid_to: str | None,
    observed_from: str | None,
    observed_to: str | None,
    documents: list[str],
    state: str,
    derived: bool = False,
    derived_rule: str | None = None,
    premises: list[str] | None = None,
    ended_unknown: bool = False,
    attested_from: str | None = None,
    fact_uid: str | None = None,
    from_uid: str | None = None,
    to_uid: str | None = None,
    direction: str = "out",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "source": source, "target": target, "relation": relation,
        "evidence": evidence, "validFrom": valid_from, "validTo": valid_to,
        "observedFrom": observed_from, "observedTo": observed_to,
        "documents": documents, "state": state, "derived": derived,
        "derivedRule": derived_rule, "premises": premises or [],
        "endedUnknown": ended_unknown, "attestedFrom": attested_from,
        "factUid": fact_uid, "fromUid": from_uid, "toUid": to_uid,
        "direction": direction,
        "interval": interval_note(valid_from, valid_to, ended_unknown=ended_unknown),
    }
    if extra:
        row.update(extra)
    return row
