"""Tests for graph.duplicate_collector (25-plan.md Phase 6 §6.4 -- multi-
signal duplicate collector for Decision/Term).

Real FalkorDB, real Qdrant/Postgres, gated behind NEURON_INTEGRATION=1 --
same convention as tests/test_graph_integration.py (real FalkorDB, unique
scratch graph per test, cleaned up in a finally block) and
tests/test_vector_store.py (real Qdrant/Postgres, unique scratch collection
per test, both backends parametrized). Vector-store setup goes through
`vector_store.upsert_vectors` directly (the module's own public write path,
with an explicit uid so a graph node and its vector-store point always
share identity) rather than reaching into either backend's internals.
"""

from __future__ import annotations

import math
import os
import uuid

import pytest
from qdrant_client import QdrantClient

from connectors.core.ledger import ConnectorLedger
from graph import duplicate_collector as dc
from graph import vector_store
from graph import writer as w
from graph.falkor_client import build_client
from graph.semantic_pass import polarity_conflict as sp_polarity_conflict
from storage.postgres import PostgresConfigurationError, PostgresStore, PostgresVectorClient, database_url

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB/Qdrant/Postgres",
)

_DIM = vector_store.EMBEDDING_DIMENSION


def _vec(similarity: float) -> list[float]:
    """A vector at cosine `similarity` to the all-first-axis base vector --
    same trick tests/test_vector_store.py uses, so a test can target an
    exact similarity score without a real embedding model."""
    theta = math.acos(max(-1.0, min(1.0, similarity)))
    return [math.cos(theta), math.sin(theta)] + [0.0] * (_DIM - 2)


# --------------------------------------------------------------------- fixtures


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_{uuid.uuid4().hex}")
    # Some tests raise before ever writing to the graph (e.g. a ValueError
    # on bad input); FalkorDB has no key to `delete()` in that case ("Invalid
    # graph operation on empty key"). A harmless seed write, same fix
    # tests/test_graph_integration.py's own `graph` fixture already uses,
    # guarantees the key exists for teardown regardless of what the test body does.
    g.query("CREATE (:_FixtureSeed {uid: '_fixture_seed'})")
    try:
        yield g
    finally:
        g.delete()


@pytest.fixture
def ledger(tmp_path):
    return ConnectorLedger(tmp_path / "ledger.sqlite3")


@pytest.fixture(params=["qdrant", "postgres"])
def vector_client(request):
    """(client, collection) for both vector backends `vector_store.py`
    supports -- every candidate-generation test runs against both."""
    if request.param == "qdrant":
        client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
        collection = f"test_dup_collector_{uuid.uuid4().hex}"
        vector_store.ensure_collection(client, collection)
        try:
            yield client, collection
        finally:
            client.delete_collection(collection_name=collection)
    else:
        if not database_url():
            pytest.skip("DATABASE_URL not configured for Postgres integration test")
        try:
            store = PostgresStore()
            store.bootstrap()
        except PostgresConfigurationError as exc:
            pytest.skip(str(exc))
        collection = f"test_dup_collector_{uuid.uuid4().hex}"
        handle = PostgresVectorClient(store)
        try:
            yield handle, collection
        finally:
            handle.delete_collection(collection)


def _term(graph, uid: str, name: str, text: str | None = None) -> str:
    w.upsert_entities(graph, "Term", [{"uid": uid, "props": {"name": name, "search_text": text or name}}])
    return uid


def _fact_edge(graph, rel_type, from_label, to_label, from_uid, to_uid, keys, confidence=0.9):
    w.upsert_fact_edges(graph, rel_type, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": list(keys),
        "evidence": "e", "extraction_method": "llm", "confidence": confidence,
    }])


# ------------------------------------------------------------------- signals


