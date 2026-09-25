"""Tests for graph/hygiene.py (25-plan.md Phase 6 §6.1 isolated-node report,
§6.3 cardinality/contradiction audit).

Same convention as tests/test_expand.py and tests/test_writer_temporal.py:
a real FalkorDB via `NEURON_INTEGRATION=1`, one throwaway graph per test,
deleted in `finally`. `ConnectorLedger` uses pytest's `tmp_path` for a real
on-disk SQLite file, no mocking -- these tests exercise the actual Cypher
in graph/hygiene.py and the actual ledger accessors in
connectors/core/ledger.py (already merged; only exercised here, not
modified).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from connectors.core.ledger import ConnectorLedger
from graph.axioms import DEFAULT_AXIOMS
from graph.falkor_client import build_client
from graph.hygiene import (
    HygieneReport,
    IsolatedNodeCount,
    cardinality_violations,
    isolated_node_report,
    open_disputes,
    run_hygiene_checks,
)

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_hygiene_{uuid4().hex}")
    try:
        yield g
    finally:
        g.delete()


@pytest.fixture
def ledger(tmp_path):
    return ConnectorLedger(tmp_path / "ledger.sqlite3")


def _node(graph, label: str, uid: str, name: str | None = None) -> None:
    graph.query(
        f"CREATE (n:{label} {{uid: $uid, name: $name}})",
        params={"uid": uid, "name": name or uid},
    )


def _edge(
    graph, rel_type: str, from_uid: str, to_uid: str, *,
    invalid_at: str | None = None,
    assertion_status: str | None = None,
    projection_status: str | None = None,
) -> None:
    """Create one relationship with an explicit liveness shape -- lets tests
    build a `corrected`/`pending_review`/closed edge directly, without going
    through graph/writer.py's higher-level supersede/correct machinery."""
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}})
        CREATE (a)-[r:{rel_type} {{
            invalid_at: $invalid_at,
            assertion_status: $assertion_status,
            projection_status: $projection_status,
            first_seen_at: $now
        }}]->(b)
        """,
        params={
            "from_uid": from_uid, "to_uid": to_uid,
            "invalid_at": invalid_at, "assertion_status": assertion_status,
            "projection_status": projection_status, "now": "2026-01-01T00:00:00Z",
        },
    )


def _disputed_with(graph, a_uid: str, b_uid: str) -> None:
    """Mirrors exactly how graph.resolve_text_fact.link_disputed writes a
    DISPUTED_WITH edge: single directed a->b, MERGE, only first_seen_at/
    last_confirmed_at, never invalid_at."""
    graph.query(
        """
        MATCH (a {uid: $a}), (b {uid: $b})
        MERGE (a)-[r:DISPUTED_WITH]->(b)
        ON CREATE SET r.first_seen_at = $now
        ON MATCH SET r.last_confirmed_at = $now
        """,
        params={"a": a_uid, "b": b_uid, "now": "2026-01-01T00:00:00Z"},
    )


# --------------------------------------------------------------------- §6.1


def test_isolated_document_report_counts_and_lists(graph, ledger):
    _node(graph, "Document", "doc1")
    _node(graph, "Document", "doc2")
    _node(graph, "WorkItem", "wi1")
    _edge(graph, "DOCUMENTS", "doc1", "wi1")  # doc1 is connected

    report = isolated_node_report(graph, ledger)
    doc = report["Document"]
    assert isinstance(doc, IsolatedNodeCount)
    assert doc.total_count == 2
    assert doc.isolated_count == 1
    assert doc.isolated_uids == ["doc2"]


def test_isolated_document_with_edge_is_not_isolated(graph, ledger):
    """Negative control: a Document WITH a live DOCUMENTS edge must not be
    counted as isolated."""
    _node(graph, "Document", "doc1")
    _node(graph, "WorkItem", "wi1")
    _edge(graph, "DOCUMENTS", "doc1", "wi1")

    report = isolated_node_report(graph, ledger)
    assert report["Document"].isolated_count == 0
    assert report["Document"].isolated_uids == []


def test_isolated_document_ignores_non_live_documents_edge(graph, ledger):
    """A DOCUMENTS edge that is closed (invalid_at set) or corrected must
    not count as "connected" -- the node is still isolated."""
    _node(graph, "Document", "doc1")
    _node(graph, "WorkItem", "wi1")
    _edge(graph, "DOCUMENTS", "doc1", "wi1", invalid_at="2026-02-01T00:00:00Z")

    report = isolated_node_report(graph, ledger)
    assert report["Document"].isolated_count == 1
    assert report["Document"].isolated_uids == ["doc1"]


def test_isolated_decision_report(graph, ledger):
    _node(graph, "Decision", "dec1")
    _node(graph, "Decision", "dec2")
    _node(graph, "Term", "t1")
    _edge(graph, "APPLIES_TO", "dec1", "t1")

    report = isolated_node_report(graph, ledger)
    assert report["Decision"].total_count == 2
    assert report["Decision"].isolated_count == 1
    assert report["Decision"].isolated_uids == ["dec2"]


def test_isolated_commit_report(graph, ledger):
    _node(graph, "Commit", "c1")
    _node(graph, "Commit", "c2")
    _node(graph, "WorkItem", "wi1")
    _edge(graph, "IMPLEMENTS", "c1", "wi1")

    report = isolated_node_report(graph, ledger)
    assert report["Commit"].total_count == 2
    assert report["Commit"].isolated_count == 1
    assert report["Commit"].isolated_uids == ["c2"]


def test_degree_one_term_only_extracted_from_is_isolated(graph, ledger):
    _node(graph, "Term", "t1")
    _node(graph, "Document", "doc1")
    _edge(graph, "EXTRACTED_FROM", "t1", "doc1")

    report = isolated_node_report(graph, ledger)
    assert report["Term"].isolated_count == 1
    assert report["Term"].isolated_uids == ["t1"]


def test_degree_one_via_different_relation_is_not_isolated(graph, ledger):
    """The easiest rule to get subtly wrong: a Term with degree 1 via some
    OTHER relation (not EXTRACTED_FROM) must NOT be flagged by this rule."""
    _node(graph, "Term", "t1")
    _node(graph, "Decision", "dec1")
    _edge(graph, "APPLIES_TO", "dec1", "t1")  # only edge t1 has, wrong type

    report = isolated_node_report(graph, ledger)
    assert report["Term"].isolated_count == 0
    assert report["Term"].isolated_uids == []


def test_degree_two_with_extracted_from_is_not_isolated(graph, ledger):
    """A Term with EXTRACTED_FROM AND another live edge has degree 2 and
    must not be flagged, even though it has an EXTRACTED_FROM edge."""
    _node(graph, "Term", "t1")
    _node(graph, "Document", "doc1")
    _node(graph, "Decision", "dec1")
    _edge(graph, "EXTRACTED_FROM", "t1", "doc1")
    _edge(graph, "APPLIES_TO", "dec1", "t1")

    report = isolated_node_report(graph, ledger)
    assert report["Term"].isolated_count == 0


def test_degree_one_ignores_mentioned_in_as_provenance(graph, ledger):
    """MENTIONED_IN is provenance, not structure (graph/derived.py already
    excludes it the same way) -- a Term with EXTRACTED_FROM + MENTIONED_IN
    (its normal real-world shape, since graph/semantic_pass.py writes both
    for every extracted entity) is still isolated by this rule, because
    MENTIONED_IN doesn't count toward degree."""
    _node(graph, "Term", "t1")
    _node(graph, "Document", "doc1")
    _node(graph, "SourceRecord", "sr1")
    _edge(graph, "EXTRACTED_FROM", "t1", "doc1")
    _edge(graph, "MENTIONED_IN", "t1", "sr1")

    report = isolated_node_report(graph, ledger)
    assert report["Term"].isolated_count == 1
    assert report["Term"].isolated_uids == ["t1"]


