"""Counting may widen the vocabulary. It may not widen anything else.

The loop this closes: a refused fact is recorded with its evidence and its
shape is counted, which turns "the ontology is too narrow" into a ranked list
with a cost — and then nobody reads the list. 490 facts sat refused on the
live graph because acting on them meant hand-writing SQL.

Every test here pins one of the three constraints that make closing it safe:
only the allow-list widens, every adoption is revertible, and a dismissed
shape is never re-proposed.
"""

from __future__ import annotations

import pytest

from connectors.core.ledger import ConnectorLedger, DropReason, ExtractionDrop
from graph import adoption


def _ledger(tmp_path) -> ConnectorLedger:
    return ConnectorLedger(tmp_path / "l.sqlite3")


def _refuse(ledger, record_key, chunk_id, subject, relation, obj, subject_name="A", object_name="B"):
    """Record one refused fact the way the extraction path does."""
    ledger.commit(record_key, "hash-" + chunk_id)
    ledger.save_chunks(record_key, [(chunk_id, 0, f"text for {chunk_id}")])
    ledger.record_drops(record_key, chunk_id, [ExtractionDrop(
        reason=DropReason.RELATION_NOT_ALLOWED,
        subject_kind=subject, subject_name=subject_name, relation=relation,
        object_kind=obj, object_name=object_name, detail="the sentence it came from",
    )])


# ------------------------------------------------------------ the doc bar

def test_one_document_is_not_vocabulary(tmp_path):
    """The load-bearing rule. A shape seen in a single document is that
    document's wording; the ontology feeds back into the extraction prompt,
    so adopting it turns one accident into a standing instruction."""
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")

    assert ledger.adoption_candidates(min_docs=2, min_facts=1) == []


def test_two_documents_clears_the_bar(tmp_path):
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")

    candidates = ledger.adoption_candidates(min_docs=2, min_facts=1)
    assert [(c["subject_kind"], c["relation"], c["object_kind"]) for c in candidates] == \
        [("Term", "APPLIES_TO", "System")]
    assert candidates[0]["docs"] == 2


def test_one_document_cannot_reach_the_bar_by_repeating_itself(tmp_path):
    """Distinct documents, never a sum. Utopia hit this: one document may use
    two wordings, and summing lets that single document push a shape past a
    '>= 2 documents' bar on its own."""
    ledger = _ledger(tmp_path)
    ledger.commit("notion:c:page:p1", "h")
    ledger.save_chunks("notion:c:page:p1", [("c1", 0, "t1"), ("c2", 1, "t2")])
    for chunk_id in ("c1", "c2"):
        ledger.record_drops("notion:c:page:p1", chunk_id, [ExtractionDrop(
            reason=DropReason.RELATION_NOT_ALLOWED, subject_kind="Term",
            subject_name="x", relation="APPLIES_TO", object_kind="System", object_name="y",
        )])

    assert ledger.adoption_candidates(min_docs=2, min_facts=1) == []


def test_a_tautology_is_never_a_candidate(tmp_path):
    """Found in the live data before this existed: the widest candidate by
    document count was `Document -DEFINES-> System`, and 19 of its 84 facts
    said a thing defines itself. Counting cannot tell that from a real claim."""
    ledger = _ledger(tmp_path)
    for i, key in enumerate(("notion:c:page:p1", "notion:c:page:p2", "notion:c:page:p3")):
        _refuse(ledger, key, f"c{i}", "Document", "DEFINES", "System",
                subject_name="DynamoDB", object_name="dynamodb")

    assert ledger.adoption_candidates(min_docs=2, min_facts=1) == []