class TestLexicalOverlapScore:
    def test_partial_overlap_real_tokens(self):
        score = dc.lexical_overlap_score("Redis rate limiter", "rate limiter for Redis")
        # {redis, rate, limiter} vs {rate, limiter, for, redis}: intersection 3, union 4.
        assert math.isclose(score, 0.75)

    def test_identical_names(self):
        assert dc.lexical_overlap_score("Sliding Window", "sliding window") == 1.0

    def test_disjoint_names(self):
        assert dc.lexical_overlap_score("Redis", "Kafka") == 0.0

    def test_normalization_ignores_punctuation_and_case(self):
        # Same normalize-then-split convention as graph.semantic_pass._normalize_identity.
        assert dc.lexical_overlap_score("blast-radius!", "Blast Radius") == 1.0


class TestSharedSourceRecordsScore:
    def test_both_empty_is_zero_not_one(self):
        assert dc.shared_source_records_score([], []) == 0.0

    def test_identical_lists(self):
        assert dc.shared_source_records_score(["r1", "r2"], ["r2", "r1"]) == 1.0

    def test_partial_overlap(self):
        assert math.isclose(dc.shared_source_records_score(["r1", "r2"], ["r2", "r3"]), 1 / 3)


class TestPolarityVeto:
    def test_reuses_semantic_pass_polarity_conflict_directly(self):
        """Not a reimplementation: the imported name is the exact same
        function object semantic_pass itself uses."""
        assert dc.polarity_conflict is sp_polarity_conflict

    def test_true_on_negation_mismatch(self):
        assert dc.polarity_veto(
            "use Redis for rate limiting", "do not use Redis for rate limiting",
        ) is True

    def test_false_when_neither_or_both_negated(self):
        assert dc.polarity_veto(
            "use Redis for rate limiting", "use Redis for the rate limiter",
        ) is False


class TestSharedTargetsScore:
    def test_against_real_graph_edges(self, graph):
        uid_a = _term(graph, "term-a", "A")
        uid_b = _term(graph, "term-b", "B")
        w.upsert_entities(graph, "System", [
            {"uid": "sys-1", "props": {"name": "Sys1"}},
            {"uid": "sys-2", "props": {"name": "Sys2"}},
            {"uid": "sys-3", "props": {"name": "Sys3"}},
        ])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_a, "sys-1", ["r1"])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_a, "sys-2", ["r1"])
        _fact_edge(graph, "DEFINES", "Term", "System", uid_b, "sys-2", ["r1"])
        _fact_edge(graph, "DEFINES", "Term", "System", uid_b, "sys-3", ["r1"])

        # A -> {sys-1, sys-2}; B -> {sys-2, sys-3}; intersection 1, union 3.
        assert math.isclose(dc.shared_targets_score(graph, uid_a, uid_b), 1 / 3)

    def test_excludes_invalidated_edges(self, graph):
        uid_a = _term(graph, "term-a", "A")
        uid_b = _term(graph, "term-b", "B")
        w.upsert_entities(graph, "System", [{"uid": "sys-1", "props": {"name": "Sys1"}}])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_a, "sys-1", ["r1"])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_b, "sys-1", ["r1"])
        assert dc.shared_targets_score(graph, uid_a, uid_b) == 1.0

        w.invalidate_edges_by_uid_pairs(
            graph, [{"from_uid": uid_b, "to_uid": "sys-1", "rel_type": "APPLIES_TO"}],
        )
        assert dc.shared_targets_score(graph, uid_a, uid_b) == 0.0

    def test_ignores_relations_outside_the_default_set(self, graph):
        uid_a = _term(graph, "term-a", "A")
        uid_b = _term(graph, "term-b", "B")
        w.upsert_entities(graph, "Term", [{"uid": "term-c", "props": {"name": "C"}}])
        _fact_edge(graph, "SUPERSEDES", "Term", "Term", uid_a, "term-c", ["r1"])
        _fact_edge(graph, "SUPERSEDES", "Term", "Term", uid_b, "term-c", ["r1"])
        # SUPERSEDES is not in the default (APPLIES_TO, DEFINES) relation set.
        assert dc.shared_targets_score(graph, uid_a, uid_b) == 0.0


# ----------------------------------------------------------------- duplicate_score


