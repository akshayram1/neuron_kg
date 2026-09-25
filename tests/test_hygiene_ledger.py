"""Phase 6 hygiene job storage (25-plan.md §6.1, §6.2, §6.4):
`ConnectorLedger`'s `hygiene_runs`, `link_candidates`, and `merge_trace`
tables.

Same convention as `tests/test_reviews.py` and
`tests/test_entity_resolution_ledger.py` -- a real temp-file
`ConnectorLedger`, no mocking. This file only exercises the storage/
accessor layer built here; the hygiene LOGIC that produces these rows
(isolated-node counting, candidate scoring, duplicate collection) lives in
`graph/hygiene.py` (parallel work) and is out of scope for these tests.
"""

from __future__ import annotations

from connectors.core.ledger import (
    ConnectorLedger,
    HygieneCount,
    ReviewState,
)

# ------------------------------------------------------------- hygiene_runs


def test_record_hygiene_counts_and_read_trend(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_hygiene_counts(
        "run-1",
        [
            HygieneCount("Document", isolated_count=5, total_count=120),
            HygieneCount("Decision", isolated_count=2, total_count=40),
        ],
    )

    trend = ledger.hygiene_trend("Document")
    assert len(trend) == 1
    row = trend[0]
    assert row.run_id == "run-1"
    assert row.graph_name == "default"
    assert row.label == "Document"
    assert row.isolated_count == 5
    assert row.total_count == 120
    assert row.created_at

    # A different label from the same run is its own row.
    decision_trend = ledger.hygiene_trend("Decision")
    assert len(decision_trend) == 1
    assert decision_trend[0].isolated_count == 2


def test_record_hygiene_counts_is_batch_friendly(tmp_path):
    """A single run measures several labels at once -- one call, several
    rows, not one call per label."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_hygiene_counts(
        "run-1",
        [
            HygieneCount("Document", isolated_count=1, total_count=10),
            HygieneCount("Decision", isolated_count=2, total_count=20),
            HygieneCount("Commit", isolated_count=3, total_count=30),
            HygieneCount("Term", isolated_count=4, total_count=40),
            HygieneCount("System", isolated_count=5, total_count=50),
        ],
    )
    for label, expected in [
        ("Document", 1), ("Decision", 2), ("Commit", 3), ("Term", 4), ("System", 5),
    ]:
        rows = ledger.hygiene_trend(label)
        assert len(rows) == 1
        assert rows[0].isolated_count == expected


def test_record_hygiene_counts_empty_list_is_a_noop(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_hygiene_counts("run-1", [])
    assert ledger.hygiene_trend("Document") == []


def test_multiple_runs_stay_separate_and_orderable_by_time(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_hygiene_counts("run-1", [HygieneCount("Document", 10, 100)])
    ledger.record_hygiene_counts("run-2", [HygieneCount("Document", 8, 105)])
    ledger.record_hygiene_counts("run-3", [HygieneCount("Document", 5, 110)])

    trend = ledger.hygiene_trend("Document")
    assert len(trend) == 3
    # Newest first.
    assert [row.run_id for row in trend] == ["run-3", "run-2", "run-1"]
    assert [row.isolated_count for row in trend] == [5, 8, 10]


def test_hygiene_trend_respects_limit(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    for i in range(5):
        ledger.record_hygiene_counts(f"run-{i}", [HygieneCount("Document", i, 100)])

    trend = ledger.hygiene_trend("Document", limit=2)
    assert len(trend) == 2
    assert [row.run_id for row in trend] == ["run-4", "run-3"]


def test_hygiene_trend_scoped_by_graph_name(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_hygiene_counts("run-1", [HygieneCount("Document", 1, 10)], graph_name="alpha")
    ledger.record_hygiene_counts("run-1", [HygieneCount("Document", 9, 90)], graph_name="beta")

    assert [row.isolated_count for row in ledger.hygiene_trend("Document", graph_name="alpha")] == [1]
    assert [row.isolated_count for row in ledger.hygiene_trend("Document", graph_name="beta")] == [9]
    # Default graph_name has nothing.
    assert ledger.hygiene_trend("Document") == []


# ---------------------------------------------------------- link_candidates


def test_create_link_candidate_starts_pending(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop",
    )
    candidate = ledger.get_link_candidate(candidate_id)
    assert candidate.from_uid == "uid-a"
    assert candidate.to_uid == "uid-b"
    assert candidate.relation == "RELATES_TO"
    assert candidate.derived_rule == "two_hop"
    assert candidate.confidence == 0.5
    assert candidate.state == ReviewState.PENDING
    assert candidate.created_at
    assert candidate.decided_at is None


def test_create_link_candidate_default_confidence_is_dice_neutral(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="semantic_candidate",
    )
    assert ledger.get_link_candidate(candidate_id).confidence == 0.5


def test_idempotent_reproposal_of_same_triple_does_not_duplicate(tmp_path):
    """The same (from_uid, to_uid, relation) triple proposed twice -- e.g.
    by two hygiene runs -- resolves to the same row, not a second one."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    first_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop", confidence=0.5,
    )
    second_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop", confidence=0.5,
    )
    assert first_id == second_id
    assert len(ledger.list_link_candidates(state=None)) == 1


