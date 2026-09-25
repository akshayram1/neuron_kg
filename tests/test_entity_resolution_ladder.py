"""Phase 4 -- the scoped entity-resolution ladder (25-plan.md §4.0-§4.8).

`graph/semantic_pass.py`'s old two-line identity resolution
(`find_similar_uid(...) or semantic_uid(...)`) is replaced by:
  - §4.0 scoped identity keys (System/Term: namespace-scoped; Decision:
    record+statement-scoped; Api/Endpoint: unchanged),
  - §4.1's polarity veto,
  - the 6-rung ladder in §4.2 (`_resolve_semantic_entity`),
  - §4.4's per-run cache (`semantic_uids`/`decision_name_index`, now owned
    by `run_semantic_pass` and threaded through `_write_extraction`),
  - §4.5's mention filter (`_passes_mention_filter`),
  - §4.7's structural-kind veto (already-existing `_resolve_endpoint`
    behavior, tested here per-label as the plan asks).

These tests exercise the ladder both directly (`_resolve_semantic_entity`,
the finest-grained and easiest place to prove rung order/short-circuiting)
and through `_write_extraction` (mention filter drops, the run-level cache,
§4.7's veto) -- the same real-`ConnectorLedger` /  `_FakeGraph`-double style
`tests/test_admission_gates.py` already uses.
"""

from __future__ import annotations

import pytest

import graph.semantic_pass as sp
from connectors.core.ledger import ConnectorLedger, DropReason, PendingChunk
from graph.axioms import DEFAULT_AXIOMS
from graph.ontology import Api, Decision, Endpoint, System, Term
from graph.profiles import ExtractedFact, WorkManagementExtraction
from graph.semantic_pass import (
    _derive_namespace_uid,
    _passes_mention_filter,
    _resolve_semantic_entity,
    _rung6_laya_same_entity,
    _write_extraction,
    polarity_conflict,
    semantic_uid,
)
from graph.token_usage import TokenUsage
from graph.writer import make_uid


# --------------------------------------------------------------------- doubles


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class _FakeGraph:
    """Just enough of the FalkorDB `Graph` surface for the ladder,
    `_resolve_endpoint`, `_derive_namespace_uid` and `_write_extraction`'s
    own bookkeeping queries -- pattern-matched by substring, same style as
    `test_admission_gates.py`'s `_FakeGraph`.

    `queries` records every Cypher string passed in, in order, so tests can
    prove a rung was (or was NOT) reached by counting query shapes.
    """

    def __init__(
        self,
        *,
        existing_uids: set[str] | None = None,
        node_text: dict[str, str] | None = None,
        project_uid: str | None = None,
        repository_uid: str | None = None,
        workspace_uid: str | None = None,
        person_uid_by_name: dict[str, str] | None = None,
    ):
        self.existing_uids = existing_uids or set()
        self.node_text = node_text or {}
        self.project_uid = project_uid
        self.repository_uid = repository_uid
        self.workspace_uid = workspace_uid
        self.person_uid_by_name = person_uid_by_name or {}
        self.queries: list[str] = []

    def query(self, cypher, params=None):
        params = params or {}
        self.queries.append(cypher)
        if "SourceRecord" in cypher:
            return _FakeResult([])
        if "RETURN n.uid LIMIT 1" in cypher:
            uid = params.get("uid")
            return _FakeResult([[uid]] if uid in self.existing_uids else [])
        if "RETURN n.search_text, n.name" in cypher:
            text = self.node_text.get(params.get("uid"))
            return _FakeResult([[text, text]] if text is not None else [])
        if "ASSIGNED_TO|REPORTED_BY|AUTHORED_BY" in cypher:
            uid = self.person_uid_by_name.get(str(params.get("name", "")).strip().lower())
            return _FakeResult([[uid]] if uid else [])
        if "BELONGS_TO]->(p:Project)" in cypher:
            return _FakeResult([[self.project_uid]] if self.project_uid else [])
        if "(p:Repository)-[:CONTAINS]" in cypher:
            return _FakeResult([[self.repository_uid]] if self.repository_uid else [])
        if "(p:Workspace)-[:CONTAINS]" in cypher:
            return _FakeResult([[self.workspace_uid]] if self.workspace_uid else [])
        return _FakeResult([])  # e.g. the UNWIND MERGE writer calls