class TestDuplicateScore:
    def test_veto_forces_none_regardless_of_signals(self):
        assert dc.duplicate_score(1.0, 1.0, 1.0, 1.0, veto=True) is None
        assert dc.duplicate_score(0.0, 0.0, 0.0, 0.0, veto=True) is None

    def test_weighted_sum_arithmetic(self):
        score = dc.duplicate_score(0.9, 0.5, 0.5, 0.5, veto=False)
        assert math.isclose(score, 0.4 * 0.9 + 0.2 * 0.5 + 0.2 * 0.5 + 0.2 * 0.5)
        assert math.isclose(score, 0.66)

    def test_all_ones_sums_to_one(self):
        assert dc.duplicate_score(1.0, 1.0, 1.0, 1.0, veto=False) == 1.0

    def test_boundary_at_point_six(self):
        # vector=1.0 alone contributes exactly 0.4; add lexical=1.0 (0.2) and
        # shared_targets=1.0 (0.2) for exactly 0.4+0.2+0.2 = 0.8 -- too high.
        # Tune to land exactly on 0.6: vector=1.0 (0.4) + source_records=1.0 (0.2)
        # + lexical=0.0 + targets=0.0 = 0.6 exactly.
        at_boundary = dc.duplicate_score(1.0, 0.0, 0.0, 1.0, veto=False)
        assert math.isclose(at_boundary, 0.6)
        assert at_boundary >= 0.6

        just_below = dc.duplicate_score(0.99, 0.0, 0.0, 1.0, veto=False)
        assert just_below < 0.6


# ------------------------------------------------------------- candidate generation


class TestFindDuplicateCandidates:
    def test_rejects_unsupported_label(self, graph, vector_client):
        client, collection = vector_client
        with pytest.raises(ValueError):
            dc.find_duplicate_candidates(graph, client, "System", collection=collection)

    def test_no_duplicate_pairs_and_filters_by_score_threshold(self, graph, vector_client):
        # Vector-store point ids must be real UUIDs (Qdrant/pgvector column
        # constraint), and a graph node's uid must match its vector-store
        # point id for `_node_embedding` to find it -- so these candidate-
        # generation tests use real uuid4 strings, unlike the pure-graph
        # tests above which can use readable literal uids freely.
        client, collection = vector_client
        uid_a = _term(graph, str(uuid.uuid4()), "Redis rate limiter")
        uid_b = _term(graph, str(uuid.uuid4()), "Redis rate limiter")  # identical name -> lexical=1.0
        uid_c = _term(graph, str(uuid.uuid4()), "totally unrelated concept")

        w.upsert_source_records(graph, [{"record_key": "sr:shared"}])
        w.link_mentioned_in(graph, "Term", [
            {"uid": uid_a, "record_key": "sr:shared"}, {"uid": uid_b, "record_key": "sr:shared"},
        ])

        vector_store.upsert_vectors(client, [
            {"uid": uid_a, "label": "Term", "embedding": _vec(1.0)},
            {"uid": uid_b, "label": "Term", "embedding": _vec(0.9)},
            # Above similarity_threshold (0.8) but with nothing else in common
            # -- must be filtered by score_threshold, not proposed.
            {"uid": uid_c, "label": "Term", "embedding": _vec(0.85)},
        ], collection=collection)

        candidates = dc.find_duplicate_candidates(graph, client, "Term", collection=collection)

        pairs = {(c.uid_a, c.uid_b) for c in candidates}
        assert pairs == {tuple(sorted((uid_a, uid_b)))}
        assert len(candidates) == 1
        only = candidates[0]
        assert only.uid_a < only.uid_b
        assert only.score >= dc.DEFAULT_SCORE_THRESHOLD
        assert only.lexical_overlap == 1.0
        assert only.shared_source_records == 1.0

    def test_excludes_polarity_vetoed_pairs(self, graph, vector_client):
        client, collection = vector_client
        uid_a = _term(
            graph, str(uuid.uuid4()), "use Redis for rate limiting",
            text="use Redis for rate limiting",
        )
        uid_b = _term(
            graph, str(uuid.uuid4()), "do not use Redis for rate limiting",
            text="do not use Redis for rate limiting",
        )
        vector_store.upsert_vectors(client, [
            {"uid": uid_a, "label": "Term", "embedding": _vec(1.0)},
            {"uid": uid_b, "label": "Term", "embedding": _vec(0.95)},
        ], collection=collection)

        candidates = dc.find_duplicate_candidates(graph, client, "Term", collection=collection)
        assert candidates == []

    def test_respects_similarity_threshold_and_search_limit(self, graph, vector_client, monkeypatch):
        client, collection = vector_client
        uid_a = _term(graph, str(uuid.uuid4()), "A")
        vector_store.upsert_vectors(
            client, [{"uid": uid_a, "label": "Term", "embedding": _vec(1.0)}], collection=collection,
        )

        real_search_above = vector_store.search_above
        calls: list[tuple[tuple, dict]] = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_search_above(*args, **kwargs)

        monkeypatch.setattr(dc.vector_store, "search_above", spy)

        dc.find_duplicate_candidates(
            graph, client, "Term", similarity_threshold=0.77, search_limit=5, collection=collection,
        )

        assert calls, "search_above should have been called at least once"
        args, kwargs = calls[0]
        assert args[3] == 0.77  # min_similarity, 4th positional arg
        assert kwargs["limit"] == 5
        assert kwargs["collection"] == collection


