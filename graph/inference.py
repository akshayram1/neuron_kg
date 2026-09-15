"""Forward chaining over relation axioms — no LLM anywhere in this module.

`graph/derived.py` holds four hand-written rules, each its own Cypher query.
This one compiles rules from the axioms in `graph/axioms.py`: declare
`PARENT_OF` transitive once and the closure follows, with no new code. It is
the mechanism that can win back some of the "why" that disappeared when LLM
extraction was switched off for Jira/GitHub/Bitbucket, because it produces
new edges from structure alone.

Every constraint below exists because inference that is merely plausible is
worse than none — a wrong axiom multiplies into thousands of wrong edges.
They are Utopia's, arrived at by measurement rather than taste
(`utopia/crates/utopia-reason/src/derive.rs`):

  - **Asserted always wins.** A triple already asserted is never derived, so
    "who said this" has exactly one answer.
  - **Validity is the INTERSECTION of the premises.** A conclusion cannot
    hold while one of its reasons does not. An empty intersection derives
    nothing at all rather than an undated edge.
  - **Confidence is the MINIMUM of the premises** — a chain is only as
    trustworthy as its weakest link.
  - **Depth and per-relation caps, and what got capped is REPORTED.**
    Utopia measured `part_of` going 185 -> 828 without converging, with a
    depth histogram that oscillated from level 5 on: the shape of a cycle.
    Silently truncating makes "derived fewer" and "nothing satisfies the
    rule" look identical in the output.
  - **Off by default.** Derivation is opt-in per run; a graph nobody asked
    to reason over should not grow edges on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from falkordb import Graph

from graph import writer as w
from graph.axioms import AxiomSet, DEFAULT_AXIOMS

logger = logging.getLogger("neuron.inference")

MAX_DEPTH = 8
MAX_DERIVED_PER_RELATION = 20_000

TRANSITIVE = "axiom_transitive"
SYMMETRIC = "axiom_symmetric"
INVERSE = "axiom_inverse"
SUB_PROPERTY = "axiom_sub_property"


@dataclass(frozen=True)
class Edge:
    """One asserted edge, reduced to what inference actually needs."""

    from_uid: str
    to_uid: str
    from_label: str
    to_label: str
    fact_uid: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None
    confidence: float = 1.0
    # Carried, not decorative: every read path does
    # `UNWIND coalesce(r.source_record_keys, [])`, so an edge with no keys is
    # invisible to the entity panel, to chat evidence and to the graph view
    # -- derived work nobody can see is worse than none.
    source_record_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Derivation:
    relation: str
    rule: str
    from_uid: str
    to_uid: str
    from_label: str
    to_label: str
    premises: tuple[str, ...]
    valid_at: str | None
    invalid_at: str | None
    confidence: float
    source_record_keys: tuple[str, ...] = ()


@dataclass
class InferenceReport:
    derived: int = 0
    skipped_asserted: int = 0
    skipped_empty_interval: int = 0
    capped: list[str] = field(default_factory=list)
    per_rule: dict[str, int] = field(default_factory=dict)


def intersect_validity(
    premises: list[Edge],
) -> tuple[str | None, str | None] | None:
    """Validity of a conclusion = the window where EVERY premise holds.

    Returns None when that window is empty, which means: derive nothing. The
    alternative — writing the edge with no dates — would assert the
    conclusion holds always, which is precisely what the premises deny.

    ISO-8601 strings compare lexicographically in UTC, which is what every
    writer in this repo stores.
    """
    starts = [edge.valid_at for edge in premises if edge.valid_at]
    ends = [edge.invalid_at for edge in premises if edge.invalid_at]
    valid_at = max(starts) if starts else None
    invalid_at = min(ends) if ends else None
    if valid_at and invalid_at and valid_at >= invalid_at:
        return None
    return valid_at, invalid_at


def min_confidence(premises: list[Edge]) -> float:
    return min((edge.confidence for edge in premises), default=1.0)


def _conclude(
    relation: str, rule: str, first: Edge, last: Edge,
    from_uid: str, to_uid: str, from_label: str, to_label: str,
    premises: list[Edge],
) -> Derivation | None:
    window = intersect_validity(premises)
    if window is None:
        return None
    valid_at, invalid_at = window
    return Derivation(
        relation=relation, rule=rule,
        from_uid=from_uid, to_uid=to_uid,
        from_label=from_label, to_label=to_label,
        premises=tuple(e.fact_uid for e in premises if e.fact_uid),
        valid_at=valid_at, invalid_at=invalid_at,
        confidence=min_confidence(premises),
        # A conclusion's provenance is the union of its reasons' provenance.
        source_record_keys=tuple(sorted({
            key for edge in premises for key in edge.source_record_keys if key
        })),
    )


def transitive_closure(
    relation: str, edges: list[Edge], *, max_depth: int = MAX_DEPTH,
    cap: int = MAX_DERIVED_PER_RELATION,
) -> tuple[list[Derivation], bool]:
    """A->B, B->C  =>  A->C, repeated to a fixed point or a limit.

    Self-loops are never emitted: a cycle in the data would otherwise derive
    `A -> A`, which is true of nothing. Reaching `max_depth` or `cap` returns
    `capped=True` so the caller can say so out loud.
    """
    asserted = {(e.from_uid, e.to_uid) for e in edges}
    by_source: dict[str, list[Edge]] = {}
    for edge in edges:
        by_source.setdefault(edge.from_uid, []).append(edge)

    derived: dict[tuple[str, str], Derivation] = {}
    # frontier: (start_edge_chain) -> paths of length >= 2
    frontier: list[tuple[Edge, Edge, list[Edge]]] = []
    for first in edges:
        for second in by_source.get(first.to_uid, []):
            frontier.append((first, second, [first, second]))

    capped = False
    depth = 2
    while frontier and depth <= max_depth:
        next_frontier: list[tuple[Edge, Edge, list[Edge]]] = []
        for first, last, chain in frontier:
            pair = (first.from_uid, last.to_uid)
            if first.from_uid == last.to_uid:
                continue                      # a cycle, not a fact
            if pair not in asserted and pair not in derived:
                conclusion = _conclude(
                    relation, TRANSITIVE, first, last,
                    first.from_uid, last.to_uid, first.from_label, last.to_label, chain,
                )
                if conclusion is not None:
                    derived[pair] = conclusion
                    if len(derived) >= cap:
                        return list(derived.values()), True
            for nxt in by_source.get(last.to_uid, []):
                next_frontier.append((first, nxt, [*chain, nxt]))
        frontier = next_frontier
        depth += 1
    if frontier:
        capped = True                         # stopped by depth, not exhaustion
    return list(derived.values()), capped


def symmetric_closure(relation: str, edges: list[Edge]) -> list[Derivation]:
    """A->B  =>  B->A, for a relation declared symmetric."""
    asserted = {(e.from_uid, e.to_uid) for e in edges}
    out: list[Derivation] = []
    for edge in edges:
        pair = (edge.to_uid, edge.from_uid)
        if pair in asserted or edge.from_uid == edge.to_uid:
            continue
        conclusion = _conclude(
            relation, SYMMETRIC, edge, edge,
            edge.to_uid, edge.from_uid, edge.to_label, edge.from_label, [edge],
        )
        if conclusion is not None:
            out.append(conclusion)
    return out


def inverse_edges(relation: str, inverse: str, edges: list[Edge]) -> list[Derivation]:
    """A -rel-> B  =>  B -inverse-> A."""
    out: list[Derivation] = []
    for edge in edges:
        conclusion = _conclude(
            inverse, INVERSE, edge, edge,
            edge.to_uid, edge.from_uid, edge.to_label, edge.from_label, [edge],
        )
        if conclusion is not None:
            out.append(conclusion)
    return out


def sub_property_edges(parent: str, edges: list[Edge]) -> list[Derivation]:
    """A -child-> B  =>  A -parent-> B, when child is declared below parent."""
    out: list[Derivation] = []
    for edge in edges:
        conclusion = _conclude(
            parent, SUB_PROPERTY, edge, edge,
            edge.from_uid, edge.to_uid, edge.from_label, edge.to_label, [edge],
        )
        if conclusion is not None:
            out.append(conclusion)
    return out


# ------------------------------------------------------------------ graph I/O

def read_asserted(graph: Graph, relation: str) -> list[Edge]:
    """Live, asserted edges of one relation. Derived rows are excluded on
    purpose: inference stays a pure function of (asserted facts, axioms), so
    a run cannot feed on its own previous output."""
    rows = graph.query(
        f"MATCH (a)-[r:{relation}]->(b) "
        "WHERE r.invalid_at IS NULL AND coalesce(r.derived, false) = false "
        "RETURN a.uid, b.uid, labels(a)[0], labels(b)[0], r.fact_uid, "
        "       r.valid_at, r.invalid_at, r.confidence, r.source_record_keys"
    ).result_set
    return [
        Edge(
            from_uid=row[0], to_uid=row[1], from_label=row[2] or "", to_label=row[3] or "",
            fact_uid=row[4], valid_at=row[5], invalid_at=row[6],
            confidence=float(row[7]) if row[7] is not None else 1.0,
            source_record_keys=tuple(row[8] or ()),
        )
        for row in rows if row[0] and row[1]
    ]


def _write(graph: Graph, derivations: list[Derivation]) -> int:
    by_shape: dict[tuple[str, str, str], list[dict]] = {}
    for item in derivations:
        by_shape.setdefault((item.relation, item.from_label, item.to_label), []).append({
            "from_uid": item.from_uid, "to_uid": item.to_uid,
            "source_record_keys": list(item.source_record_keys),
            "evidence": None,
            "extraction_method": "derived",
            "confidence": item.confidence,
            "valid_at": item.valid_at,
            "invalid_at": item.invalid_at,
            "derived": True,
            "derived_rule": item.rule,
            "premise_fact_uids": list(item.premises),
            "extractor_version": "axiom-v1",
            "model": None,
            "chunk_id": None, "chunk_hash": None,
        })
    written = 0
    for (relation, from_label, to_label), payload in by_shape.items():
        if not from_label or not to_label:
            continue
        w.upsert_fact_edges(graph, relation, from_label, to_label, payload)
        written += len(payload)
    return written


def materialize(
    graph: Graph, axioms: AxiomSet = DEFAULT_AXIOMS, *, dry_run: bool = False,
    max_depth: int = MAX_DEPTH, cap: int = MAX_DERIVED_PER_RELATION,
) -> InferenceReport:
    """Run every compiled rule once over the whole graph.

    `dry_run` reports what would be written without writing it — the safe way
    to look at a new axiom before letting it multiply.
    """
    report = InferenceReport()
    pending: list[Derivation] = []

    for relation in axioms.transitive_relations():
        edges = read_asserted(graph, relation)
        if not edges:
            continue
        conclusions, capped = transitive_closure(
            relation, edges, max_depth=max_depth, cap=cap
        )
        if capped:
            report.capped.append(relation)
        pending.extend(conclusions)
        report.per_rule[f"{TRANSITIVE}:{relation}"] = len(conclusions)

    for relation in axioms.symmetric_relations():
        edges = read_asserted(graph, relation)
        conclusions = symmetric_closure(relation, edges)
        pending.extend(conclusions)
        if conclusions:
            report.per_rule[f"{SYMMETRIC}:{relation}"] = len(conclusions)

    for relation, inverse in axioms.inverse_pairs():
        conclusions = inverse_edges(relation, inverse, read_asserted(graph, relation))
        pending.extend(conclusions)
        if conclusions:
            report.per_rule[f"{INVERSE}:{relation}"] = len(conclusions)

    for axiom in axioms.axioms:
        if not axiom.sub_property_of:
            continue
        conclusions = sub_property_edges(
            axiom.sub_property_of, read_asserted(graph, axiom.relation)
        )
        pending.extend(conclusions)
        if conclusions:
            report.per_rule[f"{SUB_PROPERTY}:{axiom.relation}"] = len(conclusions)

    report.derived = len(pending)
    if not dry_run:
        _write(graph, pending)
    if report.capped:
        logger.warning(
            "inference hit a limit for %s -- the result is a slice, not the closure",
            ", ".join(report.capped),
        )
    logger.info(
        "inference: %d derived%s, rules=%s",
        report.derived, " (dry run)" if dry_run else "", report.per_rule,
    )
    return report
