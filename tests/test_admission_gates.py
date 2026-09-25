"""plan.md §3.4 "Admission gates as an explicit list".

`_write_extraction` writes a fact only after it clears, in order, the six
admission gates documented at the top of `graph/semantic_pass.py`
(`ADMISSION_GATES`). Gates 1-3 are real today (selective admission, evidence
verbatim, relation allowed/direction); gates 4-6 are documented no-op
placeholders for Phase 4 (entity resolution) and Phase 5 (temporal facts),
which do not exist in this codebase yet.

These tests do not re-test `evidence_in_chunk` or `axioms.resolve_direction`
themselves (see `test_semantic_policy.py` / `test_extraction_drops.py`) --
they test that `_write_extraction` runs the gates in the documented order,
logs which gate rejected a fact, and still writes a fact that clears all six,
exactly as before this refactor.
"""

from __future__ import annotations

import logging

import pytest

from connectors.core.ledger import ConnectorLedger, DropReason, PendingChunk, SemanticStatus
from graph.axioms import DEFAULT_AXIOMS
from graph.profiles import ExtractedFact, WorkManagementExtraction
from graph.semantic_pass import (
    ADMISSION_GATES,
    _gate_conflict_classification,
    _gate_laya_triage,
    _gate_merge_candidate,
    _gate_projection_eligibility,
    _write_extraction,
    run_semantic_pass,
)
from graph.token_usage import TokenUsage


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class _FakeGraph:
    """Just enough of the FalkorDB `Graph` surface for `_write_extraction`
    with an extraction that has no terms/decisions/systems/apis/endpoints
    (so the entity-write loop never runs) -- only the fact loop's
    `SourceRecord` lookup, semantic-endpoint existence check, and
    `upsert_fact_edges`'s single UNWIND write.
    """

    def __init__(self):
        self.queries: list[str] = []

    def query(self, cypher, params=None):
        params = params or {}
        self.queries.append(cypher)
        if "SourceRecord" in cypher:
            return _FakeResult([])  # no source_time on record -- valid_at stays None
        if "RETURN n.uid LIMIT 1" in cypher:
            # Simulate: a semantic node with this uid already exists (so the
            # fact's endpoint resolves without needing the entity-write loop).
            return _FakeResult([[params["uid"]]])
        return _FakeResult([])  # e.g. the upsert_fact_edges UNWIND write


def _extraction(*facts: ExtractedFact) -> WorkManagementExtraction:
    return WorkManagementExtraction(facts=list(facts))


def _write(ledger, chunk, extraction, *, record_own_kind="Project", primary_uid="proj-1"):
    return _write_extraction(
        _FakeGraph(), ledger, chunk, extraction, primary_uid, record_own_kind,
        client=None, embedding_model="test-embed", extraction_model="test-model",
        profile_name="work_management", token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
    )


# --------------------------------------------------------- documented order

def test_admission_gates_are_documented_in_order():
    assert ADMISSION_GATES == (
        "1:selective_admission_and_laya_triage",
        "2:evidence_verbatim",
        "3:relation_allowed_direction",
        "4:merge_candidate",
        "5:conflict_classification",
        "6:projection_eligibility",
    )


# ------------------------------------------------- gate 2: evidence verbatim

