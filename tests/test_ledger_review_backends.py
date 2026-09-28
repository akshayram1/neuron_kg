"""The ledger's review / resolution / hygiene accessors on both SQL backends.

Every test runs against SQLite and (when ``NEURON_TEST_DATABASE_URL`` is
reachable) Postgres through ``storage.sql_backend``: same `ConnectorLedger`
calls, same expected results. Covers the portable-SQL rewrites in that part
of the ledger -- ``RETURNING id`` instead of ``lastrowid``, ``ON CONFLICT``
instead of ``INSERT OR IGNORE/REPLACE``, ``id`` instead of ``rowid``.
"""

from __future__ import annotations

import pytest

from connectors.core.ledger import ConnectorLedger, HygieneCount, ReviewState

pytestmark = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


@pytest.fixture
def ledger(sql_backend, tmp_path):
    return ConnectorLedger(tmp_path / "l.sqlite3")


# ------------------------------------------------------------------ reviews


def test_review_create_list_approve(ledger):
    first = ledger.create_review("entity_merge", {"a": 1}, identity="merge:a|b")
    second = ledger.create_review("fact_update", {"b": [1, 2]})
    assert isinstance(first, int) and isinstance(second, int) and second > first

    review = ledger.get_review(first)
    assert review is not None
    assert review.payload == {"a": 1}
    assert review.identity == "merge:a|b"
    assert review.state == ReviewState.PENDING

    assert [r.id for r in ledger.list_reviews()] == [second, first]
    assert [r.id for r in ledger.list_reviews(type="fact_update")] == [second]
    assert [r.id for r in ledger.list_reviews(state=ReviewState.PENDING, limit=1)] == [second]

    approved = ledger.approve_review(first, "alice")
    assert approved is not None
    assert approved.state == ReviewState.APPROVED
    assert approved.decided_by == "alice"
    assert approved.decided_at
    # decide-once: a second decision is refused
    assert ledger.approve_review(first, "bob") is None
    assert ledger.reject_review(first, "bob") is None
    assert ledger.approve_review(999_999, "alice") is None
    assert [r.id for r in ledger.list_reviews(state=ReviewState.APPROVED)] == [first]


def test_review_reject_caches_identity(ledger):
    review_id = ledger.create_review("entity_merge", {"x": 1}, identity="merge:x|y")
    assert not ledger.is_identity_rejected("merge:x|y")

    rejected = ledger.reject_review(review_id, "carol")
    assert rejected is not None and rejected.state == ReviewState.REJECTED
    assert ledger.is_identity_rejected("merge:x|y")
    # the same proposal is not recreated
    assert ledger.create_review("entity_merge", {"x": 1}, identity="merge:x|y") is None

    # a rejection for an identity already cached is an upsert, not an error
    other = ledger.create_review("entity_merge", {"x": 2}, identity="merge:other")
    ledger.reject_review(other, "carol")
    assert ledger.is_identity_rejected("merge:other")

    # identity-less rejection caches nothing and does not fail
    plain = ledger.create_review("fact_update", {})
    assert ledger.reject_review(plain, "carol").state == ReviewState.REJECTED


# ------------------------------------------------------------------ aliases


def test_entity_aliases_upsert_and_lookup(ledger):
    ledger.add_entity_alias("Person", "ns:1", "alice", "uid-a", "manual")
    ledger.add_entity_alias("Decision", None, "use kafka", "uid-d", "review:1")
    assert ledger.lookup_alias("Person", "ns:1", "alice") == "uid-a"
    assert ledger.lookup_alias("Person", "ns:2", "alice") is None
    assert ledger.lookup_alias("Decision", "", "use kafka") == "uid-d"
    assert ledger.lookup_alias("Decision", None, "use kafka") == "uid-d"

    # re-adding the same alias re-points it rather than duplicating
    ledger.add_entity_alias("Person", "ns:1", "alice", "uid-b", "phase6_merge:3")
    assert ledger.lookup_alias("Person", "ns:1", "alice") == "uid-b"
    assert ledger.aliases_for_uid("uid-a") == []
    [alias] = ledger.aliases_for_uid("uid-b")
    assert (alias.label, alias.namespace_uid, alias.source) == ("Person", "ns:1", "phase6_merge:3")


# ----------------------------------------------------------------- stoplist


def test_mention_stoplist(ledger):
    # seeded global terms exist
    assert ledger.is_stoplisted("  DATA ")
    assert not ledger.is_stoplisted("kafka")

    ledger.add_stoplist_term("Kafka", label="System", reason="too generic here")
    assert ledger.is_stoplisted("kafka", label="System")
    assert not ledger.is_stoplisted("kafka", label="Term")
    assert not ledger.is_stoplisted("kafka")

    ledger.add_stoplist_term("kafka", label="System", reason="updated")
    scoped = [t for t in ledger.stoplist_terms(label="System") if t.label == "System"]
    assert [(t.term_norm, t.reason) for t in scoped] == [("kafka", "updated")]
    assert any(t.label == "" for t in ledger.stoplist_terms(label="System"))

    ledger.remove_stoplist_term("KAFKA", label="System")
    assert not ledger.is_stoplisted("kafka", label="System")


