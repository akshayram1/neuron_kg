"""Review queue endpoints (25-plan.md Phase 3 §3.0 "Minimal review queue").

A `reviews` row is a proposal a human has to accept or decline before it
takes effect -- `possibly_same_as` (Phase 4) and `fact_update` (Phase 5)
candidates today, and later link/duplicate candidates the plan does not name
yet. This module only serves the queue: creating, listing, approving and
rejecting rows lives on `ConnectorLedger` (`connectors/core/ledger.py`,
`reviews`/`review_rejections` tables); this is the same split
`sync_coverage_routes.py` uses for its read side. `BridgePanel` and any
dashboard for this queue are Phase 6, not here.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query

from connectors.core.ledger import ConnectorLedger, Review, ReviewState
from graph import multigraph
from graph import vector_store as vector_store_module
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/reviews", tags=["reviews"])


def _ledger(graph_name: str) -> ConnectorLedger:
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    return ConnectorLedger(target.ledger_path)


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
    row = _ledger(graph_name).approve_review(review_id, decided_by)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"review {review_id} not found or no longer pending",
        )
    return {"review": _as_dict(row)}


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