def test_isolated_system_uses_same_degree_one_rule(graph, ledger):
    _node(graph, "System", "s1")
    _node(graph, "Document", "doc1")
    _edge(graph, "EXTRACTED_FROM", "s1", "doc1")

    report = isolated_node_report(graph, ledger)
    assert report["System"].isolated_count == 1
    assert report["System"].isolated_uids == ["s1"]


def test_isolated_node_report_records_to_ledger(graph, ledger):
    _node(graph, "Document", "doc1")

    report = isolated_node_report(graph, ledger, graph_name="hygiene_test")
    trend = ledger.hygiene_trend("Document", graph_name="hygiene_test")
    assert len(trend) == 1
    assert trend[0].isolated_count == report["Document"].isolated_count
    assert trend[0].total_count == report["Document"].total_count

    for label in ("Decision", "Commit", "Term", "System"):
        trend = ledger.hygiene_trend(label, graph_name="hygiene_test")
        assert len(trend) == 1


# --------------------------------------------------------------------- §6.3


def test_cardinality_violation_flags_two_live_edges(graph, ledger):
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _node(graph, "Person", "p2")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p2")

    violations = cardinality_violations(graph, DEFAULT_AXIOMS)
    hit = [v for v in violations if v.relation == "ASSIGNED_TO" and v.subject_uid == "wi1"]
    assert len(hit) == 1
    assert sorted(hit[0].object_uids) == ["p1", "p2"]
    assert hit[0].subject_label == "WorkItem"