# --------------------------------------------------------------- review proposals


class TestProposeDuplicateReviews:
    def test_identity_dedup_prevents_a_second_proposal_after_rejection(self, ledger):
        candidate = dc.DuplicateCandidate(
            label="Term", uid_a="a1", uid_b="b1", vector_similarity=0.9,
            lexical_overlap=1.0, shared_targets=0.0, shared_source_records=1.0, score=0.76,
        )
        first_ids = dc.propose_duplicate_reviews(ledger, [candidate])
        assert first_ids[0] is not None
        review = ledger.get_review(first_ids[0])
        assert review.type == "duplicate_pair"
        assert review.payload["uid_a"] == "a1"
        assert review.payload["uid_b"] == "b1"
        assert review.payload["score"] == 0.76
        assert review.identity == "duplicate_pair:Term:a1:b1"

        ledger.reject_review(first_ids[0], decided_by="tester")

        second_ids = dc.propose_duplicate_reviews(ledger, [candidate])
        assert second_ids[0] is None
        assert len(ledger.list_reviews(type="duplicate_pair", state=None, limit=200)) == 1

    def test_two_distinct_pairs_get_two_reviews(self, ledger):
        candidates = [
            dc.DuplicateCandidate("Term", "a1", "b1", 0.9, 1.0, 0.0, 1.0, 0.76),
            dc.DuplicateCandidate("Term", "a1", "c1", 0.85, 0.5, 0.0, 0.0, 0.44),
        ]
        ids = dc.propose_duplicate_reviews(ledger, candidates)
        assert all(i is not None for i in ids)
        assert len(set(ids)) == 2


# ----------------------------------------------------------------- merge execution


class TestChooseSurvivor:
    def test_prefers_more_reinforced_node(self, graph):
        uid_a = _term(graph, "term-a", "Redis rate limiter")
        uid_b = _term(graph, "term-b", "Redis limiter")
        w.upsert_entities(graph, "System", [{"uid": "sys-1", "props": {"name": "Redis"}}])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_a, "sys-1", ["r1", "r2", "r5"])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_b, "sys-1", ["r3"])

        survivor, absorbed = dc._choose_survivor(graph, uid_a, uid_b)
        assert (survivor, absorbed) == (uid_a, uid_b)
        # Order of arguments must not matter.
        survivor2, absorbed2 = dc._choose_survivor(graph, uid_b, uid_a)
        assert (survivor2, absorbed2) == (uid_a, uid_b)

    def test_falls_back_to_oldest_when_reinforcement_tied(self, graph):
        uid_a = _term(graph, "term-a", "Redis rate limiter")
        uid_b = _term(graph, "term-b", "Redis limiter")
        # No fact edges at all -- reinforcement 0 for both, a genuine tie.
        graph.query(
            "MATCH (n {uid: $uid}) SET n.first_seen_at = $ts",
            params={"uid": uid_a, "ts": "2026-01-01T00:00:00+00:00"},
        )
        graph.query(
            "MATCH (n {uid: $uid}) SET n.first_seen_at = $ts",
            params={"uid": uid_b, "ts": "2026-02-01T00:00:00+00:00"},
        )

        survivor, absorbed = dc._choose_survivor(graph, uid_a, uid_b)
        assert (survivor, absorbed) == (uid_a, uid_b)  # A is older
        survivor2, absorbed2 = dc._choose_survivor(graph, uid_b, uid_a)
        assert (survivor2, absorbed2) == (uid_a, uid_b)


