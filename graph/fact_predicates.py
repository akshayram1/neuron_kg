"""Centralized "is this fact edge world-time-live" predicate.

25-plan.md Phase 5 §5.0.6 ("Read contract"): "ordinary world-time readers
require `assertion_status != 'corrected'` and `projection_status = 'live'`."

`graph/chat.py`, `graph/entity.py`, `graph/derived.py` and roughly twenty
other call sites across the codebase each currently hand-roll
`r.invalid_at IS NULL` wherever they need "the live fact(s)". That was
harmless as long as nothing ever wrote `assertion_status` or
`projection_status` -- every edge in the graph implicitly satisfied both
checks by omission. `graph/writer.py`'s `correct_fact` and the
`projection_status` row field on `upsert_fact_edges` are the first writers
of those two properties, so from this point on a hand-rolled
`invalid_at IS NULL` check can silently surface a `corrected` assertion (a
fact we now know was never true) or a `pending_review` candidate (a fact
not yet eligible to answer with) as if either were an ordinary live fact.

This module is the one place that predicate is defined, in both a Cypher
fragment and a pure-Python mirror. It deliberately does **not** rewrite any
existing reader -- `graph/chat.py` in particular is off-limits to this
change, and updating the ~23 other `invalid_at IS NULL` call sites across
the codebase is a separate, larger follow-up (tracked, not started here;
see the Phase 5 writer-contract report). Every reader touched going
forward should import and use `LIVE_FACT_CYPHER` / `live_fact_cypher` /
`is_live_fact` instead of re-deriving the check by hand, so chat, entity
detail, history and graph views cannot drift from each other again.
"""

from __future__ import annotations

from typing import Any

# A Cypher boolean expression, safe to inline into any `WHERE`/`CASE` that
# has a relationship variable bound to `r`. Missing `assertion_status` /
# `projection_status` (every edge written before this concept existed)
# reads as `'live'` via `coalesce`, exactly like the Python mirror below.
LIVE_FACT_CYPHER = (
    "r.invalid_at IS NULL "
    "AND coalesce(r.assertion_status, 'live') <> 'corrected' "
    "AND coalesce(r.projection_status, 'live') = 'live'"
)


def live_fact_cypher(var: str = "r") -> str:
    """`LIVE_FACT_CYPHER` against relationship variable `var` instead of `r`,
    for callers whose Cypher binds the edge to a different name."""
    return (
        f"{var}.invalid_at IS NULL "
        f"AND coalesce({var}.assertion_status, 'live') <> 'corrected' "
        f"AND coalesce({var}.projection_status, 'live') = 'live'"
    )


def is_live_fact(edge: dict[str, Any]) -> bool:
    """Python-side mirror of `LIVE_FACT_CYPHER`, for readers that already
    hold an edge's properties as a plain dict (e.g. after `RETURN r{.*}`,
    or a FalkorDB result row zipped back into a mapping) and for tests that
    want to exercise the predicate without round-tripping through Cypher.

    `edge` is any mapping with (a subset of) `invalid_at`, `assertion_status`,
    `projection_status` keys; a missing/`None` key is treated as its "live"
    default -- same as the Cypher `coalesce(...)` above.
    """
    if edge.get("invalid_at") is not None:
        return False
    if (edge.get("assertion_status") or "live") == "corrected":
        return False
    if (edge.get("projection_status") or "live") != "live":
        return False
    return True
