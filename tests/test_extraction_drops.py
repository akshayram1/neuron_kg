"""Nothing an extraction discards may vanish without a reason on record.

Before this, `_write_extraction` had five paths that threw an item away:
three bumped a counter, two were completely silent, and none of them
outlived the log line. A count with no reason cannot distinguish "the
ontology is too narrow" from "the model is wrong", which is the only
question worth asking about a drop.
"""

from __future__ import annotations

from connectors.core.ledger import ConnectorLedger, DropReason, ExtractionDrop
from graph.ontology import AS_IS, SWAPPED, resolve_direction


# ----------------------------------------------------------------- direction

def test_a_correctly_stated_fact_is_left_alone():
    assert resolve_direction("Decision", "APPLIES_TO", "System") == AS_IS


def test_a_reversed_fact_is_salvaged_by_swapping_not_dropped():
    """The ontology declares APPLIES_TO as Decision -> System. English lets
    the model state it the other way round just as naturally, and that is a
    direction error, not a false statement."""
    assert resolve_direction("System", "APPLIES_TO", "Decision") == SWAPPED


def test_owns_is_salvaged_the_same_way():
    assert resolve_direction("Person", "OWNS", "Term") == AS_IS
    assert resolve_direction("Term", "OWNS", "Person") == SWAPPED


def test_a_fact_valid_in_neither_direction_stays_rejected():
    """Salvaging must not become "accept anything": if no declared pair
    carries this relation, the ontology genuinely does not model it."""
    assert resolve_direction("Decision", "APPLIES_TO", "Person") is None
    assert resolve_direction("Term", "DECIDED_BY", "Term") is None


def test_swapping_is_not_symmetric_for_self_pairs():
    # Decision SUPERSEDES Decision is declared; both directions are the same
    # pair, so it resolves as_is and never reports a correction.
    assert resolve_direction("Decision", "SUPERSEDES", "Decision") == AS_IS


# --------------------------------------------------------------------- drops

def test_drops_are_persisted_with_their_reason(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_drops("notion:c:page:p1", "chunk-1", [
        ExtractionDrop(reason=DropReason.RELATION_NOT_ALLOWED, subject_kind="Decision",
                       subject_name="use Redis", relation="APPLIES_TO",
                       object_kind="Person", object_name="Ada"),
        ExtractionDrop(reason=DropReason.EVIDENCE_NOT_IN_CHUNK, subject_kind="Term",
                       subject_name="blast radius", detail="paraphrased quote"),
    ])

    assert ledger.drop_counts() == {"relation_not_allowed": 1, "evidence_not_in_chunk": 1}
    only = ledger.drops(reason=DropReason.RELATION_NOT_ALLOWED)
    assert len(only) == 1
    assert only[0].subject_name == "use Redis" and only[0].object_name == "Ada"


def test_re_extracting_a_chunk_replaces_its_drops(tmp_path):
    """A chunk re-extracted after an ontology change must not leave behind
    the drops from a rule that no longer exists."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    key, chunk = "notion:c:page:p1", "chunk-1"
    ledger.record_drops(key, chunk, [ExtractionDrop(reason=DropReason.RELATION_NOT_ALLOWED)])
    assert ledger.drop_counts() == {"relation_not_allowed": 1}

    ledger.record_drops(key, chunk, [])          # the rule now allows it

    assert ledger.drop_counts() == {}


def test_drops_from_other_chunks_are_untouched(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_drops("notion:c:page:p1", "chunk-1",
                        [ExtractionDrop(reason=DropReason.RELATION_NOT_ALLOWED)])
    ledger.record_drops("notion:c:page:p1", "chunk-2",
                        [ExtractionDrop(reason=DropReason.ENDPOINT_UNRESOLVED)])

    ledger.record_drops("notion:c:page:p1", "chunk-1", [])

    assert ledger.drop_counts() == {"endpoint_unresolved": 1}


def test_direction_corrected_is_recorded_even_though_the_fact_was_written(tmp_path):
    """It is in the drop vocabulary precisely so a correction can never be
    silent -- it is a trace, not a loss."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_drops("notion:c:page:p1", "chunk-1", [
        ExtractionDrop(reason=DropReason.DIRECTION_CORRECTED, subject_kind="System",
                       subject_name="Redis", relation="APPLIES_TO",
                       object_kind="Decision", object_name="use Redis",
                       detail="written as (Decision) -APPLIES_TO-> (System)"),
    ])
    assert ledger.drop_counts() == {"direction_corrected": 1}
    assert "written as" in (ledger.drops()[0].detail or "")


def test_drop_counts_can_be_scoped_to_one_provider(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_drops("notion:c:page:p1", "a", [ExtractionDrop(reason=DropReason.RELATION_NOT_ALLOWED)])
    ledger.record_drops("jira:c:work_item:w1", "b", [ExtractionDrop(reason=DropReason.ENDPOINT_UNRESOLVED)])

    assert ledger.drop_counts(record_prefix="notion:") == {"relation_not_allowed": 1}
    assert ledger.drop_counts(record_prefix="jira:") == {"endpoint_unresolved": 1}