def _ladder(graph, ledger, label, item, vector, *, namespace_uid="", record_key="k1",
            semantic_uids=None, decision_name_index=None, collection="test"):
    return _resolve_semantic_entity(
        graph, ledger, label, item, vector,
        namespace_uid=namespace_uid, record_key=record_key,
        semantic_uids=semantic_uids if semantic_uids is not None else {},
        decision_name_index=decision_name_index if decision_name_index is not None else {},
        collection=collection,
    )


def _no_vector_search(*args, **kwargs):
    raise AssertionError("vector_store.search_above must not be called for this rung")


@pytest.fixture(autouse=True)
def _isolate_ladder_tests_from_qdrant(monkeypatch):
    """Search is faked per test, so constructing a live client is accidental."""
    monkeypatch.setattr(sp.vector_store, "client", lambda: object())


# ------------------------------------------------------------- §4.1 polarity


def test_polarity_conflict_plan_examples():
    # Both are the plan's own illustration: two decisions differing only by
    # negation must never be treated as the same thing.
    assert polarity_conflict(
        "use Redis sliding window for rate limiting",
        "do not use Redis sliding window for rate limiting",
    )
    assert polarity_conflict(
        "we will keep using the v1 export endpoint",
        "deprecated the v1 export endpoint",
    )


def test_polarity_conflict_false_for_non_conflicting_pairs():
    assert not polarity_conflict(
        "use Redis sliding window for rate limiting",
        "use a Redis-backed sliding window rate limiter",
    )
    # Both negated -> same polarity, not a conflict.
    assert not polarity_conflict(
        "never use raw SQL in the handler", "we will never use raw SQL in handlers",
    )


# --------------------------------------------------------- §4.0 scoped identity


