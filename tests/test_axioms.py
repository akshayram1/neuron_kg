"""The ontology as data.

The load-bearing property here is that moving the vocabulary out of a Python
dict changed NOTHING about what an extraction is allowed to assert. Adding
the structural relations widens what the graph can reason about; it must not
widen what the model can claim.
"""

from __future__ import annotations

import itertools

from connectors.core.ledger import ConnectorLedger
from graph.axioms import AS_IS, SWAPPED, AxiomSet, DEFAULT_AXIOMS, RelationAxiom, load_axioms, seed_axioms
from graph.ontology import RELATION_TYPE_MAP, is_relation_allowed

_KINDS = sorted(
    {kind for pair in RELATION_TYPE_MAP for kind in pair}
    | {"Person", "WorkItem", "Project", "Repository", "SourceFile", "Commit",
       "PullRequest", "Document", "Workspace", "Decision", "Term", "System",
       "Api", "Endpoint"}
)
_RELATIONS = sorted(
    {relation for relations in RELATION_TYPE_MAP.values() for relation in relations}
    | {"ASSIGNED_TO", "PARENT_OF", "CONTAINS", "MODIFIES", "SAME_AS",
       "AUTHORED_BY", "IMPLEMENTS", "BELONGS_TO", "REPORTED_BY", "BLOCKS", "DOCUMENTS",
       "PROVIDES_API", "CONSUMES_API", "EXPOSES_ENDPOINT", "CALLS_ENDPOINT",
       "CHANGES", "DEPRECATES", "MIGRATES_TO"}
)


def test_the_axiom_store_answers_exactly_as_the_old_dict_did():
    """Exhaustive, not sampled: every triple over every known kind and
    relation. This is the whole safety argument for the migration."""
    for subject, relation, obj in itertools.product(_KINDS, _RELATIONS, _KINDS):
        assert DEFAULT_AXIOMS.is_allowed(subject, relation, obj) == \
            is_relation_allowed(subject, relation, obj), (subject, relation, obj)


def test_structural_relations_are_known_but_not_extractable():
    """`ASSIGNED_TO` is read from a Jira field, never inferred from prose. It
    is in the store for its cardinality axiom, not to let the model assert
    it."""
    assert DEFAULT_AXIOMS.is_allowed("WorkItem", "ASSIGNED_TO", "Person") is False
    assert any(
        a.relation == "ASSIGNED_TO" and a.functional for a in DEFAULT_AXIOMS.axioms
    )


def test_api_relations_have_strict_endpoint_shapes():
    assert DEFAULT_AXIOMS.is_allowed("Project", "CONSUMES_API", "Api")
    assert DEFAULT_AXIOMS.is_allowed("Api", "EXPOSES_ENDPOINT", "Endpoint")
    assert DEFAULT_AXIOMS.is_allowed("SourceFile", "CALLS_ENDPOINT", "Endpoint")
    assert not DEFAULT_AXIOMS.is_allowed("SourceFile", "CALLS_ENDPOINT", "Api")


def test_axioms_the_old_dict_could_not_express():
    assert "PARENT_OF" in DEFAULT_AXIOMS.transitive_relations()
    assert "SAME_AS" in DEFAULT_AXIOMS.symmetric_relations()
    assert "SAME_AS" in DEFAULT_AXIOMS.transitive_relations()
    assert "ASSIGNED_TO" in DEFAULT_AXIOMS.functional_relations()
    # An event happened once; it has no interval to be superseded over.
    assert [a.temporal for a in DEFAULT_AXIOMS.for_relation("MODIFIES")] == ["event"]


def test_disputed_with_is_structural_and_symmetric_but_not_transitive():
    """plan.md §5.4: DISPUTED_WITH is a structural axiom -- symmetric like
    SAME_AS (A disputes B implies B disputes A) but, unlike SAME_AS, NOT
    transitive (A vs B and B vs C says nothing about A vs C)."""
    disputed = DEFAULT_AXIOMS.for_relation("DISPUTED_WITH")
    assert len(disputed) == 1
    axiom = disputed[0]
    assert axiom.subject_kind == "Decision"
    assert axiom.object_kind == "Decision"
    assert axiom.is_symmetric is True
    assert axiom.extractable is False
    assert axiom.is_transitive is False

    assert "DISPUTED_WITH" in DEFAULT_AXIOMS.symmetric_relations()
    assert "DISPUTED_WITH" not in DEFAULT_AXIOMS.transitive_relations()


