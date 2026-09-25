"""Phase 3 §3.0 "Minimal review queue": `ConnectorLedger`'s `reviews` /
`review_rejections` tables and the `demo_ui/backend/review_routes.py`
endpoints built on top of them.

Ledger-level tests exercise a real temp-file `ConnectorLedger`, the same
convention as `tests/test_sync_coverage.py` and `tests/test_ledger_priority.py`
-- no mocking. The route-level test builds a small standalone FastAPI app
around just `review_routes.router` (not the full `demo_ui.backend.app`,
whose startup event needs a live FalkorDB) and drives it with a real
`TestClient`, pointed at a temp data directory so it never touches real
ledger files.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from connectors.core.ledger import ConnectorLedger, ReviewState
from demo_ui.backend import review_routes

# --------------------------------------------------------------- ledger-level


def test_create_review_starts_pending(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    review_id = ledger.create_review(
        "possibly_same_as", {"subject_uid": "a", "object_uid": "b"},
        identity="possibly_same_as:a:b",
    )
    assert review_id is not None

    row = ledger.get_review(review_id)
    assert row.type == "possibly_same_as"
    assert row.payload == {"subject_uid": "a", "object_uid": "b"}
    assert row.identity == "possibly_same_as:a:b"
    assert row.state == ReviewState.PENDING
    assert row.decided_by is None
    assert row.decided_at is None
    assert row.created_at


def test_create_review_without_identity_is_allowed(tmp_path):
    """`identity` is optional -- a caller with no stable key yet still gets
    a plain pending row, just without dedupe."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    review_id = ledger.create_review("fact_update", {"note": "no identity here"})
    assert review_id is not None
    assert ledger.get_review(review_id).identity is None


