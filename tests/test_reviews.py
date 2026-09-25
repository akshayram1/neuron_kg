"""Phase 3 §3.0 "Minimal review queue": `ConnectorLedger`'s `reviews` /
`review_rejections` tables and the `demo_ui/backend/review_routes.py`
endpoints built on top of them. Extended for Phase 6 §6.5's "approve = one
atomic action" dispatch (`review_routes._APPLY_FUNCTIONS`).

Ledger-level tests exercise a real temp-file `ConnectorLedger`, the same
convention as `tests/test_sync_coverage.py` and `tests/test_ledger_priority.py`
-- no mocking. The route-level tests build a small standalone FastAPI app
around just `review_routes.router` (not the full `demo_ui.backend.app`,
whose startup event needs a live FalkorDB) and drive it with a real
`TestClient`, pointed at a temp data directory so it never touches real
ledger files.

The §6.5 dispatch tests below additionally need a real FalkorDB (approving a
`fact_update`/`duplicate_pair`/`possibly_same_as` review now calls
`review_routes._graph`, which opens a real connection even when the apply
function itself never queries it -- verified directly: constructing
`falkordb.FalkorDB(...)` eagerly pings the server) -- gated behind
`NEURON_INTEGRATION=1`, same convention as `tests/test_resolve_text_fact.py`.
They run against their own uniquely-named graph (`graph_name` fixture below),
never the shared "default" graph every other test in this file's route-level
section implicitly targets (those never reach `_graph`, since none of their
review types have an apply function).
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from connectors.core.ledger import ConnectorLedger, ReviewState
from demo_ui.backend import review_routes
from graph import writer as w
from graph.falkor_client import build_client
from graph.resolve_text_fact import NewFact, OldFact, resolve_text_fact

integration = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)

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
    """Generic queue plumbing (list/approve/reject/rejection-cache), for a
    review `type` that has no Phase 6.5 apply-on-approval step --
    `polarity_conflict_candidate` (§4.1, real type, never wired to an apply
    function) rather than `possibly_same_as`, since approving a
    `possibly_same_as` review now dispatches to
    `apply_approved_possibly_same_as`, which always raises (see
    `test_approve_possibly_same_as_is_approved_but_apply_fails` below) --
    this test is specifically about the "no apply step" no-regression case."""
    client = _client(tmp_path, monkeypatch)
    ledger = review_routes._ledger("default")
    approve_id = ledger.create_review(
        "polarity_conflict_candidate", {"subject_uid": "a", "object_uid": "b"},
    )
    reject_id = ledger.create_review(
        "polarity_conflict_candidate", {"subject_uid": "c", "object_uid": "d"},
        identity="polarity_conflict_candidate:c:d",
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
    assert ledger.is_identity_rejected("polarity_conflict_candidate:c:d") is True

    pending_after = client.get("/api/reviews", params={"state": "pending"}).json()["reviews"]
    assert pending_after == []

    approved_after = client.get(
        "/api/reviews", params={"state": "approved", "type": "polarity_conflict_candidate"},
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


# ----------------------------------------------- §6.5 apply-on-approval dispatch


@pytest.fixture
def graph_name():
    """A throwaway, uniquely-named graph slug -- `multigraph.resolve` maps a
    non-`"default"` slug to its own isolated FalkorDB graph
    (`<FALKOR_GRAPH>__<slug>`) and ledger file (Phase 0's multigraph work),
    so these dispatch tests' real graph writes never touch the shared
    "default" graph every other test in this file implicitly targets."""
    name = f"testdispatch{uuid.uuid4().hex[:12]}"
    yield name
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{name}"
    try:
        build_client().select_graph(falkor_name).delete()
    except Exception:
        # A test that never wrote to this graph (e.g. the `possibly_same_as`
        # dispatch test, which fails before any graph write) leaves no real
        # key behind -- FalkorDB errors on deleting one that was never
        # created ("Invalid graph operation on empty key"). Nothing to clean
        # up in that case.
        pass


def _falkor_graph(graph_name: str):
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{graph_name}"
    return build_client().select_graph(falkor_name)


@integration
def test_api_approve_fact_update_triggers_real_apply(tmp_path, monkeypatch, graph_name):
    """End-to-end: HTTP approve -> ledger state flip -> real graph mutation,
    for the review shape `resolve_text_fact`'s own `suggest` branch produces
    (§6.5's "fact_update -> apply proposed close / dispute")."""
    client = _client(tmp_path, monkeypatch)
    g = _falkor_graph(graph_name)
    w.upsert_entities(g, "Person", [{"uid": "p1", "props": {"name": "Alice"}}])
    w.upsert_entities(g, "Term", [{"uid": "t1", "props": {"name": "Rate Limit"}}])
    w.upsert_entities(g, "Term", [{"uid": "t2", "props": {"name": "Retry Policy"}}])
    w.upsert_fact_edges(g, "OWNS", "Person", "Term", [{
        "from_uid": "p1", "to_uid": "t1", "source_record_keys": ["rec-old"],
        "evidence": "old evidence", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    old_fact_uid = g.query(
        "MATCH (:Person {uid:'p1'})-[r:OWNS]->(:Term {uid:'t1'}) RETURN r.fact_uid"
    ).result_set[0][0]
    old = OldFact(
        fact_uid=old_fact_uid, from_uid="p1", from_label="Person",
        to_uid="t1", to_label="Term", rel_type="OWNS", valid_at="2026-01-01T00:00:00Z",
    )
    new = NewFact(
        from_uid="p1", from_label="Person", to_uid="t2", to_label="Term",
        rel_type="OWNS", source_time="2026-06-01T00:00:00Z", source_record_key="rec-new",
        valid_at="2026-06-01T00:00:00Z", evidence="new evidence", confidence=0.8,
    )
    ledger = review_routes._ledger(graph_name)
    suggested = resolve_text_fact(g, ledger, new, old, "newer_state", mode="suggest")
    review_id = ledger.list_reviews(type="fact_update")[0].id

    response = client.post(
        f"/api/reviews/{review_id}/approve",
        params={"decided_by": "tester", "graph_name": graph_name},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review"]["state"] == "approved"
    assert body["applied"]["action"] == "newer_state_close"
    assert body["applied"]["new_fact_uid"] == suggested["new_fact_uid"]

    old_row = g.query(
        "MATCH ()-[r]->() WHERE r.fact_uid = $fu RETURN r.invalid_at",
        params={"fu": old_fact_uid},
    ).result_set[0]
    assert old_row[0] == "2026-06-01T00:00:00Z"
    new_row = g.query(
        "MATCH ()-[r]->() WHERE r.fact_uid = $fu RETURN r.projection_status",
        params={"fu": suggested["new_fact_uid"]},
    ).result_set[0]
    assert (new_row[0] or "live") == "live"


@integration
def test_api_approve_duplicate_pair_triggers_real_apply(tmp_path, monkeypatch, graph_name):
    """End-to-end for §6.5's "duplicate_pair -> merge that approved pair" --
    same fixture shape as
    `tests/test_duplicate_collector.py::TestApplyApprovedDuplicateMerge`,
    driven through the real HTTP endpoint instead of calling
    `apply_approved_duplicate_merge` directly."""
    client = _client(tmp_path, monkeypatch)
    g = _falkor_graph(graph_name)
    w.upsert_entities(g, "Term", [{"uid": "term-a", "props": {"name": "Redis rate limiter"}}])
    w.upsert_entities(g, "Term", [{"uid": "term-b", "props": {"name": "Redis limiter"}}])
    w.upsert_entities(g, "System", [{"uid": "sys-1", "props": {"name": "Redis"}}])
    w.upsert_fact_edges(g, "APPLIES_TO", "Term", "System", [{
        "from_uid": "term-a", "to_uid": "sys-1", "source_record_keys": ["r1", "r2"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    w.upsert_fact_edges(g, "APPLIES_TO", "Term", "System", [{
        "from_uid": "term-b", "to_uid": "sys-1", "source_record_keys": ["r3"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])

    ledger = review_routes._ledger(graph_name)
    review_id = ledger.create_review(
        "duplicate_pair",
        {"label": "Term", "uid_a": "term-a", "uid_b": "term-b", "score": 0.8, "signals": {}},
        identity="duplicate_pair:Term:term-a:term-b",
    )

    response = client.post(
        f"/api/reviews/{review_id}/approve",
        params={"decided_by": "tester", "graph_name": graph_name},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review"]["state"] == "approved"
    # term-a has 2 reinforcing source records vs term-b's 1 -- more
    # reinforced, so it survives (`_choose_survivor`'s own tiebreak rule).
    assert body["applied"]["survivor_uid"] == "term-a"
    assert body["applied"]["absorbed_uid"] == "term-b"

    redirected = g.query(
        "MATCH (a {uid:'term-a'})-[r:APPLIES_TO]->(s {uid:'sys-1'}) "
        "RETURN r.invalid_at, r.source_record_keys",
    ).result_set
    assert redirected and redirected[0][0] is None
    assert set(redirected[0][1]) == {"r1", "r2", "r3"}


@integration
def test_api_approve_possibly_same_as_is_approved_but_apply_fails(tmp_path, monkeypatch, graph_name):
    """No real `possibly_same_as` review is ever proposed today
    (`apply_approved_possibly_same_as` always raises `NotImplementedError` --
    see `graph/resolve_text_fact.py`), so approving one through the unified
    endpoint must surface that failure loudly (500, `review_routes.py`'s
    documented approved-but-apply-failed edge case) rather than silently
    reporting success -- and the ledger's state flip, which already
    committed before the apply step ran, must not be hidden or rolled back."""
    client = _client(tmp_path, monkeypatch)
    ledger = review_routes._ledger(graph_name)
    review_id = ledger.create_review(
        "possibly_same_as", {"subject_uid": "a", "object_uid": "b"},
    )

    response = client.post(
        f"/api/reviews/{review_id}/approve",
        params={"decided_by": "tester", "graph_name": graph_name},
    )

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert str(review_id) in detail
    assert "approved" in detail

    row = ledger.get_review(review_id)
    assert row.state == ReviewState.APPROVED
    assert row.decided_by == "tester"