def test_an_already_known_shape_is_not_re_proposed(tmp_path):
    """`Decision -APPLIES_TO-> System` is seeded. A refusal recorded against it
    (a stale row from before an earlier adoption) must not produce a duplicate
    axiom."""
    from graph.axioms import load_axioms

    ledger = _ledger(tmp_path)
    load_axioms(ledger)                      # seeds the 37 built-ins
    _refuse(ledger, "notion:c:page:p1", "c1", "Decision", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Decision", "APPLIES_TO", "System")

    assert ledger.adoption_candidates(min_docs=2, min_facts=1) == []


def test_a_dismissed_shape_is_never_re_proposed(tmp_path):
    """With adoption running unattended, re-proposing something a human
    declined is the system overruling them — the exact failure `dismissed_at`
    exists to prevent, and it must hold at any count."""
    ledger = _ledger(tmp_path)
    for i in range(6):
        _refuse(ledger, f"notion:c:page:p{i}", f"c{i}", "Term", "APPLIES_TO", "System")
    assert ledger.adoption_candidates(min_docs=2, min_facts=1)      # eligible first

    ledger.record_miss("relation_type", "Term -APPLIES_TO-> System")
    ledger.dismiss_miss("relation_type", "Term -APPLIES_TO-> System")

    assert ledger.adoption_candidates(min_docs=2, min_facts=1) == []


# ------------------------------------------------- only the allow-list widens

def test_adoption_widens_the_allow_list_and_nothing_else(tmp_path):
    """Frequency says a shape is common. It says nothing about cardinality,
    transitivity or symmetry. Utopia's carve-out is the sharp end: a wrong
    `functional` makes the temporal engine auto-close facts, and by the time
    it is noticed those closures are a chain of supersedes."""
    from graph.axioms import load_axioms

    ledger = _ledger(tmp_path)
    load_axioms(ledger)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")

    result = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True)
    assert result.adopted

    axioms = load_axioms(ledger)
    adopted = [a for a in axioms.axioms
               if (a.subject_kind, a.relation, a.object_kind) == ("Term", "APPLIES_TO", "System")]
    assert len(adopted) == 1
    axiom = adopted[0]
    assert axiom.extractable is True            # the one thing that changes
    assert axiom.functional is False
    assert axiom.is_transitive is False
    assert axiom.is_symmetric is False
    assert axiom.is_asymmetric is False
    assert axiom.temporal == "state"
    assert axiom.adopted_batch == result.batch_id


def test_adoption_makes_the_refused_triple_allowed(tmp_path):
    from graph.axioms import load_axioms

    ledger = _ledger(tmp_path)
    load_axioms(ledger)
    assert load_axioms(ledger).is_allowed("Term", "APPLIES_TO", "System") is False

    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    adoption.adopt(ledger, min_docs=2, min_facts=1, force=True)

    assert load_axioms(ledger).is_allowed("Term", "APPLIES_TO", "System") is True


def test_adoption_reopens_only_the_chunks_it_frees(tmp_path):
    """Adoption alone changes nothing — those facts were refused at extraction
    time and the graph never saw them. Re-opening every chunk would throw away
    the whole point of the content diff."""
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    # an untouched record with nothing to gain
    ledger.commit("notion:c:page:p9", "h9")
    ledger.save_chunks("notion:c:page:p9", [("c9", 0, "unrelated")])
    ledger.commit_chunk("notion:c:page:p1", "c1")
    ledger.commit_chunk("notion:c:page:p2", "c2")
    ledger.commit_chunk("notion:c:page:p9", "c9")

    result = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True)
    assert result.chunks_requeued == 2

    pending = {c.chunk_id for c in ledger.pending_chunks(limit=10)}
    assert pending == {"c1", "c2"}


# -------------------------------------------------------------- the switch

def test_the_switch_is_off_by_default_and_blocks_unattended_adoption(tmp_path):
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")

    assert adoption.auto_extend_enabled(ledger) is False
    result = adoption.adopt(ledger, min_docs=2, min_facts=1)
    assert not result.adopted
    assert result.skipped_reason == "auto_extend_ontology is off"


def test_the_switch_survives_a_reopen(tmp_path):
    ledger = _ledger(tmp_path)
    adoption.set_auto_extend(ledger, True)
    assert adoption.auto_extend_enabled(ConnectorLedger(tmp_path / "l.sqlite3")) is True
    adoption.set_auto_extend(ledger, False)
    assert adoption.auto_extend_enabled(ConnectorLedger(tmp_path / "l.sqlite3")) is False


def test_force_adopts_with_the_switch_off(tmp_path):
    """The explicit button. The switch governs the unattended path only."""
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")

    assert adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).adopted


# ----------------------------------------------------------------- the undo