# ------------------------------------------------------------ resolution stats


def test_resolution_stats_accumulate(ledger):
    assert ledger.latest_resolution_stats() == []
    ledger.record_resolution("run-1", "Person", "alias")
    ledger.record_resolution("run-1", "Person", "alias", count=4)
    ledger.record_resolution("run-1", "Person", "scoped_exact", count=2)
    ledger.record_resolution("run-2", "System", "new", count=7)

    stats = ledger.resolution_stats_for_run("run-1")
    assert [(s.label, s.resolved_by, s.count) for s in stats] == [
        ("Person", "alias", 5),
        ("Person", "scoped_exact", 2),
    ]
    latest = ledger.latest_resolution_stats()
    assert [(s.run_id, s.count) for s in latest] == [("run-2", 7)]


# ------------------------------------------------------------- hygiene runs


def test_hygiene_runs(ledger):
    assert ledger.latest_hygiene_snapshot() == []
    ledger.record_hygiene_counts("run-1", [])  # no-op
    ledger.record_hygiene_counts(
        "run-1",
        [HygieneCount("Document", isolated_count=5, total_count=120),
         HygieneCount("Decision", isolated_count=2, total_count=40)],
    )
    ledger.record_hygiene_counts(
        "run-2", [HygieneCount("Document", isolated_count=3, total_count=125)],
    )
    ledger.record_hygiene_counts(
        "run-x", [HygieneCount("Document", isolated_count=1, total_count=2)], graph_name="other",
    )

    trend = ledger.hygiene_trend("Document")
    assert [(r.run_id, r.isolated_count, r.total_count) for r in trend] == [
        ("run-2", 3, 125),
        ("run-1", 5, 120),
    ]
    assert len(ledger.hygiene_trend("Document", limit=1)) == 1
    assert [r.run_id for r in ledger.latest_hygiene_snapshot()] == ["run-2"]
    assert [r.run_id for r in ledger.latest_hygiene_snapshot(graph_name="other")] == ["run-x"]


# ---------------------------------------------------------- link candidates


def test_link_candidate_lifecycle(ledger):
    first = ledger.create_link_candidate("a", "b", "RELATES_TO", derived_rule="two_hop", confidence=0.7)
    again = ledger.create_link_candidate("a", "b", "RELATES_TO", derived_rule="semantic", confidence=0.9)
    assert again == first  # idempotent on the triple; original row kept
    other = ledger.create_link_candidate("a", "c", "RELATES_TO", derived_rule="semantic")

    candidate = ledger.get_link_candidate(first)
    assert candidate is not None
    assert (candidate.derived_rule, candidate.confidence, candidate.state) == (
        "two_hop", 0.7, ReviewState.PENDING,
    )
    assert ledger.get_link_candidate(999_999) is None
    assert [c.id for c in ledger.list_link_candidates()] == [other, first]
    assert [c.id for c in ledger.list_link_candidates(derived_rule="semantic")] == [other]

    approved = ledger.approve_link_candidate(first)
    assert approved is not None and approved.state == ReviewState.APPROVED and approved.decided_at
    assert ledger.approve_link_candidate(first) is None
    rejected = ledger.reject_link_candidate(other)
    assert rejected is not None and rejected.state == ReviewState.REJECTED
    assert ledger.reject_link_candidate(other) is None

    # a decided candidate is never resurrected as pending by a re-proposal
    assert ledger.create_link_candidate("a", "c", "RELATES_TO", derived_rule="semantic") == other
    assert ledger.get_link_candidate(other).state == ReviewState.REJECTED
    assert ledger.list_link_candidates() == []
    assert {c.id for c in ledger.list_link_candidates(state=None)} == {first, other}


# -------------------------------------------------------------- merge trace


def test_merge_trace_and_merged_into(ledger):
    assert ledger.merged_into("absorbed") is None
    first = ledger.record_merge_trace("survivor-1", "absorbed", "Person", reason="dup")
    second = ledger.record_merge_trace("survivor-2", "absorbed", "Person")
    assert isinstance(first, int) and second > first

    assert ledger.merged_into("absorbed") == "survivor-2"  # most recent wins
    assert ledger.merged_into("survivor-1") is None
    traces = ledger.list_merge_traces()
    assert [t.id for t in traces] == [second, first]
    assert traces[1].reason == "dup" and traces[0].reason is None
    assert len(ledger.list_merge_traces(limit=1)) == 1
