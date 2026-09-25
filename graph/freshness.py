"""Read-time freshness policy and effective confidence.

25-plan.md Phase 5 §5.5 ("Freshness policy") and §5.8 ("Effective confidence
and pinning"). Both are **pure computation** over values a caller already
holds — nothing here queries FalkorDB, mutates a fact edge, or decides what
`decay_class` a fact gets at write time. Deliberately out of scope, per the
task this module was built for:

- writing `decay_class` onto a fact edge from a chunk's Laya `chunk_type`
  (§5.5's write-time half) — blocked on Laya's `chunk_type` classification,
  same unresolved packaging question as everywhere else in this plan;
  `decay_class` already exists as a writable, optional field on fact edges
  (`graph/writer.py::upsert_fact_edges`, confirmed by reading that module on
  `main` — see the module-level note below on why this worktree read it via
  `git show` rather than importing it);
- the "evidence line" text (`last confirmed 2026-05-12 · status_update · may
  be outdated`) and the chat prompt rule that reads it — both
  `graph/chat.py` concerns, off-limits to this task. `freshness_class`'s
  output (a `Freshness` value) is meant to be clean enough for a future
  `graph/chat.py` change to build that text from.

Resolving `work_item_closed` for a `task`-class fact (a graph lookup: is the
linked WorkItem's status closed?) is also the caller's job — this module only
consumes the boolean once handed one.

--- Hysteresis reasoning (documented per the task's instruction) ---

§5.5's rule: "a fact becomes `stale` at 1.0x its window and returns to
`fresh` only when reconfirmed (a `duplicate` from new evidence), never by
time." A function computing `fresh`/`stale` from `(decay_class,
last_confirmed_at, now)` alone looks stateless — no memory of "was this
already stale" — which looks like it can't implement true hysteresis. It
CAN, and does, because the memory lives in `last_confirmed_at` itself, not
in extra state this function would otherwise need to track:

  `last_confirmed_at` only advances on a genuinely content-changing write —
  confirmed by reading `graph/writer.py::upsert_fact_edges`'s `ON MATCH`
  clause and its `confirm_fact` primitive on `main` (this worktree's own
  `graph/writer.py` predates the Phase 5.0/5.6 writer-contract merge, so it
  was read via `git show main:graph/writer.py` rather than imported — see
  the QUERIES section of this task's final report for why the branch was
  not fast-forward-merged). `upsert_fact_edges`'s docstring states this
  explicitly: "`last_confirmed_at` ... only bumps on a genuinely
  content-changing match: new/changed evidence, changed confidence, a
  source record key not already recorded, or a revive that actually reopens
  a closed edge. A KEEP-sync that re-asserts identical content leaves
  `last_confirmed_at` untouched." `confirm_fact` (used by
  `graph/resolve_text_fact.py::resolve_text_fact`'s `duplicate` branch —
  the exact "reconfirmed" case §5.5 names) applies the same content-change
  rule before bumping the clock.

  So: time passing alone can never move `last_confirmed_at`. It moves only
  on a `duplicate` reconfirmation (or an equivalent content-changing write).
  A function that computes "is `now - last_confirmed_at` within the window"
  therefore never flips a fact back to `fresh` by the passage of time —
  only a real reconfirmation event, which is exactly what the hysteresis
  rule requires. There is no separate "was this already stale" flag to
  maintain; `last_confirmed_at` already encodes the only event that is
  allowed to un-stale a fact.

This conclusion was verified by reading the merged `main`-branch source
directly (not assumed from the plan text alone) — see the final report's
"hysteresis reasoning" section.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from graph.time_axis import parse_iso


class Freshness(StrEnum):
    """Read-time-only classification of a fact edge's age. Never stored —
    `freshness_class` recomputes it on every call from `last_confirmed_at`
    (plus, for `task`-class facts, a caller-supplied `work_item_closed`)."""

    FRESH = "fresh"
    STALE = "stale"


# 25-plan.md §5.5's table. `None` for "task" is a sentinel, not "no window":
# task-class freshness is governed by whether the linked WorkItem is closed,
# not by a day count — see `freshness_class` below.
DECAY_WINDOWS: dict[str, int | None] = {
    "fast": 21,
    "task": None,
    "slow": 180,
    "durable": 365,
}

# "other -> slow" per the plan's own table. Applies to any `decay_class`
# that is missing, `None`, or not one of the four known values above.
_DEFAULT_DECAY_CLASS = "slow"


def _window_days(decay_class: str | None) -> int | None:
    """Resolve a `decay_class` to its freshness window in days, defaulting
    unknown/missing classes to `slow` (180 days). Returns `None` only for
    `task`, which the caller must special-case before reaching a day-count
    comparison."""
    return DECAY_WINDOWS.get(decay_class or "", DECAY_WINDOWS[_DEFAULT_DECAY_CLASS])


def freshness_class(
    decay_class: str | None,
    last_confirmed_at: str | None,
    *,
    now: str | None = None,
    work_item_closed: bool | None = None,
) -> Freshness:
    """Is this fact fresh or stale, read at `now` (default: the current
    time)?

    `decay_class` selects the window (`DECAY_WINDOWS`, unknown/missing ->
    `slow`/180 days), except `task`, which instead depends on
    `work_item_closed`:

    - `work_item_closed=True`  -> `stale` (the linked action item is done;
      the fact's "current" claim no longer holds).
    - `work_item_closed=False` or `None` -> `fresh`. A caller that has not
      resolved the linked WorkItem's status (this module never does that
      lookup itself — it is graph state, not a pure day-count) gets the
      safe, documented default: treat the WorkItem as still open, i.e. "not
      yet closed until proven otherwise" (design principle 5, 25-plan.md
      §6: "Never guess time" / never guess a negative state either — an
      unresolved status must not silently read as stale).

    For every other `decay_class`, staleness is a pure function of elapsed
    time since `last_confirmed_at`: `stale` once `now - last_confirmed_at`
    reaches (inclusive) 1.0x the window, `fresh` before that. See the
    module docstring for why this correctly implements the plan's
    hysteresis rule without any extra state.

    `last_confirmed_at` missing/`None` is treated as `stale` — never guess
    a fact is fresh when there is no record of when it was last confirmed.
    """
    if decay_class == "task":
        return Freshness.STALE if work_item_closed else Freshness.FRESH

    if not last_confirmed_at:
        return Freshness.STALE

    confirmed_dt = parse_iso(last_confirmed_at)
    if confirmed_dt is None:
        return Freshness.STALE
    now_dt = parse_iso(now) if now else datetime.now(UTC)

    window_days = _window_days(decay_class)
    assert window_days is not None  # only "task" (handled above) is None
    elapsed = now_dt - confirmed_dt
    return Freshness.STALE if elapsed >= timedelta(days=window_days) else Freshness.FRESH


# §5.8: `freshness_factor` — 1.0 when fresh (or pinned), 0.7 when stale and
# not pinned. Kept as a small lookup, same shape as `DECAY_WINDOWS`, rather
# than inline `if`/`else` chains in `effective_confidence`.
_STALE_FACTOR = 0.7
_FRESH_FACTOR = 1.0


def effective_confidence(
    confidence: float,
    freshness: Freshness | str,
    *,
    pinned: bool = False,
) -> float:
    """`effective = confidence * freshness_factor` — 25-plan.md §5.8.

    Stored `confidence` never changes; this is a read-only computation over
    its inputs, meant to be called from a future (currently off-limits)
    `graph/chat.py` reranking/display path. It never mutates anything.

    `pinned=True` forces `freshness_factor = 1.0` regardless of `freshness`
    — "pinned... exempts from staleness" (§5.8). Pinning also exempts a
    fact from demotion in §5.2's `resolve_text_fact`; that half is already
    implemented in `graph/resolve_text_fact.py` (the `old.pinned` branch
    redirects to a dispute instead of closing/correcting — confirmed by
    reading that module on `main`), so nothing more is needed here for that
    part.

    `freshness` accepts a `Freshness` value or a plain `"fresh"`/`"stale"`
    string (e.g. a value round-tripped through JSON/logging) — either is
    normalized via `Freshness(freshness)`, which raises `ValueError` on
    anything else rather than silently defaulting.
    """
    if pinned:
        return confidence * _FRESH_FACTOR
    factor = _FRESH_FACTOR if Freshness(freshness) == Freshness.FRESH else _STALE_FACTOR
    return confidence * factor