def test_unadopt_removes_the_axioms(tmp_path):
    from graph.axioms import load_axioms

    ledger = _ledger(tmp_path)
    load_axioms(ledger)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    batch = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).batch_id

    assert ledger.unadopt(batch)
    assert load_axioms(ledger).is_allowed("Term", "APPLIES_TO", "System") is False


def test_unadopting_twice_is_not_an_error_and_does_nothing_the_second_time(tmp_path):
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    batch = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).batch_id

    assert ledger.unadopt(batch)
    assert ledger.unadopt(batch) == []


def test_an_undone_batch_leaves_the_seeded_vocabulary_alone(tmp_path):
    """An undo must never reach past what its own adoption created."""
    from graph.axioms import load_axioms, seed_axioms

    ledger = _ledger(tmp_path)
    load_axioms(ledger)
    seeded = len(seed_axioms())
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    batch = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).batch_id
    assert len(load_axioms(ledger).axioms) == seeded + 1

    ledger.unadopt(batch)
    assert len(load_axioms(ledger).axioms) == seeded


def test_adoptions_are_listed_and_hide_undone_ones_by_default(tmp_path):
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    batch = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).batch_id

    assert [a["batch_id"] for a in ledger.adoptions()] == [batch]
    ledger.unadopt(batch)
    assert ledger.adoptions() == []
    assert [a["batch_id"] for a in ledger.adoptions(include_undone=True)] == [batch]


# ---------------------------------------------------------------- reporting

def test_the_report_separates_eligible_from_everything_refused(tmp_path):
    """What the UI shows after a sync: not just what would be adopted, but how
    much is being left out either way."""
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p3", "c3", "Term", "OWNS", "Commit")   # one doc only

    report = adoption.pending_report(ledger, min_docs=2, min_facts=1)
    assert report["eligible_shapes"] == 1
    assert report["eligible_facts"] == 2
    assert report["total_shapes"] == 2
    assert report["total_facts"] == 3
    assert report["auto_extend"] is False


def test_nothing_eligible_is_reported_as_a_reason_not_an_error(tmp_path):
    ledger = _ledger(tmp_path)
    result = adoption.adopt(ledger, force=True)
    assert not result.adopted
    assert result.skipped_reason == "no shape clears the threshold"


# ------------------------------------------------- the undo, against a graph

class _FakeGraph:
    """Records the delete query and reports how many edges it removed."""

    def __init__(self, stamped: dict[str, int]):
        self.stamped = stamped
        self.queries: list[tuple[str, dict]] = []

    def query(self, cypher, params=None):
        self.queries.append((cypher, params or {}))
        batch = (params or {}).get("batch")

        class _R:
            result_set = [[self.stamped.pop(batch, 0)]]
        return _R()


def test_unadopt_deletes_only_edges_carrying_its_own_batch(tmp_path):
    """The stamp is what bounds the blast radius. Edges from the seeded
    vocabulary carry NULL, so an undo can never reach past what its own
    adoption created — verified against the live graph as well: a stamped and
    an unstamped edge on the same label pair, and only the stamped one went."""
    ledger = _ledger(tmp_path)
    _refuse(ledger, "notion:c:page:p1", "c1", "Term", "APPLIES_TO", "System")
    _refuse(ledger, "notion:c:page:p2", "c2", "Term", "APPLIES_TO", "System")
    batch = adoption.adopt(ledger, min_docs=2, min_facts=1, force=True).batch_id

    graph = _FakeGraph({batch: 17})
    result = adoption.unadopt(graph, ledger, batch)

    assert result == {"batch_id": batch, "shapes": 1, "edges_removed": 17}
    cypher, params = graph.queries[0]
    assert "r.adopted_batch = $batch" in cypher and "DELETE r" in cypher
    assert params["batch"] == batch


def test_unadopting_an_unknown_batch_touches_no_edges(tmp_path):
    """Order matters: the ledger is checked first, so a bad id never reaches
    a DELETE against the graph."""
    ledger = _ledger(tmp_path)
    graph = _FakeGraph({})

    result = adoption.unadopt(graph, ledger, "not-a-batch")

    assert result["edges_removed"] == 0
    assert graph.queries == []
