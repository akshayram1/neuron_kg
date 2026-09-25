"""Link candidate queue endpoints (25-plan.md Phase 6 §6.2 "Candidate links
for isolated nodes", surfaced by §6.5 "Review queue UI").

A `link_candidates` row is a proposed derived edge between two nodes,
produced by `graph/link_candidates.py`'s two-hop/semantic candidate sources
plus Laya's `relation_type` gate -- not yet a real graph edge. This is a
separate table (and separate router) from the generic `reviews` table
(`demo_ui/backend/review_routes.py`): `graph/link_candidates.py`'s own module
docstring and `connectors.core.ledger.LinkCandidate`'s docstring both
document why -- edge-specific columns (`from_uid`/`to_uid`/`relation`/
`confidence`/`derived_rule`) rather than the generic `reviews` table's
opaque `payload` blob, plus its own simpler idempotency mechanism (a UNIQUE
`(from_uid, to_uid, relation)` triple instead of `reviews`' rejection-identity
cache -- see `ConnectorLedger.create_link_candidate`'s docstring).

Same "approve = one atomic action" convention `review_routes.py` establishes
for the generic queue (§6.5's own "Approve does: <action>" framing): approving
a candidate here calls `ledger.approve_link_candidate` AND, in the same
request, `graph.link_candidates.apply_approved_link_candidate` to actually
write the derived edge -- a human does not separately click "apply" after
approving. Rejecting never applies anything (nothing to undo -- rejection is
terminal per §6.2/§6.5's "flag, not delete" rule, already enforced by the
row's own state column, see `reject_link_candidate`'s docstring).

Unlike `review_routes.py`'s dispatch, `apply_approved_link_candidate` never
raises -- it returns `False` for "no write happened" (missing/rejected
candidate, or an endpoint node that no longer exists). The candidate is
already flipped to `approved` in the ledger by the time that bool comes
back (same approved-but-not-applied possibility `review_routes.py`'s module
docstring documents for its own dispatch), so this module surfaces it
plainly in the response body (`"applied": false`) rather than manufacturing
an HTTP error status for a return value that was never an exception.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from connectors.core.ledger import ConnectorLedger, LinkCandidate, ReviewState
from graph import multigraph
from graph import vector_store as vector_store_module
from graph.falkor_client import get_graph
from graph.link_candidates import apply_approved_link_candidate
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/link-candidates", tags=["link-candidates"])


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


def _as_dict(row: LinkCandidate) -> dict:
    return {
        "id": row.id,
        "from_uid": row.from_uid,
        "to_uid": row.to_uid,
        "relation": row.relation,
        "confidence": row.confidence,
        "derived_rule": row.derived_rule,
        "state": row.state,
        "created_at": row.created_at,
        "decided_at": row.decided_at,
    }


@router.get("")
async def list_link_candidates(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
    state: str | None = Query(default=None, max_length=20),
    derived_rule: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """List link candidates for this graph, optionally filtered by `state`
    (pending/approved/rejected) and/or `derived_rule` (`two_hop` /
    `semantic_candidate`). Unlike `ConnectorLedger.list_link_candidates`'s
    own Python default (`state="pending"` -- its primary read shape), this
    endpoint defaults to no state filter at all, matching
    `GET /api/reviews`'s own "list everything unless told otherwise"
    convention -- a caller that specifically wants the review-queue's usual
    "what needs a decision" view passes `state=pending` explicitly."""
    if state is not None:
        try:
            ReviewState(state)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"invalid state {state!r}; expected one of "
                       f"{[s.value for s in ReviewState]}",
            )
    rows = _ledger(graph_name).list_link_candidates(
        state=state, derived_rule=derived_rule, limit=limit,
    )
    return {"candidates": [_as_dict(row) for row in rows]}


@router.post("/{candidate_id}/approve")
async def approve_link_candidate(
    candidate_id: int,
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    """Approve a pending candidate, then -- in the same request -- write the
    real derived edge via `graph.link_candidates.apply_approved_link_candidate`
    (§6.2: `derived=True`, `extraction_method="derived"`). See module
    docstring for why a `False` result is surfaced as `"applied": false`
    rather than an HTTP error: the candidate is already `approved` either
    way, and `apply_approved_link_candidate` itself never raises."""
    row = _ledger(graph_name).approve_link_candidate(candidate_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"link candidate {candidate_id} not found or no longer pending",
        )
    applied = apply_approved_link_candidate(_graph(graph_name), _ledger(graph_name), candidate_id)
    return {"candidate": _as_dict(row), "applied": applied}


@router.post("/{candidate_id}/reject")
async def reject_link_candidate(
    candidate_id: int,
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    """Reject a pending candidate. No apply step -- rejecting never writes a
    graph edge. `reject_link_candidate`'s own UNIQUE-triple-backed state
    column already keeps this from being re-proposed as pending (see module
    docstring's "Rejections are cached" note)."""
    row = _ledger(graph_name).reject_link_candidate(candidate_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"link candidate {candidate_id} not found or no longer pending",
        )
    return {"candidate": _as_dict(row)}