def test_disputed_with_is_never_llm_extractable():
    """The single most important behavior: an extraction must never be able
    to assert a dispute directly -- it is written only by resolve_text_fact
    and Phase 6.3 (plan.md §5.4)."""
    assert DEFAULT_AXIOMS.is_allowed("Decision", "DISPUTED_WITH", "Decision") is False
    assert DEFAULT_AXIOMS.resolve_direction("Decision", "DISPUTED_WITH", "Decision") is None
    # Also unreachable through the ontology's own extraction allow-list.
    assert is_relation_allowed("Decision", "DISPUTED_WITH", "Decision") is False


def test_disputed_with_is_in_seed_axioms():
    seeded = [a for a in seed_axioms() if a.relation == "DISPUTED_WITH"]
    assert len(seeded) == 1
    assert seeded[0].subject_kind == "Decision"
    assert seeded[0].object_kind == "Decision"
    assert seeded[0].is_symmetric is True
    assert seeded[0].extractable is False


def test_direction_resolution_reads_from_the_store():
    assert DEFAULT_AXIOMS.resolve_direction("Decision", "APPLIES_TO", "System") == AS_IS
    assert DEFAULT_AXIOMS.resolve_direction("System", "APPLIES_TO", "Decision") == SWAPPED
    assert DEFAULT_AXIOMS.resolve_direction("Decision", "APPLIES_TO", "Person") is None


def test_a_narrower_store_really_does_narrow_extraction():
    """Proves the allow-list is data now: swap the rows, the answer changes."""
    tiny = AxiomSet.build([RelationAxiom("DEFINES", "Document", "Term", extractable=True)])
    assert tiny.is_allowed("Document", "DEFINES", "Term") is True
    assert tiny.is_allowed("Decision", "APPLIES_TO", "System") is False


# ------------------------------------------------------------------- storage

def test_axioms_round_trip_through_the_ledger(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    loaded = load_axioms(ledger)

    for subject, relation, obj in itertools.product(_KINDS[:6], _RELATIONS, _KINDS[:6]):
        assert loaded.is_allowed(subject, relation, obj) == \
            is_relation_allowed(subject, relation, obj), (subject, relation, obj)
    assert "PARENT_OF" in loaded.transitive_relations()


def test_seeding_never_overwrites_an_edit(tmp_path):
    """The table exists so it can be edited. A redeploy silently reverting a
    deliberate change is the exact failure it was created to prevent."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    load_axioms(ledger)

    import sqlite3
    with sqlite3.connect(ledger.path) as db:
        db.execute(
            "UPDATE relation_axioms SET extractable = 0 "
            "WHERE relation = 'APPLIES_TO' AND subject_kind = 'Decision' AND object_kind = 'System'"
        )

    reloaded = load_axioms(ledger)          # seeds again; must not resurrect the row
    assert reloaded.is_allowed("Decision", "APPLIES_TO", "System") is False


def test_seeding_is_idempotent(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    first = len(load_axioms(ledger).axioms)
    second = len(load_axioms(ledger).axioms)
    assert first == second > 0


# -------------------------------------------------------------------- misses

def test_an_out_of_vocabulary_term_is_counted_not_discarded(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_miss("relation_type", "Decision -CAUSES-> System", example="x -> y")
    ledger.record_miss("relation_type", "Decision -CAUSES-> System", example="a -> b")

    misses = ledger.misses()
    assert misses == [("relation_type", "Decision -CAUSES-> System", 2, "x -> y")]


def test_dismissing_a_miss_is_a_flag_so_the_answer_survives_the_next_run(tmp_path):
    """Utopia shipped this as a DELETE first, and the next extraction simply
    re-inserted the term -- "the user's no did not survive one round"."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_miss("relation_type", "Decision -CAUSES-> System")
    ledger.dismiss_miss("relation_type", "Decision -CAUSES-> System")
    assert ledger.misses() == []

    ledger.record_miss("relation_type", "Decision -CAUSES-> System")   # seen again

    assert ledger.misses() == []                       # still dismissed
    assert len(ledger.misses(include_dismissed=True)) == 1
    assert ledger.misses(include_dismissed=True)[0][2] == 2   # but still counted
