"""25-plan.md Phase 5 §5.0/§5.6/§5.7/§5.8 — writer-contract primitives.

Same convention as tests/test_graph_integration.py: a real FalkorDB via
`NEURON_INTEGRATION=1`, one throwaway graph per test, deleted in `finally`.
These tests exercise `graph/writer.py` directly (no resolver/embedding path),
so they don't need OpenAI credentials the way test_graph_integration.py's
ACL/SAME_AS scenario does.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from graph import writer as w
from graph.falkor_client import build_client
from graph.fact_predicates import LIVE_FACT_CYPHER, is_live_fact
from graph.time_axis import held_at, holds_at, parse_iso

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_{uuid4().hex}")
    w.upsert_entities(g, "Person", [{"uid": "p1", "props": {"name": "Alice"}}])
    w.upsert_entities(g, "Person", [{"uid": "p2", "props": {"name": "Bob"}}])
    w.upsert_entities(g, "Person", [{"uid": "p3", "props": {"name": "Carol"}}])
    try:
        yield g
    finally:
        g.delete()


def _edge(graph, rel_type="OWNS", from_uid="p1", to_uid="p2"):
    row = graph.query(
        f"MATCH (a {{uid: $from_uid}})-[r:{rel_type}]->(b {{uid: $to_uid}}) "
        "RETURN r.fact_uid, r.invalid_at, r.last_confirmed_at, r.valid_at, "
        "r.source_record_keys, r.pinned, r.assertion_status, r.projection_status",
        params={"from_uid": from_uid, "to_uid": to_uid},
    ).result_set
    return row[0] if row else None


# --------------------------------------------------------------------- §5.0.1


def test_revive_true_reopens_a_closed_edge_default_behavior(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    w.close_fact(graph, fact_uid, "2026-02-01T00:00:00Z")
    assert _edge(graph)[1] == "2026-02-01T00:00:00Z"

    # default revive=True: reconfirming revives it, matching every existing
    # caller's expectation (an unchanged Jira assignee stays live).
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    assert _edge(graph)[1] is None


def test_revive_false_cannot_reopen_a_closed_edge(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    w.close_fact(graph, fact_uid, "2026-02-01T00:00:00Z")

    # a text-fact-style write (revive=False) re-asserting the identical
    # triple must NOT un-close it, but should still attach provenance.
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec3"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }], revive=False)
    row = _edge(graph)
    assert row[1] == "2026-02-01T00:00:00Z"  # invalid_at untouched
    assert row[3] == "2026-01-01T00:00:00Z"  # valid_at (world interval) untouched
    assert row[4] == ["rec1", "rec3"]  # provenance still accumulates


def test_revive_false_on_a_still_live_edge_behaves_like_a_normal_confirm(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }], revive=False)
    row = _edge(graph)
    assert row[1] is None
    assert row[4] == ["rec1", "rec2"]


# --------------------------------------------------------------------- §5.0.2


def test_close_fact_affects_exactly_one_fact(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p3", "source_record_keys": ["rec2"],
        "evidence": "e2", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid_p2 = _edge(graph, to_uid="p2")[0]

    w.close_fact(graph, fact_uid_p2, "2026-03-01T00:00:00Z")

    assert _edge(graph, to_uid="p2")[1] == "2026-03-01T00:00:00Z"
    assert _edge(graph, to_uid="p3")[1] is None  # the other live fact is untouched


def test_close_fact_uses_the_supplied_world_time_not_now(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    world_time = "2030-07-04T00:00:00Z"  # deliberately far from wall-clock now()
    w.close_fact(graph, fact_uid, world_time)
    assert _edge(graph)[1] == world_time


def test_close_fact_archives_history_with_close_reason(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    w.close_fact(graph, fact_uid, "2026-02-01T00:00:00Z", reason="newer_state")
    rows = graph.query(
        "MATCH (h:FactHistory) WHERE h.fact_uid = $fu "
        "RETURN h.close_reason, h.valid_from, h.valid_to",
        params={"fu": fact_uid},
    ).result_set
    assert len(rows) == 1
    assert rows[0][0] == "newer_state"
    assert rows[0][1] == "2026-01-01T00:00:00Z"
    assert rows[0][2] == "2026-02-01T00:00:00Z"


def test_close_fact_missing_fact_uid_raises(graph):
    with pytest.raises(ValueError):
        w.close_fact(graph, "does-not-exist", "2026-02-01T00:00:00Z")


def test_close_fact_ambiguous_fact_uid_raises(graph):
    # Simulate the "should not happen" ambiguous case directly: two live
    # edges deliberately stamped with the same fact_uid.
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p3", "source_record_keys": ["rec2"],
        "evidence": "e2", "extraction_method": "llm", "confidence": 0.9,
    }])
    shared_uid = "duplicate-fact-uid"
    graph.query(
        "MATCH (a {uid:'p1'})-[r:OWNS]->(b) SET r.fact_uid = $fu",
        params={"fu": shared_uid},
    )
    with pytest.raises(ValueError):
        w.close_fact(graph, shared_uid, "2026-02-01T00:00:00Z")


def test_close_fact_rejects_valid_to_before_valid_at(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-06-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    with pytest.raises(ValueError):
        w.close_fact(graph, fact_uid, "2026-01-01T00:00:00Z")


# --------------------------------------------------------------------- §5.0.3


def test_correct_fact_disappears_from_world_time_reads_via_predicate(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "limit is 100", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    assert _edge(graph)[6] == "live"  # assertion_status before correction

    w.correct_fact(graph, fact_uid, corrected_by="new-fact-uid", observed_to="2027-01-01T00:00:00Z")

    row = graph.query(
        "MATCH (a)-[r:OWNS]->(b) WHERE r.fact_uid = $fu "
        "RETURN r.invalid_at, r.assertion_status, r.projection_status, r.corrected_by, "
        "r.correction_observed_at, r.metadata_revised_at",
        params={"fu": fact_uid},
    ).result_set[0]
    invalid_at, assertion_status, projection_status, corrected_by, observed_at, revised_at = row
    assert assertion_status == "corrected"
    assert corrected_by == "new-fact-uid"
    assert observed_at == "2027-01-01T00:00:00Z"
    assert revised_at is not None

    # The centralized predicate (§5.0.6) is the thing later readers should
    # adopt -- prove it excludes a corrected fact, both the Python mirror...
    assert is_live_fact({
        "invalid_at": invalid_at, "assertion_status": assertion_status,
        "projection_status": projection_status,
    }) is False
    # ...and the Cypher fragment itself, run for real.
    cypher_live = graph.query(
        f"MATCH (a)-[r:OWNS]->(b) WHERE r.fact_uid = $fu AND {LIVE_FACT_CYPHER} RETURN r",
        params={"fu": fact_uid},
    ).result_set
    assert cypher_live == []


def test_correct_fact_distinct_from_close_fact_storage(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    w.close_fact(graph, fact_uid, "2026-02-01T00:00:00Z")
    row = graph.query(
        "MATCH (a)-[r:OWNS]->(b) WHERE r.fact_uid = $fu "
        "RETURN r.assertion_status, r.corrected_by",
        params={"fu": fact_uid},
    ).result_set[0]
    assert (row[0] or "live") != "corrected"
    assert row[1] is None


def test_correct_fact_remains_visible_in_record_time_history_until_observed_to(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    observed_to = "2027-01-01T00:00:00Z"
    w.correct_fact(graph, fact_uid, corrected_by="new-fact-uid", observed_to=observed_to)

    hist = graph.query(
        "MATCH (h:FactHistory) WHERE h.fact_uid = $fu "
        "RETURN h.observed_from, h.observed_to, h.assertion_status, h.corrected_by, "
        "h.correction_observed_at",
        params={"fu": fact_uid},
    ).result_set
    assert len(hist) == 1
    observed_from, observed_to_stored, status, corrected_by, correction_observed_at = hist[0]
    assert status == "corrected"
    assert corrected_by == "new-fact-uid"
    assert correction_observed_at == observed_to
    assert observed_to_stored == observed_to

    # `observed_from` is stamped at correction time (now), so the window to
    # probe is (now, observed_to), not the fact's own (now-irrelevant)
    # world-time `valid_at`.
    inside_window = parse_iso("2026-11-01T00:00:00Z")
    after_window = parse_iso("2027-06-01T00:00:00Z")
    assert held_at(observed_from, observed_to_stored, inside_window) is True
    assert held_at(observed_from, observed_to_stored, after_window) is False


def test_correct_fact_missing_fact_uid_raises(graph):
    with pytest.raises(ValueError):
        w.correct_fact(graph, "does-not-exist", corrected_by="x", observed_to="2027-01-01T00:00:00Z")


# --------------------------------------------------------------------- §5.0.5


def test_attested_from_always_parses_as_iso_round_trip(graph):
    iso_value = "2026-05-12T00:00:00+00:00"
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "attested_from": iso_value,
    }])
    stored = graph.query(
        "MATCH (a)-[r:OWNS]->(b) RETURN r.attested_from"
    ).result_set[0][0]
    assert parse_iso(stored) is not None
    assert parse_iso(stored) == parse_iso(iso_value)


def test_attested_by_record_stored_separately_never_a_date_field(graph):
    record_key = "jira:PROJ-123"
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "attested_from": "2026-05-12T00:00:00+00:00",
        "attested_by_record": record_key,
    }])
    attested_from, attested_by_record = graph.query(
        "MATCH (a)-[r:OWNS]->(b) RETURN r.attested_from, r.attested_by_record"
    ).result_set[0]
    assert attested_by_record == record_key
    assert attested_from != record_key
    assert parse_iso(attested_from) is not None
    with pytest.raises(ValueError):
        parse_iso(attested_by_record)  # a record key is never a parseable date


# --------------------------------------------------------------------- §5.6


def test_last_confirmed_at_not_bumped_on_unchanged_resync(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    before = _edge(graph)[2]
    # identical evidence/confidence, same (only) source_record_key -> KEEP sync
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    after = _edge(graph)[2]
    assert after == before


def test_last_confirmed_at_bumps_on_content_changing_write(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    before = _edge(graph)[2]
    # a genuinely new supporting record -> content-changing
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    after = _edge(graph)[2]
    assert after != before

    # a changed confidence value is also content-changing
    before2 = after
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.42,
    }])
    after2 = _edge(graph)[2]
    assert after2 != before2


def test_last_confirmed_at_bumps_when_revive_reopens_a_closed_edge(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    w.close_fact(graph, fact_uid, "2026-02-01T00:00:00Z")
    before = _edge(graph)[2]
    # identical content, but revive=True reopens a closed edge -> that
    # reopening itself is content-changing, even though evidence/confidence
    # /keys are unchanged.
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    after = _edge(graph)[2]
    assert after != before


def test_touch_metadata_sets_metadata_revised_at_distinct_from_other_clocks(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    fact_uid = _edge(graph)[0]
    last_confirmed_before = _edge(graph)[2]
    w.touch_metadata(graph, fact_uid, at="2026-08-01T00:00:00Z")
    row = graph.query(
        "MATCH (a)-[r:OWNS]->(b) WHERE r.fact_uid=$fu "
        "RETURN r.metadata_revised_at, r.last_confirmed_at",
        params={"fu": fact_uid},
    ).result_set[0]
    assert row[0] == "2026-08-01T00:00:00Z"
    assert row[1] == last_confirmed_before  # touching metadata never moves last_confirmed_at


# --------------------------------------------------------------------- §5.7


def test_reinforce_count_pure_function():
    assert w.reinforce_count(None) == 0
    assert w.reinforce_count([]) == 0
    assert w.reinforce_count(["a"]) == 1
    assert w.reinforce_count(["a", "b", "c"]) == 3


def test_reinforce_count_matches_stored_source_record_keys(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    keys = _edge(graph)[4]
    assert w.reinforce_count(keys) == 2


# --------------------------------------------------------------------- §5.8


def test_pinned_persists_across_unrelated_rewrite(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "pinned": True,
    }])
    assert _edge(graph)[5] is True

    # an unrelated re-write (no `pinned` field at all) must not reset it
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    assert _edge(graph)[5] is True


def test_pinned_defaults_false_when_never_set(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    assert _edge(graph)[5] is False


def test_decay_class_is_writable_and_optional(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    decay = graph.query("MATCH (a)-[r:OWNS]->(b) RETURN r.decay_class").result_set[0][0]
    assert decay is None  # no policy caller exists yet -- schema-readiness only

    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec2"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "decay_class": "fast",
    }])
    decay = graph.query("MATCH (a)-[r:OWNS]->(b) RETURN r.decay_class").result_set[0][0]
    assert decay == "fast"


# --------------------------------------------------------------------- §5.0.6


def test_new_fact_edges_default_to_live_assertion_and_projection_status(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    row = _edge(graph)
    assert row[6] == "live"
    assert row[7] == "live"


def test_is_live_fact_treats_missing_properties_as_live():
    # An edge written before this concept existed has neither property set.
    assert is_live_fact({"invalid_at": None}) is True
    assert is_live_fact({"invalid_at": "2026-01-01T00:00:00Z"}) is False
    assert is_live_fact({"invalid_at": None, "assertion_status": "corrected"}) is False
    assert is_live_fact({"invalid_at": None, "projection_status": "pending_review"}) is False


def test_projection_status_pending_review_excluded_by_predicate(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "low confidence guess", "extraction_method": "llm", "confidence": 0.2,
        "projection_status": "pending_review",
    }])
    cypher_live = graph.query(
        f"MATCH (a)-[r:OWNS]->(b) WHERE {LIVE_FACT_CYPHER} RETURN r"
    ).result_set
    assert cypher_live == []


# --------------------------------------------------------------------- §5.0.1 confirm_fact


def test_confirm_fact_does_not_bump_on_identical_restatement(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    fact_uid, _, before, *_ = _edge(graph)
    w.confirm_fact(graph, fact_uid, at="2026-05-01T00:00:00Z", source_record_key="rec1")
    after = _edge(graph)[2]
    assert after == before


def test_confirm_fact_bumps_on_new_supporting_record(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
    }])
    fact_uid = _edge(graph)[0]
    w.confirm_fact(graph, fact_uid, at="2026-05-01T00:00:00Z", source_record_key="rec2")
    row = _edge(graph)
    assert row[2] == "2026-05-01T00:00:00Z"
    assert row[4] == ["rec1", "rec2"]
    # confirm_fact must never touch valid_at/invalid_at
    assert row[1] is None


def test_confirm_fact_never_touches_temporal_fields_even_when_content_changes(graph):
    w.upsert_fact_edges(graph, "OWNS", "Person", "Person", [{
        "from_uid": "p1", "to_uid": "p2", "source_record_keys": ["rec1"],
        "evidence": "e1", "extraction_method": "llm", "confidence": 0.9,
        "valid_at": "2026-01-01T00:00:00Z",
    }])
    fact_uid = _edge(graph)[0]
    before_valid_at = _edge(graph)[3]
    w.confirm_fact(graph, fact_uid, at="2026-05-01T00:00:00Z", source_record_key="rec2")
    row = _edge(graph)
    assert row[3] == before_valid_at
    assert row[1] is None


def test_confirm_fact_missing_fact_uid_raises(graph):
    with pytest.raises(ValueError):
        w.confirm_fact(graph, "does-not-exist", source_record_key="rec1")
