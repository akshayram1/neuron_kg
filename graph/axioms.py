"""Relation axioms as data.

`graph/ontology.py`'s `RELATION_TYPE_MAP` is a Python dict: it can say which
(subject, relation, object) triples are allowed and nothing else. It cannot
say that `PARENT_OF` is transitive, that `ASSIGNED_TO` holds one value at a
time, or that `SAME_AS` is symmetric — so nothing downstream can reason from
it or detect a contradiction with it, and it cannot grow without a code
change and a redeploy.

This module keeps the same questions answerable by the same functions, but
from rows instead of a literal, and adds the axiom columns Utopia carries on
its `relation_types` table (`utopia/migrations/0003_graph.sql:49-86`).

TWO SEPARATE CONCERNS, deliberately not merged:

  - `extractable` — may the LLM assert this relation? Only the semantic
    relations may. Structural ones (`ASSIGNED_TO`, `PARENT_OF`, `MODIFIES`…)
    are written by the deterministic pass from provider fields, and letting
    an extraction invent them would put guessed structure next to read
    structure with no way to tell them apart afterwards.
  - the axioms themselves — transitivity, symmetry, inverses, cardinality.
    These describe EVERY relation, structural ones included, because that is
    what a derivation or a consistency check reads.

So adding the structural relations here widens what the graph can reason
about without widening what the model is allowed to claim. `is_allowed`
consults only `extractable` rows, which is why this change is behaviour-
preserving for extraction (see tests/test_axioms.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from graph.ontology import RELATION_TYPE_MAP

AS_IS = "as_is"
SWAPPED = "swapped"


@dataclass(frozen=True)
class RelationAxiom:
    """One (subject_kind, relation, object_kind) triple the ontology knows.

    `temporal` follows Utopia's three-way split: a `state` holds over an
    interval and can be superseded, an `event` happens at a moment and never
    ends, an `eternal` fact has no clock at all. It decides whether two
    overlapping values are a contradiction or just a succession.
    """

    relation: str
    subject_kind: str
    object_kind: str
    extractable: bool = False
    functional: bool = False       # at most one live object per subject
    is_transitive: bool = False
    is_symmetric: bool = False
    is_asymmetric: bool = False
    inverse_of: str | None = None
    sub_property_of: str | None = None
    temporal: str = "state"        # state | event | eternal


@dataclass(frozen=True)
class AxiomSet:
    axioms: tuple[RelationAxiom, ...] = ()
    _allowed: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict, compare=False)

    @classmethod
    def build(cls, axioms: list[RelationAxiom]) -> "AxiomSet":
        allowed: dict[tuple[str, str], list[str]] = {}
        for axiom in axioms:
            if not axiom.extractable:
                continue
            allowed.setdefault((axiom.subject_kind, axiom.object_kind), []).append(axiom.relation)
        return cls(
            axioms=tuple(axioms),
            _allowed={key: tuple(value) for key, value in allowed.items()},
        )

    def is_allowed(self, subject_kind: str, relation: str, object_kind: str) -> bool:
        return relation in self._allowed.get((subject_kind, object_kind), ())

    def resolve_direction(self, subject_kind: str, relation: str, object_kind: str) -> str | None:
        """`AS_IS`, `SWAPPED`, or None — see `graph.ontology.resolve_direction`
        for why a reversed triple is corrected rather than discarded."""
        if self.is_allowed(subject_kind, relation, object_kind):
            return AS_IS
        if self.is_allowed(object_kind, relation, subject_kind):
            return SWAPPED
        return None

    def for_relation(self, relation: str) -> list[RelationAxiom]:
        return [axiom for axiom in self.axioms if axiom.relation == relation]

    def temporal_of(self, relation: str) -> str:
        """`state` | `event` | `eternal` for a relation, `state` when unknown.

        A window read needs this: an event holds only at its own instant, so
        "worked on in August" must not return a July commit merely because
        that commit's fact is still open. A state overlaps the window.
        Relations whose axioms disagree fall back to `state` — the wider
        reading, so a window under-filters rather than hides evidence.
        """
        kinds = {axiom.temporal for axiom in self.axioms if axiom.relation == relation}
        return kinds.pop() if len(kinds) == 1 else "state"

    def transitive_relations(self) -> list[str]:
        return sorted({a.relation for a in self.axioms if a.is_transitive})

    def symmetric_relations(self) -> list[str]:
        return sorted({a.relation for a in self.axioms if a.is_symmetric})

    def functional_relations(self) -> list[str]:
        return sorted({a.relation for a in self.axioms if a.functional})

    def inverse_pairs(self) -> list[tuple[str, str]]:
        return sorted({
            (a.relation, a.inverse_of) for a in self.axioms if a.inverse_of
        })


# Structural relations: written by the deterministic pass, never extractable,
# present here for their axioms. Each entry is the shape the writers actually
# produce -- verified against graph/{jira,bitbucket,github,notion}_pipeline.py
# and graph/resolver.py rather than assumed.
_STRUCTURAL: list[RelationAxiom] = [
    # A subtask's parent chain: if A is under B and B under C, A is under C.
    RelationAxiom("PARENT_OF", "WorkItem", "WorkItem", is_transitive=True, is_asymmetric=True),
    RelationAxiom("CONTAINS", "Repository", "SourceFile", is_transitive=True),
    RelationAxiom("CONTAINS", "Repository", "Commit", is_transitive=True),
    RelationAxiom("CONTAINS", "Workspace", "Document", is_transitive=True),
    # Identity: the same person seen through two providers. Symmetric and
    # transitive is what makes a merge cluster a cluster.
    RelationAxiom("SAME_AS", "Person", "Person", is_symmetric=True, is_transitive=True),
    # One live assignee / reporter / project at a time -- a second one is a
    # contradiction, not an addition. (`functional` is what a cardinality
    # check reads; the writer already enforces it via supersede-then-upsert.)
    RelationAxiom("ASSIGNED_TO", "WorkItem", "Person", functional=True),
    RelationAxiom("REPORTED_BY", "WorkItem", "Person", functional=True),
    RelationAxiom("BELONGS_TO", "WorkItem", "Project", functional=True),
    RelationAxiom("AUTHORED_BY", "Commit", "Person", functional=True, temporal="event"),
    RelationAxiom("AUTHORED_BY", "PullRequest", "Person", functional=True, temporal="event"),
    RelationAxiom("MODIFIES", "Commit", "SourceFile", temporal="event"),
    RelationAxiom("IMPLEMENTS", "Commit", "WorkItem", temporal="event"),
    RelationAxiom("IMPLEMENTS", "PullRequest", "WorkItem", temporal="event"),
    RelationAxiom("BLOCKS", "WorkItem", "WorkItem", is_asymmetric=True),
    RelationAxiom("DOCUMENTS", "Document", "WorkItem"),
]

# Axioms for the extractable (LLM-asserted) relations. The allow-list itself
# is derived from RELATION_TYPE_MAP so the two can never disagree.
_SEMANTIC_AXIOMS = {
    "SUPERSEDES": {"is_transitive": True, "is_asymmetric": True},
    "DECIDED_BY": {"functional": False, "temporal": "event"},
    "DEFINES": {},
    "APPLIES_TO": {},
    "CAVEAT_OF": {},
    "OWNS": {},
}


def seed_axioms() -> list[RelationAxiom]:
    """The built-in vocabulary. Seeded into the ledger on first use; editable
    there afterwards without touching this file."""
    axioms = [
        RelationAxiom(
            relation=relation, subject_kind=subject_kind, object_kind=object_kind,
            extractable=True, **_SEMANTIC_AXIOMS.get(relation, {}),
        )
        for (subject_kind, object_kind), relations in RELATION_TYPE_MAP.items()
        for relation in relations
    ]
    return axioms + _STRUCTURAL


DEFAULT_AXIOMS = AxiomSet.build(seed_axioms())


def _as_row(axiom: RelationAxiom) -> dict:
    return {
        "relation": axiom.relation, "subject_kind": axiom.subject_kind,
        "object_kind": axiom.object_kind, "extractable": axiom.extractable,
        "functional": axiom.functional, "is_transitive": axiom.is_transitive,
        "is_symmetric": axiom.is_symmetric, "is_asymmetric": axiom.is_asymmetric,
        "inverse_of": axiom.inverse_of, "sub_property_of": axiom.sub_property_of,
        "temporal": axiom.temporal,
    }


def load_axioms(ledger) -> AxiomSet:
    """Read this graph's vocabulary, seeding it from code on first use.

    Per-graph by construction: the ledger file is already resolved per graph
    name (`graph/multigraph.py`), so two graphs can disagree about their
    ontology without either one leaking into the other.
    """
    ledger.seed_axioms_if_empty([_as_row(axiom) for axiom in seed_axioms()])
    rows = ledger.axiom_rows()
    if not rows:
        return DEFAULT_AXIOMS
    return AxiomSet.build([
        RelationAxiom(
            relation=str(row["relation"]),
            subject_kind=str(row["subject_kind"]),
            object_kind=str(row["object_kind"]),
            extractable=bool(row["extractable"]),
            functional=bool(row["functional"]),
            is_transitive=bool(row["is_transitive"]),
            is_symmetric=bool(row["is_symmetric"]),
            is_asymmetric=bool(row["is_asymmetric"]),
            inverse_of=row["inverse_of"] or None,
            sub_property_of=row["sub_property_of"] or None,
            temporal=str(row["temporal"] or "state"),
        )
        for row in rows
    ])