def test_gate2_rejects_before_gate3_ever_runs(tmp_path, caplog):
    """A fact that fails BOTH gate 2 (evidence not in chunk) and gate 3
    (Decision -APPLIES_TO-> Person is valid in neither direction) must be
    rejected for gate 2 only -- proving gate 2 runs first and short-circuits
    before gate 3 is evaluated at all."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "Completely unrelated chunk text.")
    fact = ExtractedFact(
        subject_name="GA", subject_kind="Decision", relation="APPLIES_TO",
        object_name="Ada", object_kind="Person", evidence="this quote is not in the chunk",
    )

    with caplog.at_level(logging.INFO, logger="neuron.semantic_pass"):
        entities, written, rejected = _write(ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 0, 1)
    assert ledger.drop_counts() == {"evidence_not_in_chunk": 1}
    assert not ledger.misses()  # gate 3's record_miss must never have run
    assert any("gate 2 (evidence_verbatim) rejected fact" in r.message for r in caplog.records)
    assert not any("gate 3" in r.message for r in caplog.records)


def test_gate2_evidence_not_in_chunk_still_produces_existing_drop_reason(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "The service uses Postgres.")
    fact = ExtractedFact(
        subject_name="X", subject_kind="Term", relation="DEFINES", object_name="Y",
        object_kind="Term", evidence="a paraphrase, not a verbatim quote",
    )

    entities, written, rejected = _write(ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 0, 1)
    only = ledger.drops(reason=DropReason.EVIDENCE_NOT_IN_CHUNK)
    assert len(only) == 1 and only[0].subject_name == "X"


# ------------------------------------------- gate 3: relation allowed/direction

def test_gate3_rejects_a_relation_valid_in_neither_direction(tmp_path, caplog):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "The doc says: GA is enabled for everyone.")
    fact = ExtractedFact(
        subject_name="GA", subject_kind="Decision", relation="APPLIES_TO",
        object_name="Ada", object_kind="Person", evidence="GA is enabled",
    )

    with caplog.at_level(logging.INFO, logger="neuron.semantic_pass"):
        entities, written, rejected = _write(ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 0, 1)
    assert ledger.drop_counts() == {"relation_not_allowed": 1}
    assert ledger.misses() == [("relation_type", "Decision -APPLIES_TO-> Person", 1, "GA -> Ada")]
    assert any("gate 3 (relation_allowed_direction) rejected fact" in r.message for r in caplog.records)


# ----------------------------------------- gates 4-6: no-op placeholders

def test_placeholder_gates_never_reject_anything():
    fact = ExtractedFact(
        subject_name="a", subject_kind="Decision", relation="APPLIES_TO",
        object_name="b", object_kind="WorkItem", evidence="irrelevant",
    )
    assert _gate_merge_candidate(fact, "uid-1", "uid-2") is None
    assert _gate_conflict_classification(fact, "uid-1", "uid-2") is None
    assert _gate_projection_eligibility(fact, "uid-1", "uid-2") is None


def test_laya_triage_placeholder_never_skips_a_chunk():
    chunk = PendingChunk("jira:c:project:proj-1", "chunk-1", 0, "some text")
    assert _gate_laya_triage(chunk) is None


# ----------------------------------- a fact clearing all 6 gates is still written

def test_fact_passing_every_gate_is_still_written(tmp_path):
    """No regression: a fact whose evidence is verbatim, whose relation is
    allowed, and whose endpoints resolve must still be written as a live
    edge exactly as before this refactor -- gates 4-6 being wired in as
    no-ops must not change that."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provides the Checkout API to downstream teams.",
    )
    fact = ExtractedFact(
        subject_name="Order Service", subject_kind="Project", relation="PROVIDES_API",
        object_name="Checkout API", object_kind="Api",
        evidence="Order Service provides the Checkout API",
    )

    entities, written, rejected = _write(
        ledger, chunk, _extraction(fact), record_own_kind="Project", primary_uid="proj-1",
    )

    assert (entities, written, rejected) == (0, 1, 0)
    assert ledger.drop_counts() == {}
    edges = ledger.edges_for_record(chunk.record_key)
    assert len(edges) == 1
    assert edges[0].rel_type == "PROVIDES_API"
    assert edges[0].from_uid == "proj-1"


# --------------------------------------------------- gate 1: selective admission

def test_gate1_selective_admission_logs_and_skips_unresolved_chunk(tmp_path, caplog):
    """A chunk whose record has no resolved `primary_node_uid` never reaches
    `_call_llm` -- existing behaviour, now logged with the gate name."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    record_key = "jira:c:project:proj-2"
    ledger.commit(record_key, "hash-1", primary_node_uid=None, semantic_status=SemanticStatus.PENDING)
    ledger.save_chunks(record_key, [("chunk-1", 0, "some text")])

    with caplog.at_level(logging.WARNING, logger="neuron.semantic_pass"):
        result = run_semantic_pass(graph=object(), ledger=ledger, client=object(), model="test-model")

    assert result.chunks_processed == 0
    assert any(
        "gate 1 (selective_admission_and_laya_triage) skipped chunk" in r.message
        for r in caplog.records
    )
    # Still pending -- gate 1 skipping is not a drop, it's a retry-later skip.
    assert len(ledger.pending_chunks(10)) == 1
