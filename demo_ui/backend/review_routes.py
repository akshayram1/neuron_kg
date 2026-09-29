"""Review queue endpoints (25-plan.md Phase 3 §3.0 "Minimal review queue",
extended by Phase 6 §6.5 "Review queue UI").

A `reviews` row is a proposal a human has to accept or decline before it
takes effect -- `possibly_same_as` (Phase 4), `fact_update` (Phase 5) and
`duplicate_pair` (Phase 6.4) today; `link_candidate` (Phase 6.2) lives on its
own `link_candidates` table instead, served by
`demo_ui/backend/link_candidate_routes.py`. This module serves the generic
queue: creating, listing, approving and rejecting rows lives on
`ConnectorLedger` (`connectors/core/ledger.py`, `reviews`/
`review_rejections` tables); this is the same split `sync_coverage_routes.py`
uses for its read side.

§6.5's table ("Approve does: <action>") means approving a review is one
atomic user action, not two -- a human clicks Approve once and the real
effect (merge, close/dispute a fact, ...) happens, they don't separately
click "apply". `approve_review` below does the ledger state flip (as
before) AND, when the review's `type` has a real apply-on-approval function,
calls it in the same request via `_APPLY_FUNCTIONS`:

  - `fact_update`     -> `graph.resolve_text_fact.apply_approved_fact_update`
  - `duplicate_pair`  -> `graph.duplicate_collector.apply_approved_duplicate_merge`
  - `possibly_same_as`-> `graph.resolve_text_fact.apply_approved_possibly_same_as`
    (pairwise merge plus namespace-aware alias)
  - any other type (or an unrecognized one) -> no apply step, just the state
    flip, identical to this endpoint's behavior before this dispatch existed.

EDGE CASE, judgment call flagged per this task's instructions: by the time
the apply step runs, `ledger.approve_review` has already committed the
review as `approved` -- there is no ledger-level "undo" for that state flip
(same fire-once semantics `approve_review`/`reject_review` already give
every review). If the apply step then raises, the review is left
**approved-but-not-yet-applied**: a real, valid, uncomfortable state (not a
data-corruption bug -- the graph mutation simply never happened), not
silently hidden from the caller. Two options considered: (a) swallow the
apply exception and return 200 as if nothing happened, or (b) surface it as
a clear error. (a) would hide a real failure behind a success response,
which is worse than a human seeing "approved but not applied, retry needed"
and knowing to look. This module picks (b): a 500 response whose `detail`
names the review id, says it is already approved, and gives the apply
exception's own message, so a caller (human or the future `BridgePanel`) is
never left thinking a fact_update/duplicate merge that failed actually
happened. Retrying is the caller's job (e.g. by calling the graph-layer
apply function directly against the same, still-approved review id) -- this
endpoint does not retry on the caller's behalf.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from falkordb import Graph
from fastapi import APIRouter, HTTPException, Query

from connectors.core.ledger import ConnectorLedger, Review, ReviewState
from graph.storage import multigraph
from graph.storage import vector_store as vector_store_module
from graph.resolution.duplicate_collector import apply_approved_duplicate_merge
from graph.storage.falkor_client import get_graph
from graph.resolution.resolve_text_fact import (
    apply_approved_fact_update,
    apply_approved_possibly_same_as,
)
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/reviews", tags=["reviews"])

# §6.5's "Approve does: <action>" table, minus `link_candidate` (its own
# table/router, see module docstring). Keyed by `Review.type`; a type with
# no entry here just gets the plain state flip, unchanged from Phase 3.
_APPLY_FUNCTIONS: dict[str, Callable[[Graph, ConnectorLedger, int], dict]] = {
    "fact_update": apply_approved_fact_update,
    "duplicate_pair": apply_approved_duplicate_merge,
    "possibly_same_as": apply_approved_possibly_same_as,
}


def _ledger(graph_name: str) -> ConnectorLedger:
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    return ConnectorLedger(target.ledger_path)


def _graph(graph_name: str) -> Any:
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    return get_graph(name=target.falkor_name)


def _as_dict(row: Review) -> dict:
    return {
        "id": row.id,
        "type": row.type,
        "payload": row.payload,
        "identity": row.identity,
        "state": row.state,
        "decided_by": row.decided_by,
        "decided_at": row.decided_at,
        "created_at": row.created_at,
    }


@router.get("")
async def list_reviews(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
    state: str | None = Query(default=None, max_length=20),
    type: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """List reviews for this graph, optionally filtered by `state`
    (pending/approved/rejected) and/or `type` (e.g. `possibly_same_as`)."""
    if state is not None:
        try:
            ReviewState(state)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"invalid state {state!r}; expected one of "
                       f"{[s.value for s in ReviewState]}",
            )
    rows = _ledger(graph_name).list_reviews(state=state, type=type, limit=limit)
    return {"reviews": [_as_dict(row) for row in rows]}


@router.post("/{review_id}/approve")
async def approve_review(
    review_id: int,
    decided_by: str = Query(..., max_length=200),
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    """Approve a pending review, then -- in the same request -- apply its
    real effect if `row.type` has one (`_APPLY_FUNCTIONS`; see module
    docstring for the full "approve = one atomic action" rationale and the
    approved-but-apply-failed edge case's exact handling)."""
    row = _ledger(graph_name).approve_review(review_id, decided_by)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"review {review_id} not found or no longer pending",
        )

    apply_fn = _APPLY_FUNCTIONS.get(row.type)
    if apply_fn is None:
        return {"review": _as_dict(row)}

    try:
        applied = apply_fn(_graph(graph_name), _ledger(graph_name), review_id)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"review {review_id} (type={row.type!r}) is now approved, but "
                f"applying it failed: {exc}. The review is NOT re-decidable "
                "(approve/reject only act on a still-pending row) -- it stays "
                "approved-but-not-applied until the apply step is retried "
                "directly against this review id."
            ),
        ) from exc
    return {"review": _as_dict(row), "applied": applied}


@router.post("/{review_id}/reject")
async def reject_review(
    review_id: int,
    decided_by: str = Query(..., max_length=200),
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    row = _ledger(graph_name).reject_review(review_id, decided_by)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"review {review_id} not found or no longer pending",
        )
    return {"review": _as_dict(row)}
