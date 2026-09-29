"""Tests for scripts.migrate_scoped_identity (25-plan.md §9.5 migration
notes -- scoped identity + valid_at_basis/assertion_status backfill).

Real FalkorDB, gated behind NEURON_INTEGRATION=1 -- same convention as
tests/test_duplicate_collector.py (unique scratch graph per test, seeded
so teardown never hits an empty key, cleaned up in a finally block) and
tests/test_graph_integration.py. No vector store involved: this migration
never touches Qdrant/Postgres (see the script's own docstring for why that's
a deliberately separate, later step).
"""

from __future__ import annotations

import os
import uuid

import pytest

from connectors.core.ledger import ConnectorLedger
from graph.storage import writer as w
from graph.storage.falkor_client import build_client
from graph.ingestion.semantic_pass import _normalize_identity, semantic_uid
from scripts import migrate_scoped_identity as m

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_{uuid.uuid4().hex}")
    g.query("CREATE (:_FixtureSeed {uid: '_fixture_seed'})")
    try:
        yield g
    finally:
        g.delete()


@pytest.fixture
def ledger(tmp_path):
    return ConnectorLedger(tmp_path / "ledger.sqlite3")


# --------------------------------------------------------------------------- seed helpers


def _project_record(graph, ledger, record_key: str, project_uid: str) -> None:
    """A record whose own kind IS the namespace-bearing node (`Project`) --
    the simplest namespace derivation path (`_namespace_uid_for_record`'s
    first branch returns `record_primary_uid` directly, no BELONGS_TO
    traversal needed), so tests can pin an exact expected namespace without
    needing to build a full WorkItem->Project graph."""
    w.upsert_source_records(graph, [{"record_key": record_key}])
    ledger.commit(record_key, content_hash="h", primary_node_uid=project_uid)


def _legacy_node(graph, label: str, name: str, extra_props: dict | None = None) -> str:
    """A node minted the OLD way: global `semantic_uid(label, name)` uid,
    no `namespace_uid` property at all -- exactly what `_write_extraction`
    produced before Phase 4 landed."""
    uid = semantic_uid(label, name)
    props = {"name": name, "search_text": name}
    props.update(extra_props or {})
    w.upsert_entities(graph, label, [{"uid": uid, "props": props}])
    return uid


def _mention(graph, label: str, uid: str, record_key: str) -> None:
    w.link_mentioned_in(graph, label, [{"uid": uid, "record_key": record_key}])


def _fact_edge(graph, rel_type, from_label, to_label, from_uid, to_uid, keys, **extra):
    row = {
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": list(keys),
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }
    row.update(extra)
    w.upsert_fact_edges(graph, rel_type, from_label, to_label, [row])


def _snapshot(graph) -> tuple[list, list]:
    nodes = graph.query(
        "MATCH (n) RETURN n.uid, labels(n), properties(n) ORDER BY n.uid"
    ).result_set
    edges = graph.query(
        "MATCH (a)-[r]->(b) RETURN a.uid, type(r), b.uid, properties(r) "
        "ORDER BY a.uid, type(r), b.uid"
    ).result_set
    return nodes, edges


# --------------------------------------------------------------------------- dry-run mapping