def test_system_same_name_different_namespace_gets_different_uid(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    item = System(name="Redis")

    uid_a, by_a, new_a = _ladder(_FakeGraph(), ledger, "System", item, None, namespace_uid="ns-a")
    uid_b, by_b, new_b = _ladder(_FakeGraph(), ledger, "System", item, None, namespace_uid="ns-b")

    assert uid_a != uid_b
    assert (by_a, new_a) == ("new", True)
    assert (by_b, new_b) == ("new", True)
    assert uid_a == make_uid("System", "ns-a", "redis")
    assert uid_b == make_uid("System", "ns-b", "redis")


def test_term_repeated_scoped_name_reuses_uid_via_run_cache(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph()
    semantic_uids: dict = {}
    item = Term(name="blast radius", definition="how much breaks")

    uid1, by1, new1 = _ladder(graph, ledger, "Term", item, None, namespace_uid="ns-a",
                               semantic_uids=semantic_uids)
    uid2, by2, new2 = _ladder(graph, ledger, "Term", item, None, namespace_uid="ns-a",
                               semantic_uids=semantic_uids)

    assert uid1 == uid2
    assert (by1, new1) == ("new", True)
    assert (by2, new2) == ("scoped_exact", False)


def test_decision_uid_is_scoped_by_record_and_statement_not_name(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    same_name_different_statement_a = Decision(
        name="use redis", statement="We will use Redis for rate limiting.",
    )
    same_name_different_statement_b = Decision(
        name="use redis", statement="We will use Postgres for rate limiting.",
    )

    uid_a, by_a, new_a = _ladder(
        _FakeGraph(), ledger, "Decision", same_name_different_statement_a, None,
        record_key="jira:c:work_item:W-1",
    )
    uid_b, by_b, new_b = _ladder(
        _FakeGraph(), ledger, "Decision", same_name_different_statement_b, None,
        record_key="jira:c:work_item:W-1",
    )

    assert uid_a != uid_b, "same name, different statement -> different identity"
    assert uid_a == make_uid(
        "Decision", "jira:c:work_item:W-1", sp._normalize_identity(same_name_different_statement_a.statement),
    )

    # Same statement, different record -> also a different uid (record-scoped).
    uid_c, *_ = _ladder(
        _FakeGraph(), ledger, "Decision", same_name_different_statement_a, None,
        record_key="jira:c:work_item:W-2",
    )
    assert uid_c != uid_a


def test_api_and_endpoint_identity_is_unchanged_and_unscoped(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    api_item = Api(name="Auth API v1", version="v1")
    endpoint_item = Endpoint(name="POST /v1/auth", method="POST", path="/v1/auth")

    api_uid, api_by, api_new = _ladder(_FakeGraph(), ledger, "Api", api_item, None, namespace_uid="ns-a")
    api_uid2, *_ = _ladder(_FakeGraph(), ledger, "Api", api_item, None, namespace_uid="ns-b")
    endpoint_uid, ep_by, ep_new = _ladder(_FakeGraph(), ledger, "Endpoint", endpoint_item, None, namespace_uid="ns-a")

    assert api_uid == semantic_uid("Api", "Auth API v1")
    assert api_uid == api_uid2, "namespace must not affect Api/Endpoint identity (§4.0)"
    assert endpoint_uid == semantic_uid("Endpoint", "POST /v1/auth")
    assert (api_by, api_new) == ("new", True)
    assert (ep_by, ep_new) == ("new", True)


# --------------------------------------------------------------- §4.0 namespace


def test_namespace_uses_project_when_record_is_a_workitem():
    graph = _FakeGraph(project_uid="proj-42")
    ns = _derive_namespace_uid(graph, "jira:c:work_item:W-1", "WorkItem", "wi-uid-1")
    assert ns == "proj-42"


def test_namespace_uses_own_uid_when_record_is_a_project():
    ns = _derive_namespace_uid(_FakeGraph(), "jira:c:project:p1", "Project", "proj-1")
    assert ns == "proj-1"


def test_namespace_falls_back_to_connection_scope_when_nothing_structural_found():
    graph = _FakeGraph()  # no project/workspace configured
    ns = _derive_namespace_uid(graph, "notion:conn-1:page:p1", "Document", "doc-uid-1")
    assert ns == "notion:conn-1"


# --------------------------------------------------------- ladder rung order


def test_scoped_exact_hit_short_circuits_before_alias_or_vector(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.vector_store, "search_above", _no_vector_search)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    lookup_calls: list = []
    original_lookup = ledger.lookup_alias
    monkeypatch.setattr(
        ledger, "lookup_alias",
        lambda *a, **k: lookup_calls.append((a, k)) or original_lookup(*a, **k),
    )
    item = System(name="Redis")
    expected_uid = make_uid("System", "ns-a", "redis")
    graph = _FakeGraph(existing_uids={expected_uid})

    uid, resolved_by, is_new = _ladder(
        graph, ledger, "System", item, [0.1, 0.2], namespace_uid="ns-a",
    )

    assert (uid, resolved_by, is_new) == (expected_uid, "scoped_exact", False)
    assert lookup_calls == []


def test_alias_hit_short_circuits_before_vector(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.vector_store, "search_above", _no_vector_search)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("System", "ns-a", "redis", "existing-uid-123", source="manual")
    item = System(name="Redis")

    uid, resolved_by, is_new = _ladder(
        _FakeGraph(), ledger, "System", item, [0.1], namespace_uid="ns-a",
    )

    assert (uid, resolved_by, is_new) == ("existing-uid-123", "alias", False)


def test_vector_exactly_one_high_similarity_candidate_merges(tmp_path, monkeypatch):
    def fake_search_above(client, label, vector, min_similarity, namespace_uid=None,
                           max_similarity=None, limit=20, collection=None):
        return [("cand-uid", 0.95)] if min_similarity == 0.90 else []

    monkeypatch.setattr(sp.vector_store, "search_above", fake_search_above)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph(node_text={"cand-uid": "use Redis sliding window for rate limiting"})
    item = Decision(name="use redis", statement="use Redis sliding window for rate limiting")

    uid, resolved_by, is_new = _ladder(graph, ledger, "Decision", item, [0.1], record_key="k1")

    assert (uid, resolved_by, is_new) == ("cand-uid", "vector", False)


def test_vector_more_than_one_high_similarity_candidate_is_not_confident(tmp_path, monkeypatch):
    seen_thresholds = []

    def fake_search_above(client, label, vector, min_similarity, namespace_uid=None,
                           max_similarity=None, limit=20, collection=None):
        seen_thresholds.append((min_similarity, max_similarity))
        if min_similarity == 0.90:
            return [("cand-1", 0.95), ("cand-2", 0.91)]
        return [("cand-3", 0.80)]

    monkeypatch.setattr(sp.vector_store, "search_above", fake_search_above)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    item = Decision(name="use redis", statement="use Redis for rate limiting")

    uid, resolved_by, is_new = _ladder(_FakeGraph(), ledger, "Decision", item, [0.1], record_key="k1")

    assert resolved_by == "new" and is_new
    # rung 4 (>=0.90) was queried, and since it wasn't confident (2 hits),
    # rung 5's gray zone (0.75<=sim<0.90) was queried too, per §4.2.
    assert (0.90, None) in seen_thresholds
    assert (0.75, 0.90) in seen_thresholds


def test_gray_zone_only_candidates_route_to_rung6_and_resolve_new(tmp_path, monkeypatch):
    seen_thresholds = []

    def fake_search_above(client, label, vector, min_similarity, namespace_uid=None,
                           max_similarity=None, limit=20, collection=None):
        seen_thresholds.append((min_similarity, max_similarity))
        if min_similarity == 0.90:
            return []
        return [("gray-cand", 0.80)]

    monkeypatch.setattr(sp.vector_store, "search_above", fake_search_above)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    item = System(name="Nilus")

    uid, resolved_by, is_new = _ladder(_FakeGraph(), ledger, "System", item, [0.1], namespace_uid="ns-a")

    assert resolved_by == "new" and is_new
    assert uid != "gray-cand"
    assert (0.75, 0.90) in seen_thresholds


def test_polarity_conflict_vetoes_a_single_vector_candidate_and_queues_decision_review(tmp_path, monkeypatch):
    def fake_search_above(client, label, vector, min_similarity, namespace_uid=None,
                           max_similarity=None, limit=20, collection=None):
        return [("cand-uid", 0.95)] if min_similarity == 0.90 else []

    monkeypatch.setattr(sp.vector_store, "search_above", fake_search_above)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph(node_text={"cand-uid": "do not use Redis for rate limiting"})
    item = Decision(name="use redis", statement="use Redis for rate limiting")

    uid, resolved_by, is_new = _ladder(
        graph, ledger, "Decision", item, [0.1], record_key="jira:c:work_item:W1",
    )

    assert resolved_by == "new" and is_new
    assert uid != "cand-uid", "polarity conflict must never merge"
    reviews = ledger.list_reviews(type="polarity_conflict_candidate")
    assert len(reviews) == 1
    assert reviews[0].payload["object_uid"] == "cand-uid"
    assert reviews[0].payload["subject_uid"] == uid


def test_polarity_conflict_is_not_vetoed_for_non_decision_labels(tmp_path, monkeypatch):
    """§4.1 says "for Decisions" hand the pair to Phase 5 -- but the veto
    itself (never merge on a polarity conflict) is general; only the review
    proposal is Decision-specific. Confirm a System candidate under polarity
    conflict is vetoed (new node) WITHOUT creating a review row."""
    def fake_search_above(client, label, vector, min_similarity, namespace_uid=None,
                           max_similarity=None, limit=20, collection=None):
        return [("cand-uid", 0.95)] if min_similarity == 0.90 else []

    monkeypatch.setattr(sp.vector_store, "search_above", fake_search_above)
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph(node_text={"cand-uid": "deprecated Redis"})
    item = System(name="Redis", purpose="use Redis")

    uid, resolved_by, is_new = _ladder(graph, ledger, "System", item, [0.1], namespace_uid="ns-a")

    assert resolved_by == "new" and is_new
    assert ledger.list_reviews(type="polarity_conflict_candidate") == []


def test_rung6_placeholder_always_falls_through_to_new():
    # Documented placeholder (§4.2 rung 6, blocked on Laya packaging): never
    # returns a candidate today, regardless of how strong the input looks.
    assert _rung6_laya_same_entity("Decision", object(), [("a", 0.99), ("b", 0.70)]) == (None, 0.0)


# ------------------------------------------------------------- §4.5 mention filter


def test_stoplisted_term_fails_mention_filter(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert not _passes_mention_filter(ledger, "Term", "data")
    assert not _passes_mention_filter(ledger, "System", "pipeline")


def test_minimum_length_rejects_short_mention(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert not _passes_mention_filter(ledger, "System", "ab")


def test_phrase_of_only_generic_tokens_is_rejected(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert not _passes_mention_filter(ledger, "Term", "data pipeline")


def test_real_term_and_system_pass_mention_filter(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert _passes_mention_filter(ledger, "Term", "blast radius")
    assert _passes_mention_filter(ledger, "System", "Redis")


def test_mention_filter_is_scoped_to_term_and_system_only(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert _passes_mention_filter(ledger, "Decision", "data")
    assert _passes_mention_filter(ledger, "Api", "data")


def test_write_extraction_drops_stoplisted_mention_as_generic_mention(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "The pipeline handles ingestion.")
    extraction = WorkManagementExtraction(systems=[System(name="pipeline")])
    graph = _FakeGraph()

    entities, written, rejected = _write_extraction(
        graph, ledger, chunk, extraction, "proj-1", "Project",
        client=None, embedding_model="test-embed", extraction_model="test-model",
        profile_name="work_management", token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
    )

    assert entities == 0
    only = ledger.drops(reason=DropReason.GENERIC_MENTION)
    assert len(only) == 1 and only[0].subject_name == "pipeline"


# ------------------------------------------------------------- §4.4 run cache


def test_run_level_cache_avoids_requerying_graph_for_a_later_chunk(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph()
    semantic_uids: dict = {}
    decision_name_index: dict = {}
    extraction = WorkManagementExtraction(
        systems=[System(name="Redis")],
        facts=[ExtractedFact(
            subject_kind="Decision", subject_name="use redis", relation="APPLIES_TO",
            object_kind="System", object_name="Redis", evidence="irrelevant",
        )],
    )
    chunk1 = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "chunk one text")
    chunk2 = PendingChunk("jira:c:project:proj-1", "chunk-2", 1, "chunk two text")

    _write_extraction(
        graph, ledger, chunk1, extraction, "proj-1", "Project",
        client=None, embedding_model="e", extraction_model="m", profile_name="wm",
        token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
        run_id="run-1", semantic_uids=semantic_uids, decision_name_index=decision_name_index,
    )
    scoped_uid_queries_after_first = sum(1 for q in graph.queries if "RETURN n.uid LIMIT 1" in q)
    assert scoped_uid_queries_after_first >= 1, "first mention must actually check the graph"
    queries_so_far = len(graph.queries)

    _write_extraction(
        graph, ledger, chunk2, extraction, "proj-1", "Project",
        client=None, embedding_model="e", extraction_model="m", profile_name="wm",
        token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
        run_id="run-1", semantic_uids=semantic_uids, decision_name_index=decision_name_index,
    )

    new_scoped_uid_queries = sum(
        1 for q in graph.queries[queries_so_far:] if "RETURN n.uid LIMIT 1" in q
    )
    assert new_scoped_uid_queries == 0, "second chunk should resolve 'Redis' from the run cache"

    stats = {row.resolved_by: row.count for row in ledger.resolution_stats_for_run("run-1")}
    assert stats.get("new", 0) == 1  # chunk 1 minted it
    assert stats.get("scoped_exact", 0) == 1  # chunk 2 found it in the run cache


def test_run_level_cache_is_isolated_between_separate_runs(tmp_path):
    """A fresh `run_id`/cache pair (as `run_semantic_pass` creates per call)
    must NOT see another run's memory -- the graph is the only thing that
    persists across runs, and this `_FakeGraph` has nothing in it, so a
    second run resolving the same mention with fresh dicts must re-mint."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    graph = _FakeGraph()
    extraction = WorkManagementExtraction(
        systems=[System(name="Redis")],
        facts=[ExtractedFact(
            subject_kind="Decision", subject_name="use redis", relation="APPLIES_TO",
            object_kind="System", object_name="Redis", evidence="irrelevant",
        )],
    )
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "chunk text")

    _write_extraction(
        graph, ledger, chunk, extraction, "proj-1", "Project",
        client=None, embedding_model="e", extraction_model="m", profile_name="wm",
        token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
        run_id="run-a", semantic_uids={}, decision_name_index={},
    )
    _write_extraction(
        graph, ledger, chunk, extraction, "proj-1", "Project",
        client=None, embedding_model="e", extraction_model="m", profile_name="wm",
        token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
        run_id="run-b", semantic_uids={}, decision_name_index={},
    )

    stats_a = {row.resolved_by: row.count for row in ledger.resolution_stats_for_run("run-a")}
    stats_b = {row.resolved_by: row.count for row in ledger.resolution_stats_for_run("run-b")}
    # Both runs mint it independently via rung 2's graph check (the uid is
    # deterministic, so both compute the SAME uid -- `_FakeGraph.existing_uids`
    # is never populated by a write in this double, so both see "not found").
    assert stats_a.get("new", 0) == 1
    assert stats_b.get("new", 0) == 1


# ------------------------------------------------------- §4.7 structural veto


@pytest.mark.parametrize("object_kind,relation,object_name", [
    ("Person", "DECIDED_BY", "Nobody Known"),
    ("WorkItem", "APPLIES_TO", "UNKNOWN-999"),
    ("Commit", "APPLIES_TO", "deadbeef0000"),
    ("PullRequest", "APPLIES_TO", "PR-999"),
    ("SourceFile", "APPLIES_TO", "unknown/path.py"),
    ("Repository", "APPLIES_TO", "unknown-repo"),
])
def test_unknown_structural_mention_rejected_never_minted(tmp_path, object_kind, relation, object_name):
    """§4.7: `_resolve_endpoint` already refuses to create Person / WorkItem
    / Commit / PullRequest / SourceFile / Repository from text -- these
    labels have no entity-writing loop branch at all (only Decision/Term/
    System/Api/Endpoint do), so the only way a mention of one could ever
    become live knowledge is via `_resolve_endpoint`'s fact-endpoint
    resolution. An unknown mention (not the chunk's own record, not a known
    ASSIGNED_TO/REPORTED_BY/AUTHORED_BY neighbor) must resolve to `None`
    there and be rejected with ENDPOINT_UNRESOLVED -- never minted."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    evidence = f"We decided to use Redis, which applies to {object_name}."
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, evidence)
    fact = ExtractedFact(
        subject_kind="Decision", subject_name="use redis", relation=relation,
        object_kind=object_kind, object_name=object_name, evidence=evidence,
    )
    extraction = WorkManagementExtraction(facts=[fact])
    graph = _FakeGraph()  # nothing exists: no self-record match, no neighbor

    entities, written, rejected = _write_extraction(
        graph, ledger, chunk, extraction, "proj-1", "Project",
        client=None, embedding_model="test-embed", extraction_model="test-model",
        profile_name="work_management", token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
    )

    assert entities == 0
    assert written == 0 and rejected == 1
    only = ledger.drops(reason=DropReason.ENDPOINT_UNRESOLVED)
    assert len(only) == 1
