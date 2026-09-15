"""Axiom-driven derivation. Pure functions, no graph, no LLM.

Every test here pins a safety constraint rather than a feature: a wrong
axiom does not produce one wrong edge, it multiplies into thousands.
"""

from __future__ import annotations

from graph.axioms import AxiomSet, RelationAxiom
from graph.inference import (
    Edge, intersect_validity, min_confidence, symmetric_closure,
    inverse_edges, sub_property_edges, transitive_closure,
)


def e(a: str, b: str, **kw) -> Edge:
    kw.setdefault("from_label", "WorkItem")
    kw.setdefault("to_label", "WorkItem")
    kw.setdefault("fact_uid", f"fact-{a}{b}")
    return Edge(from_uid=a, to_uid=b, **kw)


# --------------------------------------------------------------- transitivity

def test_transitivity_closes_a_chain():
    derived, capped = transitive_closure("PARENT_OF", [e("a", "b"), e("b", "c")])
    assert [(d.from_uid, d.to_uid) for d in derived] == [("a", "c")]
    assert capped is False


def test_transitivity_reaches_further_than_one_hop():
    derived, _ = transitive_closure("PARENT_OF", [e("a", "b"), e("b", "c"), e("c", "d")])
    pairs = {(d.from_uid, d.to_uid) for d in derived}
    assert pairs == {("a", "c"), ("a", "d"), ("b", "d")}


def test_an_already_asserted_edge_is_never_derived():
    """Asserted always wins, so "who said this" has exactly one answer."""
    edges = [e("a", "b"), e("b", "c"), e("a", "c")]      # a->c asserted too
    derived, _ = transitive_closure("PARENT_OF", edges)
    assert derived == []


def test_a_cycle_never_derives_a_self_loop():
    derived, _ = transitive_closure("PARENT_OF", [e("a", "b"), e("b", "a")])
    assert all(d.from_uid != d.to_uid for d in derived)


def test_hitting_the_cap_is_reported_not_silent():
    """"Derived fewer" and "nothing satisfies the rule" must not look the
    same in the output."""
    chain = [e(str(i), str(i + 1)) for i in range(12)]
    derived, capped = transitive_closure("PARENT_OF", chain, cap=5)
    assert capped is True
    assert len(derived) == 5


def test_hitting_the_depth_limit_is_reported():
    chain = [e(str(i), str(i + 1)) for i in range(10)]
    _, capped = transitive_closure("PARENT_OF", chain, max_depth=3)
    assert capped is True


def test_a_short_chain_that_finishes_is_not_flagged_as_capped():
    _, capped = transitive_closure("PARENT_OF", [e("a", "b"), e("b", "c")], max_depth=8)
    assert capped is False


# ------------------------------------------------------------------- validity

def test_validity_is_the_intersection_of_the_premises():
    premises = [
        Edge("a", "b", "W", "W", valid_at="2026-01-01", invalid_at="2026-06-01"),
        Edge("b", "c", "W", "W", valid_at="2026-03-01", invalid_at="2026-09-01"),
    ]
    assert intersect_validity(premises) == ("2026-03-01", "2026-06-01")


def test_premises_that_never_overlap_derive_nothing():
    """A conclusion cannot hold while one of its reasons does not."""
    premises = [
        Edge("a", "b", "W", "W", valid_at="2026-01-01", invalid_at="2026-02-01"),
        Edge("b", "c", "W", "W", valid_at="2026-05-01", invalid_at="2026-06-01"),
    ]
    assert intersect_validity(premises) is None


def test_an_empty_interval_stops_the_edge_being_written():
    edges = [
        Edge("a", "b", "W", "W", fact_uid="f1", valid_at="2026-01-01", invalid_at="2026-02-01"),
        Edge("b", "c", "W", "W", fact_uid="f2", valid_at="2026-05-01"),
    ]
    derived, _ = transitive_closure("PARENT_OF", edges)
    assert derived == []


def test_undated_premises_stay_undated_rather_than_being_invented():
    assert intersect_validity([Edge("a", "b", "W", "W")]) == (None, None)


def test_confidence_is_the_weakest_link():
    premises = [
        Edge("a", "b", "W", "W", confidence=0.9),
        Edge("b", "c", "W", "W", confidence=0.6),
    ]
    assert min_confidence(premises) == 0.6

    derived, _ = transitive_closure("PARENT_OF", [
        e("a", "b", confidence=0.9), e("b", "c", confidence=0.6),
    ])
    assert derived[0].confidence == 0.6


def test_a_derivation_carries_its_premises_for_the_proof_chain():
    derived, _ = transitive_closure("PARENT_OF", [e("a", "b"), e("b", "c")])
    assert set(derived[0].premises) == {"fact-ab", "fact-bc"}


def test_a_derivation_inherits_its_premises_provenance():
    """Regression, and an expensive one to find by eye: every read path does
    `UNWIND coalesce(r.source_record_keys, [])`, which yields NO rows for an
    empty list. A derived edge written without provenance keys is therefore
    invisible to the entity panel, to chat evidence and to the graph view --
    it exists in the database and nowhere in the product."""
    edges = [
        e("a", "b", source_record_keys=("jira:c:work_item:1",)),
        e("b", "c", source_record_keys=("jira:c:work_item:2",)),
    ]
    derived, _ = transitive_closure("PARENT_OF", edges)
    assert derived[0].source_record_keys == ("jira:c:work_item:1", "jira:c:work_item:2")


def test_provenance_is_deduplicated_not_repeated():
    edges = [
        e("a", "b", source_record_keys=("jira:c:work_item:1",)),
        e("b", "c", source_record_keys=("jira:c:work_item:1",)),
    ]
    derived, _ = transitive_closure("PARENT_OF", edges)
    assert derived[0].source_record_keys == ("jira:c:work_item:1",)


# --------------------------------------------------- symmetry / inverse / sub

def test_symmetry_derives_the_reverse_edge():
    derived = symmetric_closure("SAME_AS", [e("p1", "p2", from_label="Person", to_label="Person")])
    assert [(d.from_uid, d.to_uid) for d in derived] == [("p2", "p1")]


def test_symmetry_does_not_re_derive_an_asserted_reverse():
    edges = [
        e("p1", "p2", from_label="Person", to_label="Person"),
        e("p2", "p1", from_label="Person", to_label="Person"),
    ]
    assert symmetric_closure("SAME_AS", edges) == []


def test_inverse_flips_the_relation_and_the_endpoints():
    derived = inverse_edges("AUTHORED_BY", "AUTHORED", [
        e("commit1", "person1", from_label="Commit", to_label="Person")
    ])
    assert derived[0].relation == "AUTHORED"
    assert (derived[0].from_uid, derived[0].to_uid) == ("person1", "commit1")
    assert (derived[0].from_label, derived[0].to_label) == ("Person", "Commit")


def test_sub_property_keeps_direction_and_changes_only_the_relation():
    derived = sub_property_edges("RELATES_TO", [e("a", "b")])
    assert derived[0].relation == "RELATES_TO"
    assert (derived[0].from_uid, derived[0].to_uid) == ("a", "b")


# ------------------------------------------------------------ axiom selection

def test_only_relations_declared_transitive_are_closed():
    axioms = AxiomSet.build([
        RelationAxiom("PARENT_OF", "WorkItem", "WorkItem", is_transitive=True),
        RelationAxiom("BLOCKS", "WorkItem", "WorkItem"),
    ])
    assert axioms.transitive_relations() == ["PARENT_OF"]
    assert axioms.symmetric_relations() == []
