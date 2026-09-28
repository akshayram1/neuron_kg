"""graph.finding_bridge.sync_ledger_findings: bringing ledger findings
(connectors.core.ledger.ConnectorLedger.record_ingestion_assessments) into
FalkorDB as `Finding` nodes, the last missing piece after that ledger method
started actually persisting an LLM's `should_flag=True` ingestion judgements.

Needs a real FalkorDB (`Finding` nodes, MENTIONED_IN edges, embeddings) --
gated behind `NEURON_INTEGRATION=1`, same convention as tests/test_reviews.py.
Runs against its own uniquely-named graph, never the shared "default" graph.
"""

from __future__ import annotations

import os
import uuid

import pytest

from connectors.core.ledger import ConnectorLedger
from graph import vector_store
from graph import writer as w
from graph.falkor_client import build_client
from graph.finding_bridge import sync_ledger_findings
from graph.schema import bootstrap_schema

integration = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


@pytest.fixture
def graph_name():
    name = f"testfindingbridge{uuid.uuid4().hex[:12]}"
    yield name
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{name}"
    try:
        build_client().select_graph(falkor_name).delete()
    except Exception:
        pass


def _falkor_graph(graph_name: str):
    falkor_name = f"{os.getenv('FALKOR_GRAPH', 'neuron')}__{graph_name}"
    return build_client().select_graph(falkor_name)


def _seed_source_record(graph, record_key: str) -> None:
    w.upsert_source_records(graph, [{
        "record_key": record_key, "provider": "notion", "connection_id": "test-conn",
        "entity_type": "document", "name": "Test doc", "content_hash": "h",
    }])


@integration
def test_open_finding_becomes_a_mentioned_in_node(tmp_path, graph_name):
    graph = _falkor_graph(graph_name)
    bootstrap_schema(graph)
    vector_store.ensure_collection(vector_store.client())
    record_key = "notion:test-conn:document:page-1"
    _seed_source_record(graph, record_key)

    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_ingestion_assessments(record_key, "chunk-1", [{
        "should_flag": True, "action": "contradiction", "topic_key": "auth-v1-removal",
        "title": "Auth v1 still called", "summary": "A consumer still calls it.",
        "reasoning": "Evidence says so.", "evidence": "still calling /v1", "severity": "high",
        "confidence": 0.75,
    }])

    synced = sync_ledger_findings(graph, ledger, record_prefix="notion:test-conn:")
    assert synced == 1

    rows = graph.query(
        "MATCH (f:Finding)-[:MENTIONED_IN]->(sr:SourceRecord {record_key: $rk}) "
        "RETURN f.name, f.status, f.severity, f.kind",
        params={"rk": record_key},
    ).result_set
    assert len(rows) == 1
    name, status, severity, kind = rows[0]
    assert name == "Auth v1 still called"
    assert status == "open"
    assert severity == "high"
    assert kind == "llm_contradiction"


@integration
def test_stale_finding_stays_visible_with_stale_status(tmp_path, graph_name):
    graph = _falkor_graph(graph_name)
    bootstrap_schema(graph)
    vector_store.ensure_collection(vector_store.client())
    record_key = "notion:test-conn:document:page-1"
    _seed_source_record(graph, record_key)

    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assessment = {
        "should_flag": True, "action": "review", "topic_key": "something",
        "title": "Needs review", "summary": "s", "reasoning": "r", "evidence": "e",
    }
    ledger.record_ingestion_assessments(record_key, "chunk-1", [assessment])
    ledger.record_ingestion_assessments(record_key, "chunk-1", [])  # reprocessed, nothing flagged now

    sync_ledger_findings(graph, ledger, record_prefix="notion:test-conn:")
    rows = graph.query(
        "MATCH (f:Finding)-[:MENTIONED_IN]->(:SourceRecord {record_key: $rk}) RETURN f.status",
        params={"rk": record_key},
    ).result_set
    assert [r[0] for r in rows] == ["stale"]


@integration
def test_sync_is_idempotent(tmp_path, graph_name):
    graph = _falkor_graph(graph_name)
    bootstrap_schema(graph)
    vector_store.ensure_collection(vector_store.client())
    record_key = "notion:test-conn:document:page-1"
    _seed_source_record(graph, record_key)

    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_ingestion_assessments(record_key, "chunk-1", [{
        "should_flag": True, "action": "addition", "topic_key": "x", "title": "T",
        "summary": "s", "reasoning": "r", "evidence": "e",
    }])

    assert sync_ledger_findings(graph, ledger) == 1
    assert sync_ledger_findings(graph, ledger) == 1  # re-run: no duplicate node
    count = graph.query("MATCH (f:Finding) RETURN count(f)").result_set[0][0]
    assert count == 1