def test_approve_transition_sets_decided_fields(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    review_id = ledger.create_review("fact_update", {"claim": "x"})

    decided = ledger.approve_review(review_id, "akshay.chame@tmdc.io")
    assert decided.state == ReviewState.APPROVED
    assert decided.decided_by == "akshay.chame@tmdc.io"
    assert decided.decided_at

    # Persisted, not just returned in-memory.
    reloaded = ledger.get_review(review_id)
    assert reloaded.state == ReviewState.APPROVED
    assert reloaded.decided_by == "akshay.chame@tmdc.io"
    assert reloaded.decided_at == decided.decided_at


def test_reject_transition_sets_decided_fields(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    review_id = ledger.create_review("fact_update", {"claim": "y"})

    decided = ledger.reject_review(review_id, "reviewer-1")
    assert decided.state == ReviewState.REJECTED
    assert decided.decided_by == "reviewer-1"
    assert decided.decided_at

    reloaded = ledger.get_review(review_id)
    assert reloaded.state == ReviewState.REJECTED


def test_deciding_an_already_decided_review_is_a_noop(tmp_path):
    """`approve_review`/`reject_review` only act on a still-PENDING row, so a
    decided review cannot be silently re-decided out from under whoever
    already acted on it."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    review_id = ledger.create_review("fact_update", {"claim": "z"})
    ledger.approve_review(review_id, "first-reviewer")

    assert ledger.approve_review(review_id, "second-reviewer") is None
    assert ledger.reject_review(review_id, "second-reviewer") is None
    # Still shows the original decision.
    row = ledger.get_review(review_id)
    assert row.state == ReviewState.APPROVED
    assert row.decided_by == "first-reviewer"


def test_deciding_a_nonexistent_review_returns_none(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    assert ledger.approve_review(9999, "reviewer") is None
    assert ledger.reject_review(9999, "reviewer") is None


# ------------------------------------------------------------ rejection dedupe


def test_rejected_identity_blocks_recreation(tmp_path):
    """A proposal with the same identity as a previously-rejected one is not
    recreated as a new pending row -- plan.md Phase 3 §3.0's "Cache
    rejection identities so the same proposal is not recreated"."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    identity = "possibly_same_as:node-1:node-2"

    first_id = ledger.create_review(
        "possibly_same_as", {"subject_uid": "node-1", "object_uid": "node-2"},
        identity=identity,
    )
    ledger.reject_review(first_id, "reviewer-1")

    assert ledger.is_identity_rejected(identity) is True

    # A later run proposes the logically-identical merge again (even with
    # different payload bytes/ordering) -- it must not resurrect a new row.
    second_id = ledger.create_review(
        "possibly_same_as", {"object_uid": "node-2", "subject_uid": "node-1", "score": 0.91},
        identity=identity,
    )
    assert second_id is None
    assert len(ledger.list_reviews(type="possibly_same_as")) == 1


def test_approved_identity_does_not_block_recreation(tmp_path):
    """Only REJECTED identities are cached -- an approved proposal already
    took effect, so there is nothing to guard against re-proposing (and a
    caller may legitimately want to propose a fresh related update)."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    identity = "fact_update:node-1"

    first_id = ledger.create_review("fact_update", {"a": 1}, identity=identity)
    ledger.approve_review(first_id, "reviewer-1")

    assert ledger.is_identity_rejected(identity) is False
    second_id = ledger.create_review("fact_update", {"a": 2}, identity=identity)
    assert second_id is not None
    assert second_id != first_id


def test_is_identity_rejected_false_for_unknown_identity(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    assert ledger.is_identity_rejected("never-seen") is False


# --------------------------------------------------------------- listing


def test_list_reviews_filters_by_state_and_type(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    pending_id = ledger.create_review("possibly_same_as", {"i": 1})
    approved_id = ledger.create_review("possibly_same_as", {"i": 2})
    rejected_id = ledger.create_review("fact_update", {"i": 3})
    ledger.approve_review(approved_id, "r")
    ledger.reject_review(rejected_id, "r")

    all_rows = ledger.list_reviews()
    assert {row.id for row in all_rows} == {pending_id, approved_id, rejected_id}

    pending_only = ledger.list_reviews(state=ReviewState.PENDING)
    assert [row.id for row in pending_only] == [pending_id]

    same_as_only = ledger.list_reviews(type="possibly_same_as")
    assert {row.id for row in same_as_only} == {pending_id, approved_id}

    rejected_fact_updates = ledger.list_reviews(state=ReviewState.REJECTED, type="fact_update")
    assert [row.id for row in rejected_fact_updates] == [rejected_id]

    assert ledger.list_reviews(type="does_not_exist") == []


def test_list_reviews_newest_first(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    first = ledger.create_review("fact_update", {"n": 1})
    second = ledger.create_review("fact_update", {"n": 2})
    third = ledger.create_review("fact_update", {"n": 3})

    ids_in_order = [row.id for row in ledger.list_reviews()]
    assert ids_in_order[0] == third
    assert ids_in_order[-1] == first
    assert second in ids_in_order


# ------------------------------------------------------------------ API route


def _client(tmp_path, monkeypatch) -> TestClient:
    # Point the router at an isolated temp data dir so it never touches a
    # real ledger file, and mount only `review_routes.router` -- the full
    # `demo_ui.backend.app` has a startup event that needs a live FalkorDB.
    monkeypatch.setattr(review_routes, "DATA_DIR", tmp_path)
    app = FastAPI()
    app.include_router(review_routes.router)
    return TestClient(app)


def test_api_list_is_empty_for_a_fresh_graph(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/reviews")
    assert response.status_code == 200
    assert response.json() == {"reviews": []}


def test_api_list_rejects_invalid_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/reviews", params={"state": "not-a-real-state"})
    assert response.status_code == 422


def test_api_approve_and_reject_end_to_end(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ledger = review_routes._ledger("default")
    approve_id = ledger.create_review("possibly_same_as", {"subject_uid": "a", "object_uid": "b"})
    reject_id = ledger.create_review(
        "possibly_same_as", {"subject_uid": "c", "object_uid": "d"},
        identity="possibly_same_as:c:d",
    )

    listed = client.get("/api/reviews").json()["reviews"]
    assert {row["id"] for row in listed} == {approve_id, reject_id}
    assert all(row["state"] == "pending" for row in listed)

    approve_response = client.post(
        f"/api/reviews/{approve_id}/approve", params={"decided_by": "akshay.chame@tmdc.io"},
    )
    assert approve_response.status_code == 200
    approved = approve_response.json()["review"]
    assert approved["state"] == "approved"
    assert approved["decided_by"] == "akshay.chame@tmdc.io"

    reject_response = client.post(
        f"/api/reviews/{reject_id}/reject", params={"decided_by": "akshay.chame@tmdc.io"},
    )
    assert reject_response.status_code == 200
    rejected = reject_response.json()["review"]
    assert rejected["state"] == "rejected"

    # The rejection identity is now cached at the ledger level.
    assert ledger.is_identity_rejected("possibly_same_as:c:d") is True

    pending_after = client.get("/api/reviews", params={"state": "pending"}).json()["reviews"]
    assert pending_after == []

    approved_after = client.get(
        "/api/reviews", params={"state": "approved", "type": "possibly_same_as"},
    ).json()["reviews"]
    assert [row["id"] for row in approved_after] == [approve_id]


def test_api_approve_unknown_review_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/reviews/12345/approve", params={"decided_by": "someone"})
    assert response.status_code == 404


def test_api_reject_already_decided_review_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ledger = review_routes._ledger("default")
    review_id = ledger.create_review("fact_update", {"claim": "already decided"})
    ledger.approve_review(review_id, "first-reviewer")

    response = client.post(
        f"/api/reviews/{review_id}/reject", params={"decided_by": "second-reviewer"},
    )
    assert response.status_code == 404