def test_cardinality_one_live_edge_is_not_flagged(graph, ledger):
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")

    violations = cardinality_violations(graph, DEFAULT_AXIOMS)
    assert [v for v in violations if v.subject_uid == "wi1"] == []


def test_cardinality_non_live_second_edge_is_not_flagged(graph, ledger):
    """One of the two ASSIGNED_TO edges is closed (invalid_at set) -- only
    one live edge remains, so this must NOT be flagged."""
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _node(graph, "Person", "p2")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p2", invalid_at="2026-02-01T00:00:00Z")

    violations = cardinality_violations(graph, DEFAULT_AXIOMS)
    assert [v for v in violations if v.subject_uid == "wi1"] == []


def test_cardinality_corrected_second_edge_is_not_flagged(graph, ledger):
    """The other non-liveness path: assertion_status='corrected' on one
    edge -- must also not be flagged."""
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _node(graph, "Person", "p2")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p2", assertion_status="corrected")

    violations = cardinality_violations(graph, DEFAULT_AXIOMS)
    assert [v for v in violations if v.subject_uid == "wi1"] == []


def test_cardinality_pending_review_second_edge_is_not_flagged(graph, ledger):
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _node(graph, "Person", "p2")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p2", projection_status="pending_review")

    violations = cardinality_violations(graph, DEFAULT_AXIOMS)
    assert [v for v in violations if v.subject_uid == "wi1"] == []


def test_open_disputes_reports_both_endpoints(graph, ledger):
    _node(graph, "Decision", "decA", name="Decision A")
    _node(graph, "Decision", "decB", name="Decision B")
    _disputed_with(graph, "decA", "decB")

    disputes = open_disputes(graph)
    assert len(disputes) == 1
    d = disputes[0]
    assert d.a_uid == "decA" and d.a_name == "Decision A"
    assert d.b_uid == "decB" and d.b_name == "Decision B"
    assert d.first_seen_at == "2026-01-01T00:00:00Z"


def test_open_disputes_empty_when_none(graph, ledger):
    _node(graph, "Decision", "decA")
    assert open_disputes(graph) == []


# ------------------------------------------------------------- orchestration


def test_run_hygiene_checks_combines_both_and_records_audit_counts(graph, ledger):
    _node(graph, "Document", "doc1")
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Person", "p1")
    _node(graph, "Person", "p2")
    _edge(graph, "ASSIGNED_TO", "wi1", "p1")
    _edge(graph, "ASSIGNED_TO", "wi1", "p2")
    _node(graph, "Decision", "decA")
    _node(graph, "Decision", "decB")
    _disputed_with(graph, "decA", "decB")

    report = run_hygiene_checks(graph, ledger, graph_name="combo_test")
    assert isinstance(report, HygieneReport)
    assert report.run_id
    assert set(report.isolated.keys()) == {"Document", "Decision", "Commit", "Term", "System"}
    assert len(report.cardinality_violations) == 1
    assert len(report.open_disputes) == 1

    violation_trend = ledger.hygiene_trend("cardinality_violation", graph_name="combo_test")
    assert len(violation_trend) == 1
    assert violation_trend[0].isolated_count == 1
    assert violation_trend[0].run_id == report.run_id

    dispute_trend = ledger.hygiene_trend("open_dispute", graph_name="combo_test")
    assert len(dispute_trend) == 1
    assert dispute_trend[0].isolated_count == 1


def test_run_hygiene_checks_skips_audit_counts_when_opted_out(graph, ledger):
    report = run_hygiene_checks(graph, ledger, graph_name="no_audit", record_audit_counts=False)
    assert ledger.hygiene_trend("cardinality_violation", graph_name="no_audit") == []
    assert ledger.hygiene_trend("open_dispute", graph_name="no_audit") == []
    # §6.1's counts are still recorded regardless of the audit-count opt-out.
    assert ledger.hygiene_trend("Document", graph_name="no_audit")[0].run_id == report.run_id
