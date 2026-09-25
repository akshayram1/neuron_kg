"""25-plan.md Phase 5 §5.2 — `resolve_text_fact` (`graph/resolve_text_fact.py`).

Two tiers, same convention `tests/test_writer_temporal.py` and
`tests/test_dates.py` already use in this codebase:

- Pure unit tests (`windows_disjoint`, the Laya kind-mapping,
  `classify_fact_update`'s placeholder behavior, `link_disputed`'s
  unsupported-kind guard) need no graph and always run.
- Everything that reads/writes the graph is gated behind
  `NEURON_INTEGRATION=1` (real FalkorDB, one throwaway graph per test,
  deleted in `finally` — same fixture shape as `tests/test_writer_temporal.py`).
  A `ConnectorLedger` backed by a `tmp_path` sqlite file (same convention as
  `tests/test_reviews.py`) provides the review queue for suggest-mode tests.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from connectors.core.ledger import ConnectorLedger, ReviewState
from graph import writer as w
from graph.fact_predicates import LIVE_FACT_CYPHER
from graph.falkor_client import build_client
from graph.resolve_text_fact import (
    NewFact,
    OldFact,
    UnsupportedDisputeError,
    _map_laya_kind_to_resolve_kind,
    classify_fact_update,
    find_conflict_candidates,
    link_disputed,
    resolve_text_fact,
    windows_disjoint,
)

integration = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


# =========================================================================
# Pure unit tests — no graph, no Laya, always run.
# =========================================================================


def test_windows_disjoint_overlapping_is_false():
    assert windows_disjoint(
        "2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z",
        "2026-02-01T00:00:00Z", "2026-04-01T00:00:00Z",
    ) is False


def test_windows_disjoint_truly_disjoint_is_true():
    assert windows_disjoint(
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z",
    ) is True


def test_windows_disjoint_touching_boundary_is_disjoint():
    # half-open [start, end): an interval ending exactly when the next
    # begins shares no instant.
    assert windows_disjoint(
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z",
    ) is True


def test_windows_disjoint_unbounded_old_end_never_disjoint_from_later_start():
    # old has no end (still live) -> anything starting after old's start
    # necessarily overlaps it.
    assert windows_disjoint("2026-01-01T00:00:00Z", None, "2030-01-01T00:00:00Z", None) is False


def test_windows_disjoint_unbounded_new_overlaps_everything():
    assert windows_disjoint("2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z", None, None) is False


def test_windows_disjoint_both_fully_unbounded_is_false():
    assert windows_disjoint(None, None, None, None) is False


def test_windows_disjoint_missing_starts_but_new_ends_before_old_starts():
    # new is a closed historical window that ends before old's (unbounded)
    # start -- the "Bob was on-call Jan-Feb, Carol has been on-call since
    # June" scenario from the module docstring.
    assert windows_disjoint(
        "2026-06-01T00:00:00Z", None,
        "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
    ) is True


def test_classify_fact_update_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        classify_fact_update("the limit is 100", "2026-01-01T00:00:00Z", "the limit is now 1000", "2026-06-01T00:00:00Z")


@pytest.mark.parametrize(
    "laya_kind,expected",
    [
        ("duplicate", "duplicate"),
        ("updates", "newer_state"),
        ("contradicts", "contradicts"),
        ("extends", "extends"),
        ("unrelated", "unrelated"),
    ],
)
def test_map_laya_kind_to_resolve_kind(laya_kind, expected):
    assert _map_laya_kind_to_resolve_kind(laya_kind) == expected


def test_map_laya_updates_never_maps_to_corrects():
    # The critical mapping: Laya's "updates" must land on the less
    # destructive "newer_state", never "corrects" -- see module docstring.
    assert _map_laya_kind_to_resolve_kind("updates") == "newer_state"
    assert _map_laya_kind_to_resolve_kind("updates") != "corrects"


def test_map_laya_kind_unknown_raises():
    with pytest.raises(ValueError):
        _map_laya_kind_to_resolve_kind("corrects")  # not a real Laya output
    with pytest.raises(ValueError):
        _map_laya_kind_to_resolve_kind("newer_state")  # a resolve_text_fact kind, not a Laya one
    with pytest.raises(ValueError):
        _map_laya_kind_to_resolve_kind("garbage")


def test_link_disputed_raises_without_touching_graph_when_neither_side_is_decision():
    old = OldFact(
        fact_uid="f-old", from_uid="wi1", from_label="WorkItem",
        to_uid="p1", to_label="Person", rel_type="ASSIGNED_TO", valid_at=None,
    )
    new = NewFact(
        from_uid="wi2", from_label="WorkItem", to_uid="p2", to_label="Person",
        rel_type="ASSIGNED_TO", source_time="2026-01-01T00:00:00Z",
    )
    # `graph=None` proves the guard fires before any graph access is attempted.
    with pytest.raises(UnsupportedDisputeError):
        link_disputed(None, old, new)


def test_link_disputed_same_decision_on_both_sides_is_a_noop_without_graph():
    old = OldFact(
        fact_uid="f-old", from_uid="d1", from_label="Decision",
        to_uid="t1", to_label="Term", rel_type="APPLIES_TO", valid_at=None,
    )
    new = NewFact(
        from_uid="d1", from_label="Decision", to_uid="t2", to_label="Term",
        rel_type="APPLIES_TO", source_time="2026-01-01T00:00:00Z",
    )
    link_disputed(None, old, new)  # would raise on a None.query() call if it tried to write


# =========================================================================
# Integration tests — real FalkorDB.
# =========================================================================


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_{uuid4().hex}")
    w.upsert_entities(g, "Person", [{"uid": "p1", "props": {"name": "Alice"}}])
    w.upsert_entities(g, "Person", [{"uid": "p2", "props": {"name": "Bob"}}])
    w.upsert_entities(g, "Person", [{"uid": "p3", "props": {"name": "Carol"}}])
    w.upsert_entities(g, "WorkItem", [{"uid": "wi1", "props": {"name": "PROJ-1"}}])
    w.upsert_entities(g, "Term", [{"uid": "t1", "props": {"name": "Rate Limit"}}])
    w.upsert_entities(g, "Term", [{"uid": "t2", "props": {"name": "Retry Policy"}}])
    w.upsert_entities(g, "Decision", [{"uid": "d1", "props": {"name": "Use Postgres"}}])
    w.upsert_entities(g, "Decision", [{"uid": "d2", "props": {"name": "Use FalkorDB instead"}}])
    w.upsert_entities(g, "Decision", [{"uid": "d3", "props": {"name": "Third decision"}}])
    try:
        yield g
    finally:
        g.delete()


@pytest.fixture
def ledger(tmp_path):
    return ConnectorLedger(tmp_path / "ledger.sqlite3")


def _fact_uid(graph, rel_type, from_uid, to_uid):
    row = graph.query(
        f"MATCH (a {{uid: $f}})-[r:{rel_type}]->(b {{uid: $t}}) RETURN r.fact_uid",
        params={"f": from_uid, "t": to_uid},
    ).result_set
    return row[0][0] if row else None


def _edge_row(graph, fact_uid):
    row = graph.query(
        "MATCH ()-[r]->() WHERE r.fact_uid = $fu "
        "RETURN r.invalid_at, r.assertion_status, r.projection_status, r.ended_unknown, "
        "r.attested_from, r.attested_by_record, r.corrected_by, r.correction_observed_at, "
        "r.last_confirmed_at, r.source_record_keys",
        params={"fu": fact_uid},
    ).result_set
    return row[0] if row else None


def _seed_live_fact(graph, rel_type, from_label, to_label, from_uid, to_uid, *, valid_at=None, pinned=False):
    w.upsert_fact_edges(graph, rel_type, from_label, to_label, [{
        "from_uid": from_uid, "to_uid": to_uid, "source_record_keys": ["rec-old"],
        "evidence": "old evidence", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": valid_at, "pinned": pinned,
    }])
    return _fact_uid(graph, rel_type, from_uid, to_uid)


def _old_and_new(
    graph, *, rel_type, from_label, to_label, old_to, new_to,
    old_valid_at=None, new_valid_at=None, pinned=False,
    from_uid="p1", new_source_time="2026-06-01T00:00:00Z", new_invalid_at=None,
):
    old_fact_uid = _seed_live_fact(
        graph, rel_type, from_label, to_label, from_uid, old_to,
        valid_at=old_valid_at, pinned=pinned,
    )
    old = OldFact(
        fact_uid=old_fact_uid, from_uid=from_uid, from_label=from_label,
        to_uid=old_to, to_label=to_label, rel_type=rel_type,
        valid_at=old_valid_at, pinned=pinned,
    )
    new = NewFact(
        from_uid=from_uid, from_label=from_label, to_uid=new_to, to_label=to_label,
        rel_type=rel_type, source_time=new_source_time, source_record_key="rec-new",
        valid_at=new_valid_at, invalid_at=new_invalid_at,
        evidence="new evidence", confidence=0.8,
    )
    return old, new


# ------------------------------------------------------------- duplicate


@integration
def test_duplicate_confirms_and_returns_no_new_fact(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t1",
    )
    result = resolve_text_fact(graph, ledger, new, old, "duplicate", mode="suggest")
    assert result["action"] == "duplicate"
    assert result["new_fact_uid"] is None
    row = _edge_row(graph, old.fact_uid)
    assert row[0] is None  # invalid_at untouched
    assert "rec-new" in row[9]  # provenance folded into the old fact


@integration
def test_pinned_fact_can_still_be_confirmed_as_duplicate(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t1", pinned=True,
    )
    result = resolve_text_fact(graph, ledger, new, old, "duplicate", mode="suggest")
    assert result["action"] == "duplicate"
    # no review created, no DISPUTED_WITH -- duplicate short-circuits before
    # the pinned check per the plan's own branch order.
    assert ledger.list_reviews() == []


# ------------------------------------------------------------- extends/unrelated


@integration
@pytest.mark.parametrize("kind", ["extends", "unrelated"])
def test_extends_and_unrelated_write_new_live_and_leave_old_untouched(graph, ledger, kind):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2",
    )
    result = resolve_text_fact(graph, ledger, new, old, kind, mode="suggest")
    assert result["action"] == kind
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert new_row[1] in (None, "live")  # assertion_status
    assert (new_row[2] or "live") == "live"  # projection_status
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None  # old still live, untouched


# ------------------------------------------------------------- ended_unknown


@integration
def test_missing_dates_marks_old_ended_unknown_and_writes_new_normally(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2", old_valid_at=None, new_valid_at=None,
    )
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="suggest")
    assert result["action"] == "ended_unknown"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[3] is True  # ended_unknown
    assert old_row[4] == new.source_time  # attested_from
    assert old_row[5] == "rec-new"  # attested_by_record
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert (new_row[2] or "live") == "live"  # written normally, not gated


# ------------------------------------------------------------- newer_state


@integration
def test_newer_state_old_older_closes_old_immediately_in_auto_mode(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2",
        old_valid_at="2026-01-01T00:00:00Z", new_valid_at="2026-06-01T00:00:00Z",
    )
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="auto")
    assert result["action"] == "newer_state_close"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] == "2026-06-01T00:00:00Z"  # invalid_at == new.valid_at
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert (new_row[2] or "live") == "live"  # promoted live after old closed
    assert ledger.list_reviews() == []  # auto mode never creates a review


@integration
def test_newer_state_old_older_suggest_mode_defers_and_writes_pending_review(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2",
        old_valid_at="2026-01-01T00:00:00Z", new_valid_at="2026-06-01T00:00:00Z",
    )
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="suggest")
    assert result["action"] == "newer_state_close"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None  # old NOT closed yet
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert new_row[2] == "pending_review"  # excluded from live reads
    reviews = ledger.list_reviews(type="fact_update")
    assert len(reviews) == 1
    assert reviews[0].state == ReviewState.PENDING
    assert reviews[0].payload["action"] == "newer_state_close"
    assert reviews[0].payload["old_fact_uid"] == old.fact_uid
    # excluded via the centralized live-fact predicate too
    live = graph.query(
        f"MATCH ()-[r]->() WHERE r.fact_uid = $fu AND {LIVE_FACT_CYPHER} RETURN r",
        params={"fu": result["new_fact_uid"]},
    ).result_set
    assert live == []


@integration
def test_newer_state_new_older_writes_new_already_closed_ungated(graph, ledger):
    # Incoming fact is historically OLDER than the currently-live fact --
    # backfill branch. Not suggest/auto-gated: `old` is never touched.
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t2", new_to="t1",
        old_valid_at="2026-06-01T00:00:00Z", new_valid_at="2026-01-01T00:00:00Z",
    )
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="suggest")
    assert result["action"] == "newer_state_backfill"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None  # old (still current) untouched
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert new_row[0] == "2026-06-01T00:00:00Z"  # new written already closed at old.valid_at
    assert ledger.list_reviews() == []  # ungated -- no review for this branch


@integration
def test_newer_state_disjoint_windows_writes_new_live_no_mutation(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t2", new_to="t1",
        old_valid_at="2026-06-01T00:00:00Z", new_valid_at="2026-01-01T00:00:00Z",
        new_invalid_at="2026-02-01T00:00:00Z",  # closed window, well before old starts
    )
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="suggest")
    assert result["action"] == "newer_state_disjoint"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert (new_row[2] or "live") == "live"
    assert ledger.list_reviews() == []


# ------------------------------------------------------------- corrects


@integration
def test_corrects_suggest_mode_defers(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2", old_valid_at="2026-01-01T00:00:00Z",
        new_valid_at="2026-06-01T00:00:00Z",
    )
    result = resolve_text_fact(graph, ledger, new, old, "corrects", mode="suggest")
    assert result["action"] == "corrects"
    old_row = _edge_row(graph, old.fact_uid)
    assert (old_row[1] or "live") == "live"  # not corrected yet
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert new_row[2] == "pending_review"
    assert len(ledger.list_reviews(type="fact_update")) == 1


@integration
def test_corrects_auto_mode_applies_correct_fact_and_activates_new(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="OWNS", from_label="Person", to_label="Term",
        old_to="t1", new_to="t2", old_valid_at="2026-01-01T00:00:00Z",
        new_valid_at="2026-06-01T00:00:00Z",
    )
    result = resolve_text_fact(graph, ledger, new, old, "corrects", mode="auto")
    assert result["action"] == "corrects"
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[1] == "corrected"
    assert old_row[6] == result["new_fact_uid"]  # corrected_by
    assert old_row[7] == new.source_time  # correction_observed_at
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert (new_row[2] or "live") == "live"


# ------------------------------------------------------------- contradicts / pinned


@integration
def test_contradicts_suggest_mode_defers_no_disputed_with_yet(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="APPLIES_TO", from_label="Decision", to_label="Term",
        from_uid="d1", old_to="t1", new_to="t1",
    )
    # new comes from a different Decision -- rebuild `new` with the right from_uid
    new.from_uid = "d2"
    result = resolve_text_fact(graph, ledger, new, old, "contradicts", mode="suggest")
    assert result["action"] == "contradicts"
    disputed = graph.query(
        "MATCH (a {uid:'d1'})-[r:DISPUTED_WITH]->(b {uid:'d2'}) RETURN r"
    ).result_set
    assert disputed == []
    assert len(ledger.list_reviews(type="fact_update")) == 1


@integration
def test_contradicts_auto_mode_links_disputed_and_activates_new(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="APPLIES_TO", from_label="Decision", to_label="Term",
        from_uid="d1", old_to="t1", new_to="t1",
    )
    new.from_uid = "d2"
    result = resolve_text_fact(graph, ledger, new, old, "contradicts", mode="auto")
    assert result["action"] == "contradicts"
    disputed = graph.query(
        "MATCH (a {uid:'d1'})-[r:DISPUTED_WITH]->(b {uid:'d2'}) RETURN r"
    ).result_set
    assert len(disputed) == 1
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None  # both remain live
    new_row = _edge_row(graph, result["new_fact_uid"])
    assert (new_row[2] or "live") == "live"


@integration
def test_pinned_fact_redirects_newer_state_and_corrects_to_dispute(graph, ledger):
    old, new = _old_and_new(
        graph, rel_type="APPLIES_TO", from_label="Decision", to_label="Term",
        from_uid="d1", old_to="t1", new_to="t1",
        old_valid_at="2026-01-01T00:00:00Z", new_valid_at="2026-06-01T00:00:00Z",
        pinned=True,
    )
    new.from_uid = "d2"
    result = resolve_text_fact(graph, ledger, new, old, "newer_state", mode="auto")
    assert result["action"] == "contradicts"  # redirected, never closed
    old_row = _edge_row(graph, old.fact_uid)
    assert old_row[0] is None  # pinned fact never closed
    disputed = graph.query(
        "MATCH (a {uid:'d1'})-[r:DISPUTED_WITH]->(b {uid:'d2'}) RETURN r"
    ).result_set
    assert len(disputed) == 1


@integration
def test_link_disputed_merge_is_idempotent(graph):
    old = OldFact(fact_uid="x", from_uid="d1", from_label="Decision", to_uid="t1",
                  to_label="Term", rel_type="APPLIES_TO", valid_at=None)
    new = NewFact(from_uid="d2", from_label="Decision", to_uid="t1", to_label="Term",
                  rel_type="APPLIES_TO", source_time="2026-01-01T00:00:00Z")
    link_disputed(graph, old, new)
    link_disputed(graph, old, new)
    rows = graph.query("MATCH (a {uid:'d1'})-[r:DISPUTED_WITH]->(b {uid:'d2'}) RETURN r").result_set
    assert len(rows) == 1  # MERGE, not duplicated


@integration
def test_link_disputed_decision_is_from_uid_not_to_uid(graph):
    # Person OWNS Decision puts Decision on the `to_uid` side (see
    # graph/ontology.py RELATION_TYPE_MAP: ("Person", "Decision"): ["OWNS"]).
    w.upsert_fact_edges(graph, "OWNS", "Person", "Decision", [{
        "from_uid": "p1", "to_uid": "d3", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    old = OldFact(fact_uid="x", from_uid="p1", from_label="Person", to_uid="d3",
                  to_label="Decision", rel_type="OWNS", valid_at=None)
    new = NewFact(from_uid="d2", from_label="Decision", to_uid="t1", to_label="Term",
                  rel_type="APPLIES_TO", source_time="2026-01-01T00:00:00Z")
    link_disputed(graph, old, new)
    rows = graph.query("MATCH (a {uid:'d3'})-[r:DISPUTED_WITH]->(b {uid:'d2'}) RETURN r").result_set
    assert len(rows) == 1


# ------------------------------------------------------------- find_conflict_candidates


@integration
def test_find_conflict_candidates_functional_relation_matches_same_subject_different_object(graph):
    w.upsert_fact_edges(graph, "ASSIGNED_TO", "WorkItem", "Person", [{
        "from_uid": "wi1", "to_uid": "p1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "system", "confidence": 1.0,
    }])
    candidates = find_conflict_candidates(graph, "wi1", "p2", "ASSIGNED_TO", "new-fact-uid")
    assert any(c.from_uid == "wi1" and c.to_uid == "p1" for c in candidates)


@integration
def test_find_conflict_candidates_non_functional_relation_ignores_same_subject_different_object(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Term", [{
        "from_uid": "p1", "to_uid": "t1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    # OWNS is not functional in graph/axioms.py -- a different object (t2)
    # with no live edge of its own must not surface p1->t1 as a candidate.
    candidates = find_conflict_candidates(graph, "p1", "t2", "OWNS", "new-fact-uid")
    assert candidates == []


@integration
def test_find_conflict_candidates_matches_same_object_different_subject_always(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Term", [{
        "from_uid": "p1", "to_uid": "t1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    candidates = find_conflict_candidates(graph, "p2", "t1", "OWNS", "new-fact-uid")
    assert any(c.from_uid == "p1" and c.to_uid == "t1" for c in candidates)


@integration
def test_find_conflict_candidates_includes_decision_applies_to_same_target(graph):
    w.upsert_fact_edges(graph, "APPLIES_TO", "Decision", "Term", [{
        "from_uid": "d1", "to_uid": "t1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    # A different relation entirely (DEFINES), targeting the same Term --
    # the live Decision APPLIES_TO edge should still surface as a candidate.
    candidates = find_conflict_candidates(graph, "d3", "t1", "DEFINES", "new-fact-uid")
    assert any(c.fact_uid and c.from_uid == "d1" and c.rel_type == "APPLIES_TO" for c in candidates)


@integration
def test_find_conflict_candidates_excludes_new_facts_own_fact_uid(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Term", [{
        "from_uid": "p1", "to_uid": "t1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    fact_uid = _fact_uid(graph, "OWNS", "p1", "t1")
    candidates = find_conflict_candidates(graph, "p2", "t1", "OWNS", fact_uid)
    assert candidates == []


@integration
def test_find_conflict_candidates_no_duplicates_across_branches(graph):
    # A Decision APPLIES_TO edge that ALSO matches the primary
    # same-object branch (rel_type == APPLIES_TO) must not be returned twice.
    w.upsert_fact_edges(graph, "APPLIES_TO", "Decision", "Term", [{
        "from_uid": "d1", "to_uid": "t1", "source_record_keys": ["rec1"],
        "evidence": "e", "extraction_method": "llm", "confidence": 0.9,
    }])
    candidates = find_conflict_candidates(graph, "d2", "t1", "APPLIES_TO", "new-fact-uid")
    matches = [c for c in candidates if c.from_uid == "d1" and c.to_uid == "t1"]
    assert len(matches) == 1