class TestFindSystemTermCandidates:
    def test_unambiguous_mapping_computed_correctly(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        old_uid = _legacy_node(graph, "System", "Redis")
        _mention(graph, "System", old_uid, "jira:c1:project:proj-1")

        mappings, stamped = m.find_system_term_candidates(graph, ledger)
        assert stamped == []
        assert len(mappings) == 1
        mapping = mappings[0]
        assert mapping.label == "System"
        assert mapping.old_uid == old_uid
        assert mapping.ambiguous is False
        assert mapping.namespaces == ["proj-1"]
        assert mapping.new_uid == w.make_uid("System", "proj-1", "redis")
        assert mapping.new_uid != old_uid

    def test_ambiguous_when_records_disagree_on_namespace(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        _project_record(graph, ledger, "jira:c1:project:proj-2", "proj-2")
        old_uid = _legacy_node(graph, "Term", "rate limiter")
        _mention(graph, "Term", old_uid, "jira:c1:project:proj-1")
        _mention(graph, "Term", old_uid, "jira:c1:project:proj-2")

        mappings, _stamped = m.find_system_term_candidates(graph, ledger)
        assert len(mappings) == 1
        mapping = mappings[0]
        assert mapping.ambiguous is True
        assert mapping.ambiguous_reason == "conflicting_namespaces"
        assert mapping.new_uid is None
        assert set(mapping.namespaces) == {"proj-1", "proj-2"}

    def test_ambiguous_when_no_source_records(self, graph, ledger):
        old_uid = _legacy_node(graph, "System", "Orphan System")
        mappings, _stamped = m.find_system_term_candidates(graph, ledger)
        assert len(mappings) == 1
        mapping = mappings[0]
        assert mapping.old_uid == old_uid
        assert mapping.ambiguous is True
        assert mapping.ambiguous_reason == "no_source_records"
        assert mapping.new_uid is None

    def test_already_scoped_node_is_not_a_candidate(self, graph, ledger):
        # A real post-Phase-4 write always stamps namespace_uid -- such a
        # node must never show up as a migration candidate.
        w.upsert_entities(graph, "System", [
            {"uid": "sys-real", "props": {"name": "Kafka", "namespace_uid": "proj-9"}},
        ])
        mappings, _stamped = m.find_system_term_candidates(graph, ledger)
        assert mappings == []


class TestFindDecisionCandidates:
    def test_single_record_decision_is_unambiguous(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        old_uid = _legacy_node(graph, "Decision", "use redis", {"statement": "Use Redis for caching"})
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-1")

        mappings = m.find_decision_candidates(graph)
        assert len(mappings) == 1
        mapping = mappings[0]
        assert mapping.old_uid == old_uid
        assert mapping.ambiguous is False
        assert mapping.new_uid == w.make_uid(
            "Decision", "jira:c1:project:proj-1", _normalize_identity("Use Redis for caching"),
        )

    def test_cross_record_decision_is_always_ambiguous(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        _project_record(graph, ledger, "jira:c1:project:proj-2", "proj-1")  # same namespace!
        old_uid = _legacy_node(graph, "Decision", "use redis", {"statement": "Use Redis for caching"})
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-1")
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-2")

        mappings = m.find_decision_candidates(graph)
        assert len(mappings) == 1
        mapping = mappings[0]
        # Even though both records resolve to the SAME namespace, a Decision
        # spanning >1 record can never be one node under the new (record,
        # statement)-scoped identity -- always ambiguous, never a namespace
        # disagreement question the way System/Term is.
        assert mapping.ambiguous is True
        assert mapping.ambiguous_reason == "cross_record_decision_merge"
        assert mapping.new_uid is None

    def test_new_scheme_decision_is_not_a_candidate(self, graph, ledger):
        new_uid = w.make_uid("Decision", "jira:c1:project:proj-1:1", _normalize_identity("use redis"))
        w.upsert_entities(graph, "Decision", [
            {"uid": new_uid, "props": {"name": "use redis", "namespace_uid": "proj-1"}},
        ])
        assert m.find_decision_candidates(graph) == []


# --------------------------------------------------------------------------- apply mode


class TestApplyMigratesUnambiguousSystemTerm:
    def test_new_node_created_edges_redirected_alias_and_trace_recorded(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        old_uid = _legacy_node(graph, "System", "Redis")
        _mention(graph, "System", old_uid, "jira:c1:project:proj-1")

        term_uid = _legacy_node(graph, "Term", "rate limiter")
        _fact_edge(
            graph, "APPLIES_TO", "Term", "System", term_uid, old_uid,
            ["jira:c1:project:proj-1"],
        )

        report = m.run_migration(graph, ledger, apply=True)

        expected_new_uid = w.make_uid("System", "proj-1", "redis")
        result = next(r for r in report["system_term"] if r["old_uid"] == old_uid)
        assert result["status"] == "migrated"
        assert result["new_uid"] == expected_new_uid
        assert result["created_new_node"] is True

        # New node exists with the right label/props, namespace_uid stamped.
        rows = graph.query(
            "MATCH (n:System {uid: $uid}) RETURN n.name, n.namespace_uid",
            params={"uid": expected_new_uid},
        ).result_set
        assert rows == [["Redis", "proj-1"]]

        # Old node preserved, untouched as a node.
        old_rows = graph.query(
            "MATCH (n:System {uid: $uid}) RETURN n.name", params={"uid": old_uid},
        ).result_set
        assert old_rows == [["Redis"]]

        # Live edge redirected onto the new node.
        redirected = graph.query(
            "MATCH (t {uid: $t})-[r:APPLIES_TO]->(s {uid: $s}) RETURN r.invalid_at",
            params={"t": term_uid, "s": expected_new_uid},
        ).result_set
        assert redirected and redirected[0][0] is None

        # Old edge invalidated, not deleted.
        old_edge = graph.query(
            "MATCH (t {uid: $t})-[r:APPLIES_TO]->(s {uid: $s}) RETURN r.invalid_at",
            params={"t": term_uid, "s": old_uid},
        ).result_set
        assert old_edge and old_edge[0][0] is not None

        # Old node has no remaining LIVE outgoing/incoming fact edges.
        still_live = graph.query(
            "MATCH (n {uid: $uid})-[r]-() WHERE type(r) <> 'MENTIONED_IN' "
            "AND r.invalid_at IS NULL RETURN count(r)",
            params={"uid": old_uid},
        ).result_set
        assert still_live[0][0] == 0

        # Alias resolves the old normalized name to the new uid.
        assert ledger.lookup_alias("System", "proj-1", "redis") == expected_new_uid

        # Merge trace recorded, queryable via merged_into.
        assert ledger.merged_into(old_uid) == expected_new_uid

    def test_migration_merges_onto_an_already_existing_scoped_node(self, graph, ledger):
        """A fresh Phase-4 extraction may have already created the "real"
        scoped node independently before this migration ever runs. The
        existing node must survive as-is -- never have its properties
        clobbered by the old node's."""
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        old_uid = _legacy_node(graph, "System", "Redis", {"stale_field": "old"})
        _mention(graph, "System", old_uid, "jira:c1:project:proj-1")

        new_uid = w.make_uid("System", "proj-1", "redis")
        w.upsert_entities(graph, "System", [
            {"uid": new_uid, "props": {"name": "Redis", "namespace_uid": "proj-1", "fresh_field": "new"}},
        ])

        m.run_migration(graph, ledger, apply=True)

        rows = graph.query(
            "MATCH (n:System {uid: $uid}) RETURN properties(n)", params={"uid": new_uid},
        ).result_set
        props = dict(rows[0][0])
        assert props.get("fresh_field") == "new"
        assert "stale_field" not in props  # never clobbered by the old node


class TestApplyMigratesUnambiguousDecision:
    def test_single_record_decision_migrates(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        old_uid = _legacy_node(graph, "Decision", "use redis", {"statement": "Use Redis for caching"})
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-1")

        report = m.run_migration(graph, ledger, apply=True)
        expected_new_uid = w.make_uid(
            "Decision", "jira:c1:project:proj-1", _normalize_identity("Use Redis for caching"),
        )
        result = next(r for r in report["decisions"] if r["old_uid"] == old_uid)
        assert result["status"] == "migrated"
        assert result["new_uid"] == expected_new_uid
        assert ledger.merged_into(old_uid) == expected_new_uid
        # Decision aliases are not namespace-scoped (§4.0) -- looked up with None.
        assert ledger.lookup_alias("Decision", None, "use redis") == expected_new_uid


class TestAmbiguousGoesToReviewNotApplied:
    def test_ambiguous_system_term_creates_review_and_does_not_migrate(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        _project_record(graph, ledger, "jira:c1:project:proj-2", "proj-2")
        old_uid = _legacy_node(graph, "Term", "rate limiter")
        _mention(graph, "Term", old_uid, "jira:c1:project:proj-1")
        _mention(graph, "Term", old_uid, "jira:c1:project:proj-2")

        before_nodes, before_edges = _snapshot(graph)
        report = m.run_migration(graph, ledger, apply=True)
        after_nodes, after_edges = _snapshot(graph)

        result = next(r for r in report["system_term"] if r["old_uid"] == old_uid)
        assert result["review_id"] is not None

        reviews = ledger.list_reviews(type="scoped_identity_migration")
        assert len(reviews) == 1
        assert reviews[0].payload["reason"] == "conflicting_namespaces"
        assert reviews[0].payload["label"] == "Term"
        assert reviews[0].state == "pending"

        # No graph mutation for an ambiguous mapping -- same node count/edges,
        # modulo the review bookkeeping which lives entirely in the ledger.
        assert after_nodes == before_nodes
        assert after_edges == before_edges

    def test_cross_record_decision_always_goes_to_review(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        _project_record(graph, ledger, "jira:c1:project:proj-2", "proj-1")
        old_uid = _legacy_node(graph, "Decision", "use redis", {"statement": "Use Redis for caching"})
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-1")
        _mention(graph, "Decision", old_uid, "jira:c1:project:proj-2")

        report = m.run_migration(graph, ledger, apply=True)
        result = next(r for r in report["decisions"] if r["old_uid"] == old_uid)
        assert result["review_id"] is not None
        assert result.get("status") != "migrated"  # never auto-applied

        reviews = ledger.list_reviews(type="scoped_identity_migration")
        assert len(reviews) == 1
        assert reviews[0].payload["label"] == "Decision"
        assert reviews[0].payload["reason"] == "cross_record_decision_merge"

        # Never migrated: no alias, no merge trace, old node untouched.
        assert ledger.lookup_alias("Decision", None, "use redis") is None
        assert ledger.merged_into(old_uid) is None


# --------------------------------------------------------------------------- dry-run safety


class TestDryRunMakesZeroWrites:
    def test_dry_run_touches_neither_graph_nor_ledger(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        _project_record(graph, ledger, "jira:c1:project:proj-2", "proj-2")

        unambiguous_uid = _legacy_node(graph, "System", "Redis")
        _mention(graph, "System", unambiguous_uid, "jira:c1:project:proj-1")

        ambiguous_uid = _legacy_node(graph, "Term", "rate limiter")
        _mention(graph, "Term", ambiguous_uid, "jira:c1:project:proj-1")
        _mention(graph, "Term", ambiguous_uid, "jira:c1:project:proj-2")

        decision_uid = _legacy_node(graph, "Decision", "use redis", {"statement": "Use Redis"})
        _mention(graph, "Decision", decision_uid, "jira:c1:project:proj-1")
        _mention(graph, "Decision", decision_uid, "jira:c1:project:proj-2")

        before_nodes, before_edges = _snapshot(graph)
        report = m.run_migration(graph, ledger, apply=False)
        after_nodes, after_edges = _snapshot(graph)

        assert after_nodes == before_nodes
        assert after_edges == before_edges
        assert report["mode"] == "dry-run"
        assert report["summary"]["system_term_unambiguous"] == 1
        assert report["summary"]["system_term_ambiguous"] == 1
        assert report["summary"]["decision_ambiguous_or_cross_record"] == 1

        # No ledger side effects at all in dry-run mode.
        assert ledger.list_reviews(type="scoped_identity_migration") == []
        assert ledger.merged_into(unambiguous_uid) is None
        assert ledger.lookup_alias("System", "proj-1", "redis") is None


# --------------------------------------------------------------------------- temporal backfill


class TestBackfillTemporalFields:
    def _raw_edge(self, graph, from_uid, to_uid, *, extraction_method, extra=""):
        graph.query(
            f"""
            MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}})
            CREATE (a)-[r:APPLIES_TO {{
                source_record_keys: [], extraction_method: $method,
                confidence: 0.9 {extra}
            }}]->(b)
            """,
            params={"from_uid": from_uid, "to_uid": to_uid, "method": extraction_method},
        )

    def test_backfills_missing_fields_by_extraction_method(self, graph):
        w.upsert_entities(graph, "Term", [{"uid": "t1", "props": {"name": "A"}}])
        w.upsert_entities(graph, "System", [
            {"uid": "s1", "props": {"name": "B"}}, {"uid": "s2", "props": {"name": "C"}},
        ])
        self._raw_edge(graph, "t1", "s1", extraction_method="llm")
        self._raw_edge(graph, "t1", "s2", extraction_method="deterministic")

        dry_report = m.backfill_temporal_fields(graph, apply=False)
        assert dry_report["edges_missing_fields"] == 2
        assert dry_report["by_extraction_method"] == {"llm": 1, "deterministic": 1}
        assert dry_report["applied"] is False

        # Dry run wrote nothing.
        rows = graph.query(
            "MATCH ()-[r:APPLIES_TO]->() RETURN r.valid_at_basis, r.assertion_status"
        ).result_set
        assert all(vab is None and astat is None for vab, astat in rows)

        applied_report = m.backfill_temporal_fields(graph, apply=True)
        assert applied_report["applied"] is True

        rows = graph.query(
            "MATCH ()-[r:APPLIES_TO]->() RETURN r.valid_at_basis, r.assertion_status "
            "ORDER BY r.valid_at_basis"
        ).result_set
        assert len(rows) == 2
        for vab, astat in rows:
            assert vab == "record_time"
            assert astat == "live"

    def test_does_not_overwrite_existing_values(self, graph):
        w.upsert_entities(graph, "Term", [{"uid": "t1", "props": {"name": "A"}}])
        w.upsert_entities(graph, "System", [{"uid": "s1", "props": {"name": "B"}}])
        self._raw_edge(
            graph, "t1", "s1", extraction_method="llm",
            extra=", assertion_status: 'corrected', valid_at_basis: 'stated'",
        )

        report = m.backfill_temporal_fields(graph, apply=True)
        assert report["edges_missing_fields"] == 0
        assert report["applied"] is False

        rows = graph.query(
            "MATCH ()-[r:APPLIES_TO]->() RETURN r.valid_at_basis, r.assertion_status"
        ).result_set
        assert rows == [["stated", "corrected"]]

    def test_mentioned_in_edges_are_excluded(self, graph, ledger):
        _project_record(graph, ledger, "jira:c1:project:proj-1", "proj-1")
        uid = _legacy_node(graph, "System", "Redis")
        _mention(graph, "System", uid, "jira:c1:project:proj-1")

        report = m.backfill_temporal_fields(graph, apply=True)
        assert report["edges_missing_fields"] == 0
