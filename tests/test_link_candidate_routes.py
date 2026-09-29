"""Phase 6 §6.2/§6.5: `demo_ui/backend/link_candidate_routes.py`, the HTTP
surface over `ConnectorLedger`'s `link_candidates` table
(`graph/link_candidates.py`, merged Phase 6.2).

Same conventions `tests/test_reviews.py` already establishes for
`review_routes.py`:

  - list/reject never touch FalkorDB (pure ledger reads/writes), so those
    tests run unconditionally against a standalone FastAPI app built around
    just `link_candidate_routes.router`, pointed at a temp data directory;
  - approve calls `graph.link_candidates.apply_approved_link_candidate`,
    which needs a real graph -- gated behind `NEURON_INTEGRATION=1`, on its
    own uniquely-named graph (never the shared "default" graph), same
    `graph_name` fixture shape as `tests/test_reviews.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from demo_ui.backend import link_candidate_routes
from graph.storage.falkor_client import build_client

integration = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(link_candidate_routes, "DATA_DIR", tmp_path)
    app = FastAPI()
    app.include_router(link_candidate_routes.router)
    return TestClient(app)


@pytest.fixture
def graph_name():
    """See `tests/test_reviews.py::graph_name` for the full rationale --
    same isolated-graph-per-test shape, reused here for the approve test's
    real graph write."""
    name = f"testlinkcand{uuid.uuid4().hex[:12]}"
    yield name
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{name}"
    try:
        build_client().select_graph(falkor_name).delete()
    except Exception:
        pass


def _falkor_graph(graph_name: str):
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{graph_name}"
    return build_client().select_graph(falkor_name)


def _node(graph, label, uid):
    graph.query(
        f"CREATE (n:{label} {{uid: $uid, name: $uid}})", params={"uid": uid},
    )


def _mentioned_in(graph, uid, record_key):
    graph.query(
        """
        MERGE (sr:SourceRecord {record_key: $record_key})
        SET sr.deleted_at = null
        WITH sr
        MATCH (n {uid: $uid})
        CREATE (n)-[:MENTIONED_IN]->(sr)
        """,
        params={"uid": uid, "record_key": record_key},
    )


# --------------------------------------------------------------------- list


def test_api_list_is_empty_for_a_fresh_graph(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/link-candidates")
    assert response.status_code == 200
    assert response.json() == {"candidates": []}


def test_api_list_rejects_invalid_state(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/link-candidates", params={"state": "not-a-real-state"})
    assert response.status_code == 422


def test_api_list_defaults_to_every_state_not_just_pending(tmp_path, monkeypatch):
    """Deliberately different from `ConnectorLedger.list_link_candidates`'s
    own Python default (`state="pending"`) -- this endpoint's default lists
    everything, matching `GET /api/reviews`'s own convention (see
    `list_link_candidates`'s docstring in `link_candidate_routes.py`)."""
    client = _client(tmp_path, monkeypatch)
    ledger = link_candidate_routes._ledger("default")
    pending_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    rejected_id = ledger.create_link_candidate("c", "d", "OWNS", derived_rule="semantic_candidate")
    ledger.reject_link_candidate(rejected_id)

    listed = client.get("/api/link-candidates").json()["candidates"]
    assert {row["id"] for row in listed} == {pending_id, rejected_id}

    pending_only = client.get("/api/link-candidates", params={"state": "pending"}).json()["candidates"]
    assert [row["id"] for row in pending_only] == [pending_id]

    two_hop_only = client.get(
        "/api/link-candidates", params={"derived_rule": "two_hop"},
    ).json()["candidates"]
    assert [row["id"] for row in two_hop_only] == [pending_id]


# ------------------------------------------------------------------ reject


def test_api_reject_end_to_end(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ledger = link_candidate_routes._ledger("default")
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")

    response = client.post(f"/api/link-candidates/{candidate_id}/reject")
    assert response.status_code == 200
    body = response.json()["candidate"]
    assert body["state"] == "rejected"

    # Rejection is cached by the row's own state (§6.2's UNIQUE triple), not
    # a separate table like `reviews`' rejection-identity cache -- a later
    # `create_link_candidate` for the same triple must not resurrect it.
    same_triple_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    assert same_triple_id == candidate_id
    assert ledger.get_link_candidate(candidate_id).state == "rejected"


def test_api_reject_unknown_candidate_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/link-candidates/12345/reject")
    assert response.status_code == 404


def test_api_reject_already_decided_candidate_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ledger = link_candidate_routes._ledger("default")
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    ledger.approve_link_candidate(candidate_id)

    response = client.post(f"/api/link-candidates/{candidate_id}/reject")
    assert response.status_code == 404


# ------------------------------------------------------------------ approve


@integration
def test_api_approve_writes_derived_edge_with_union_provenance(tmp_path, monkeypatch, graph_name):
    """End-to-end: HTTP approve -> ledger state flip -> real derived edge,
    same "approve = one atomic action" shape `review_routes.py`'s dispatch
    uses, exercised here through the dedicated link-candidate router."""
    client = _client(tmp_path, monkeypatch)
    g = _falkor_graph(graph_name)
    _node(g, "WorkItem", "a")
    _node(g, "WorkItem", "b")
    _mentioned_in(g, "a", "sr-a")
    _mentioned_in(g, "b", "sr-b")

    ledger = link_candidate_routes._ledger(graph_name)
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")

    response = client.post(
        f"/api/link-candidates/{candidate_id}/approve", params={"graph_name": graph_name},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["state"] == "approved"
    assert body["applied"] is True

    rows = g.query(
        "MATCH (a {uid:'a'})-[r:REFERENCES]->(b {uid:'b'}) "
        "RETURN r.derived, r.extraction_method, r.source_record_keys",
    ).result_set
    assert len(rows) == 1
    is_derived, method, keys = rows[0]
    assert is_derived is True
    assert method == "derived"
    assert set(keys) == {"sr-a", "sr-b"}


@integration
def test_api_approve_missing_endpoint_is_200_with_applied_false(tmp_path, monkeypatch, graph_name):
    """`apply_approved_link_candidate` never raises -- a missing endpoint
    node (e.g. merged/deleted since the candidate was proposed) returns
    `False`, not an exception. The candidate is still `approved` in the
    ledger either way (see `link_candidate_routes.py`'s module docstring for
    why that is surfaced as `"applied": false` rather than an HTTP error)."""
    client = _client(tmp_path, monkeypatch)
    g = _falkor_graph(graph_name)
    _node(g, "WorkItem", "a")
    # "b" never created.

    ledger = link_candidate_routes._ledger(graph_name)
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")

    response = client.post(
        f"/api/link-candidates/{candidate_id}/approve", params={"graph_name": graph_name},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["state"] == "approved"
    assert body["applied"] is False


def test_api_approve_unknown_candidate_is_404(tmp_path, monkeypatch):
    """No graph touch happens before the 404 -- `approve_link_candidate`
    returns `None` immediately for a missing/non-pending row, so this runs
    without `NEURON_INTEGRATION`."""
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/link-candidates/12345/approve")
    assert response.status_code == 404
