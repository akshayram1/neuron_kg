from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from connectors.core.ledger import HygieneCount
from demo_ui.backend import dashboard_routes


def test_dashboard_aggregates_health_queue_resolution_and_eval(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard_routes, "DATA_DIR", tmp_path)
    results = tmp_path / "results.md"
    results.write_text("# Results\n\n## Latest run\n\n| metric | value |\n|---|---|\n| recall@k | 0.75 |\n")
    monkeypatch.setattr(dashboard_routes, "RESULTS_PATH", results)
    ledger = dashboard_routes._ledger("default")
    ledger.record_hygiene_counts("h1", [
        HygieneCount("Document", 2, 10),
        HygieneCount("cardinality_violation", 0, 0),
    ])
    ledger.create_review("fact_update", {"fact_uid": "f1"})
    ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    ledger.record_resolution("r1", "Term", "alias", 3)
    ledger.record_merge_trace("keep", "drop", "Term")
    app = FastAPI()
    app.include_router(dashboard_routes.router)

    response = TestClient(app).get("/api/dashboard")

    assert response.status_code == 200
    body = response.json()
    assert body["queue"] == {"fact_update": 1, "link_candidate": 1}
    assert {row["label"]: row["ratio"] for row in body["hygiene"]}["Document"] == 0.2
    assert body["resolution"][0]["count"] == 3
    assert body["merges"][0]["absorbed_uid"] == "drop"
    assert body["latest_eval"]["metrics"]["recall@k"] == "0.75"