class TestApplyApprovedDuplicateMerge:
    def _approved_review(self, ledger, label, uid_a, uid_b, score=0.8):
        sorted_pair = tuple(sorted((uid_a, uid_b)))
        review_id = ledger.create_review(
            "duplicate_pair",
            {"label": label, "uid_a": sorted_pair[0], "uid_b": sorted_pair[1], "score": score, "signals": {}},
            identity=f"duplicate_pair:{label}:{sorted_pair[0]}:{sorted_pair[1]}",
        )
        ledger.approve_review(review_id, decided_by="tester")
        return review_id

    def test_redirects_edges_unions_source_records_and_records_trace(self, graph, ledger):
        uid_a = _term(graph, "term-a", "Redis rate limiter")  # more reinforced -> survivor
        uid_b = _term(graph, "term-b", "Redis limiter")
        w.upsert_entities(graph, "System", [{"uid": "sys-1", "props": {"name": "Redis"}}])
        w.upsert_entities(graph, "Decision", [{"uid": "dec-1", "props": {"name": "use redis"}}])

        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_a, "sys-1", ["r1", "r2", "r5"])
        _fact_edge(graph, "APPLIES_TO", "Term", "System", uid_b, "sys-1", ["r3"])
        _fact_edge(graph, "APPLIES_TO", "Decision", "Term", "dec-1", uid_b, ["r4"])

        w.upsert_source_records(graph, [{"record_key": "sr:rec1"}, {"record_key": "sr:rec2"}])
        w.link_mentioned_in(graph, "Term", [{"uid": uid_b, "record_key": "sr:rec1"}])
        w.link_mentioned_in(graph, "Term", [{"uid": uid_a, "record_key": "sr:rec2"}])

        review_id = self._approved_review(ledger, "Term", uid_a, uid_b)

        result = dc.apply_approved_duplicate_merge(graph, ledger, review_id)

        assert result["review_id"] == review_id
        assert result["label"] == "Term"
        assert result["survivor_uid"] == uid_a
        assert result["absorbed_uid"] == uid_b

        # Outgoing edge redirected: survivor -> sys-1 now carries the union
        # of both edges' source_record_keys, and is live.
        redirected_out = graph.query(
            "MATCH (a {uid: $a})-[r:APPLIES_TO]->(s {uid: 'sys-1'}) RETURN r.invalid_at, r.source_record_keys",
            params={"a": uid_a},
        ).result_set
        assert redirected_out and redirected_out[0][0] is None
        assert set(redirected_out[0][1]) == {"r1", "r2", "r5", "r3"}

        # The old absorbed-node edge is invalidated, not deleted.
        old_out = graph.query(
            "MATCH (b {uid: $b})-[r:APPLIES_TO]->(s {uid: 'sys-1'}) RETURN r.invalid_at",
            params={"b": uid_b},
        ).result_set
        assert old_out and old_out[0][0] is not None

        # Incoming edge redirected: dec-1 -> survivor now live.
        redirected_in = graph.query(
            "MATCH (d {uid: 'dec-1'})-[r:APPLIES_TO]->(a {uid: $a}) RETURN r.invalid_at, r.source_record_keys",
            params={"a": uid_a},
        ).result_set
        assert redirected_in and redirected_in[0][0] is None
        assert redirected_in[0][1] == ["r4"]

        old_in = graph.query(
            "MATCH (d {uid: 'dec-1'})-[r:APPLIES_TO]->(b {uid: $b}) RETURN r.invalid_at",
            params={"b": uid_b},
        ).result_set
        assert old_in and old_in[0][0] is not None

        # MENTIONED_IN absorbed onto the survivor (union), absorbed node's own
        # mention left untouched (non-destructive).
        survivor_records = {
            row[0] for row in graph.query(
                "MATCH (a {uid: $a})-[:MENTIONED_IN]->(sr:SourceRecord) RETURN sr.record_key",
                params={"a": uid_a},
            ).result_set
        }
        assert survivor_records == {"sr:rec1", "sr:rec2"}
        absorbed_records = {
            row[0] for row in graph.query(
                "MATCH (b {uid: $b})-[:MENTIONED_IN]->(sr:SourceRecord) RETURN sr.record_key",
                params={"b": uid_b},
            ).result_set
        }
        assert absorbed_records == {"sr:rec1"}

        # merge_trace recorded and merged_into queryable.
        assert ledger.merged_into(uid_b) == uid_a
        assert ledger.merged_into(uid_a) is None

    def test_raises_on_missing_review(self, graph, ledger):
        with pytest.raises(ValueError):
            dc.apply_approved_duplicate_merge(graph, ledger, 999999)

    def test_raises_on_wrong_review_type(self, graph, ledger):
        review_id = ledger.create_review("fact_update", {"x": 1}, identity="whatever")
        ledger.approve_review(review_id, decided_by="tester")
        with pytest.raises(ValueError):
            dc.apply_approved_duplicate_merge(graph, ledger, review_id)

    def test_raises_when_not_yet_approved(self, graph, ledger):
        uid_a = _term(graph, "term-a", "A")
        uid_b = _term(graph, "term-b", "B")
        review_id = ledger.create_review(
            "duplicate_pair",
            {"label": "Term", "uid_a": uid_a, "uid_b": uid_b, "score": 0.8, "signals": {}},
            identity=f"duplicate_pair:Term:{uid_a}:{uid_b}",
        )
        # still pending -- never approved
        with pytest.raises(ValueError):
            dc.apply_approved_duplicate_merge(graph, ledger, review_id)