def test_reproposing_a_decided_candidate_does_not_reset_its_state(tmp_path):
    """Once a candidate has been approved or rejected, re-proposing the
    same triple must not resurrect it as pending."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop",
    )
    ledger.reject_link_candidate(candidate_id)

    same_id = ledger.create_link_candidate(
        "uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop",
    )
    assert same_id == candidate_id
    assert ledger.get_link_candidate(candidate_id).state == ReviewState.REJECTED


def test_different_relation_is_a_different_candidate(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    first_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")
    second_id = ledger.create_link_candidate("uid-a", "uid-b", "APPLIES_TO", derived_rule="two_hop")
    assert first_id != second_id
    assert len(ledger.list_link_candidates(state=None)) == 2


def test_list_pending_link_candidates_defaults_to_pending(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    pending_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")
    approved_id = ledger.create_link_candidate("uid-c", "uid-d", "RELATES_TO", derived_rule="two_hop")
    ledger.approve_link_candidate(approved_id)

    pending = ledger.list_link_candidates()
    assert [c.id for c in pending] == [pending_id]


def test_list_link_candidates_filtered_by_derived_rule(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    two_hop_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")
    semantic_id = ledger.create_link_candidate("uid-c", "uid-d", "RELATES_TO", derived_rule="semantic_candidate")

    two_hop_only = ledger.list_link_candidates(derived_rule="two_hop")
    assert [c.id for c in two_hop_only] == [two_hop_id]

    semantic_only = ledger.list_link_candidates(derived_rule="semantic_candidate")
    assert [c.id for c in semantic_only] == [semantic_id]

    both = ledger.list_link_candidates(state=None)
    assert {c.id for c in both} == {two_hop_id, semantic_id}


def test_approve_link_candidate_sets_decided_fields(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")

    decided = ledger.approve_link_candidate(candidate_id)
    assert decided.state == ReviewState.APPROVED
    assert decided.decided_at

    reloaded = ledger.get_link_candidate(candidate_id)
    assert reloaded.state == ReviewState.APPROVED
    assert reloaded.decided_at == decided.decided_at


def test_reject_link_candidate_sets_decided_fields(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")

    decided = ledger.reject_link_candidate(candidate_id)
    assert decided.state == ReviewState.REJECTED
    assert decided.decided_at

    reloaded = ledger.get_link_candidate(candidate_id)
    assert reloaded.state == ReviewState.REJECTED


def test_deciding_an_already_decided_candidate_is_a_noop(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    candidate_id = ledger.create_link_candidate("uid-a", "uid-b", "RELATES_TO", derived_rule="two_hop")
    ledger.approve_link_candidate(candidate_id)

    assert ledger.approve_link_candidate(candidate_id) is None
    assert ledger.reject_link_candidate(candidate_id) is None
    assert ledger.get_link_candidate(candidate_id).state == ReviewState.APPROVED


def test_deciding_a_nonexistent_candidate_returns_none(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.approve_link_candidate(9999) is None
    assert ledger.reject_link_candidate(9999) is None
    assert ledger.get_link_candidate(9999) is None


# --------------------------------------------------------------- merge_trace


def test_record_merge_trace_and_lookup_merged_into(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_merge_trace("uid-survivor", "uid-absorbed", "Decision", reason="duplicate_pair review #1")

    assert ledger.merged_into("uid-absorbed") == "uid-survivor"


def test_merged_into_returns_none_for_a_node_never_merged(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_merge_trace("uid-survivor", "uid-absorbed", "Decision")

    assert ledger.merged_into("uid-survivor") is None
    assert ledger.merged_into("uid-never-touched") is None


def test_merge_trace_reason_is_optional(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    trace_id = ledger.record_merge_trace("uid-survivor", "uid-absorbed", "Term")
    assert trace_id is not None
    assert ledger.merged_into("uid-absorbed") == "uid-survivor"


def test_multiple_merges_stay_independent(tmp_path):
    """Several absorbed nodes merged into different survivors each resolve
    to their own survivor."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_merge_trace("uid-s1", "uid-a1", "Decision")
    ledger.record_merge_trace("uid-s2", "uid-a2", "Term")

    assert ledger.merged_into("uid-a1") == "uid-s1"
    assert ledger.merged_into("uid-a2") == "uid-s2"


def test_merged_into_is_single_hop_only(tmp_path):
    """A chain (A absorbed into B, B later absorbed into C) resolves A to
    its immediate survivor B, not the end of the chain -- following
    multi-hop chains is redirect logic out of this module's scope."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_merge_trace("uid-b", "uid-a", "Decision")
    ledger.record_merge_trace("uid-c", "uid-b", "Decision")

    assert ledger.merged_into("uid-a") == "uid-b"
    assert ledger.merged_into("uid-b") == "uid-c"