class TestTransitivityNotApplied:
    def test_a_b_merge_does_not_touch_a_separate_pending_b_c_review(self, graph, ledger):
        uid_a = _term(graph, "term-a", "Redis limiter")
        uid_b = _term(graph, "term-b", "Redis rate limiter")
        uid_c = _term(graph, "term-c", "Unrelated term")

        review_ab_id = ledger.create_review(
            "duplicate_pair",
            {"label": "Term", "uid_a": uid_a, "uid_b": uid_b, "score": 0.9, "signals": {}},
            identity=f"duplicate_pair:Term:{uid_a}:{uid_b}",
        )
        review_bc_id = ledger.create_review(
            "duplicate_pair",
            {"label": "Term", "uid_a": uid_b, "uid_b": uid_c, "score": 0.7, "signals": {}},
            identity=f"duplicate_pair:Term:{uid_b}:{uid_c}",
        )
        ledger.approve_review(review_ab_id, decided_by="tester")

        result = dc.apply_approved_duplicate_merge(graph, ledger, review_ab_id)
        assert {result["survivor_uid"], result["absorbed_uid"]} == {uid_a, uid_b}

        # The separate B-C review is untouched: still pending, payload
        # unchanged -- no auto-approve/reject/rewrite as a side effect.
        review_bc_after = ledger.get_review(review_bc_id)
        assert review_bc_after.state == "pending"
        assert review_bc_after.payload["uid_a"] == uid_b
        assert review_bc_after.payload["uid_b"] == uid_c

        # Single-hop only: C itself was never merged into anything.
        assert ledger.merged_into(uid_c) is None
