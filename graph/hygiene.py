"""Phase 6 hygiene job (25-plan.md "Phase 6 -- Hygiene job and review queue").

This module builds ONLY the two read-only diagnostics named in §6.1 and
§6.3:

  - §6.1 `isolated_node_report` -- per-label isolated-node counts, stored
    into `hygiene_runs` via `connectors.core.ledger.record_hygiene_counts`.
  - §6.3 `cardinality_violations` / `open_disputes` -- axiom-driven
    cardinality audit and open `DISPUTED_WITH` pairs.

§6.2 (candidate links), §6.4 (duplicate collector), §6.5 (review queue UI)
and §6.6 (dashboard) are parallel/later work and NOT built here.

Design principle carried over from the plan's Phase 6 intro ("every
mutating step has `dry_run=True` by default"): every function in this
module is READ + RECORD only. None of them ever mutate a graph node or
edge -- `isolated_node_report` writes rows to the SQLite ledger
(`hygiene_runs`), never to the graph itself, and the §6.3 audits don't
write anywhere by default (see `run_hygiene_checks`'s docstring for the
one opt-in exception).

Every "is this edge actually live" check here goes through
`graph.fact_predicates.live_fact_cypher`/`is_live_fact` rather than a bare
`invalid_at IS NULL`, per 25-plan.md §5.0.6's read contract -- so a
`corrected` or `pending_review` fact edge is never counted as a real,
live structural connection.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger, HygieneCount
from graph.axioms import DEFAULT_AXIOMS, AxiomSet
from graph.fact_predicates import live_fact_cypher

# Node labels §6.1 measures, in the exact vocabulary
# `connectors.core.ledger.HygieneCount`'s docstring already commits to
# ("Document", "Decision", "Commit", "Term", "System") and
# `tests/test_hygiene_ledger.py` exercises -- Term and System are reported
# as two SEPARATE rows, not one combined "Term/System" row.
DOCUMENT = "Document"
DECISION = "Decision"
COMMIT = "Commit"
TERM = "Term"
SYSTEM = "System"

# Relationship type MENTIONED_IN is provenance ("this node was mentioned in
# this source record"), not structure -- every extracted node gets one
# essentially unconditionally (`graph/writer.py::link_mentioned_in` is
# called for every entity in `graph/semantic_pass.py`). `graph/derived.py`
# already treats it the same way (`type(r1) <> 'MENTIONED_IN'` when looking
# for "real" connections in `_shared_concept_documents`). The §6.1 "degree 1
# (only EXTRACTED_FROM)" rule follows that same precedent: MENTIONED_IN is
# excluded from the degree count, so "degree 1" means "exactly one
# structural relationship, of any type, besides its own provenance edge."
_NON_STRUCTURAL_RELS = ("MENTIONED_IN",)


@dataclass(frozen=True)
class IsolatedNodeCount:
    """One label's isolated-node measurement for one hygiene run (§6.1).

    `isolated_uids` is the "and list" half of the plan's "count and list" --
    capped implicitly by nothing here (callers needing a capped UI page can
    slice it); the Cypher itself has no LIMIT so the count and the list
    length always agree.
    """

    label: str
    isolated_count: int
    total_count: int
    isolated_uids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CardinalityViolation:
    """§6.3: a subject node with more than one LIVE edge of a relation the
    axioms declare `functional` (at most one live object per subject). This
    should never happen -- a hit is a bug in a writer's supersede-then-
    upsert discipline, or an unresolved conflict. Reporting only: this
    module never auto-fixes a violation."""

    relation: str
    subject_uid: str
    subject_label: str
    subject_name: str | None
    object_uids: list[str]


@dataclass(frozen=True)
class OpenDispute:
    """§6.3: one live `DISPUTED_WITH` pair, as written by
    `graph.resolve_text_fact.link_disputed` -- a single directed edge
    `a -[:DISPUTED_WITH]-> b` between two Decision nodes, read as
    undirected/symmetric per the axiom. `link_disputed` never sets
    `invalid_at` on this edge and there is no "closed dispute" concept in
    how it writes today (confirmed by reading that function), so "open"
    here means exactly "the edge still exists" -- there is nothing else to
    check.
    """

    a_uid: str
    a_name: str | None
    b_uid: str
    b_name: str | None
    first_seen_at: str | None
    last_confirmed_at: str | None


@dataclass(frozen=True)
class HygieneReport:
    """Combined output of `run_hygiene_checks` -- the "run after each sync
    and nightly" entry point the plan's Phase 6 intro describes. Wiring an
    actual scheduler/sync caller to invoke this is out of scope here; this
    function only needs to exist and be callable/testable."""

    run_id: str
    isolated: dict[str, IsolatedNodeCount]
    cardinality_violations: list[CardinalityViolation]
    open_disputes: list[OpenDispute]


def _new_run_id() -> str:
    """Same idiom `graph.semantic_pass.run_semantic_pass` uses for its own
    per-call run identifier (`run_id = str(uuid.uuid4())`) -- one run_id per
    top-level hygiene invocation, not invented per sub-check."""
    return str(uuid.uuid4())


# --------------------------------------------------------------------- §6.1


def _isolated_document_report(graph: Graph) -> IsolatedNodeCount:
    """Document nodes with no live outgoing `DOCUMENTS` edge.

    `DOCUMENTS` is `Document -> WorkItem` per `graph/axioms.py`'s
    `_STRUCTURAL` table, so Document is always the subject side -- checking
    only the outgoing direction is correct, not a simplification. This
    counts derived `DOCUMENTS` edges too (e.g. `graph/derived.py`'s
    `_shared_concept_documents`/`_lift_through_parent`, both PARENT_DOCUMENTS/
    SHARED_CONCEPT-tagged `DOCUMENTS` edges): the isolation question is
    "is this node structurally connected at all", not "connected by a
    human-asserted edge only".
    """
    rows = graph.query(
        f"""
        MATCH (d:Document)
        OPTIONAL MATCH (d)-[r:DOCUMENTS]->()
        WHERE {live_fact_cypher('r')}
        WITH d, count(r) AS live_count
        RETURN d.uid, live_count
        """
    ).result_set
    isolated_uids = [str(row[0]) for row in rows if int(row[1]) == 0]
    return IsolatedNodeCount(DOCUMENT, len(isolated_uids), len(rows), isolated_uids)


def _isolated_decision_report(graph: Graph) -> IsolatedNodeCount:
    """Decision nodes with no live outgoing `APPLIES_TO` edge. `APPLIES_TO`
    is asserted from several subject kinds (Decision, Document, ...) per
    `graph/ontology.py::RELATION_TYPE_MAP`, but §6.1 asks specifically about
    Decision as the subject, so only that direction/label is checked."""
    rows = graph.query(
        f"""
        MATCH (d:Decision)
        OPTIONAL MATCH (d)-[r:APPLIES_TO]->()
        WHERE {live_fact_cypher('r')}
        WITH d, count(r) AS live_count
        RETURN d.uid, live_count
        """
    ).result_set
    isolated_uids = [str(row[0]) for row in rows if int(row[1]) == 0]
    return IsolatedNodeCount(DECISION, len(isolated_uids), len(rows), isolated_uids)


def _isolated_commit_report(graph: Graph) -> IsolatedNodeCount:
    """Commit nodes with no live outgoing `IMPLEMENTS` edge (`IMPLEMENTS`
    is `Commit -> WorkItem` / `PullRequest -> WorkItem`; Commit is always
    subject side)."""
    rows = graph.query(
        f"""
        MATCH (c:Commit)
        OPTIONAL MATCH (c)-[r:IMPLEMENTS]->()
        WHERE {live_fact_cypher('r')}
        WITH c, count(r) AS live_count
        RETURN c.uid, live_count
        """
    ).result_set
    isolated_uids = [str(row[0]) for row in rows if int(row[1]) == 0]
    return IsolatedNodeCount(COMMIT, len(isolated_uids), len(rows), isolated_uids)


def _isolated_degree_one_report(graph: Graph, label: str) -> IsolatedNodeCount:
    """Term/System nodes whose TOTAL live-relationship degree (both
    directions, every type except the provenance-only `MENTIONED_IN` --
    see `_NON_STRUCTURAL_RELS`) is exactly 1, AND that one relationship is
    `EXTRACTED_FROM`.

    This is deliberately NOT "has an EXTRACTED_FROM edge and nothing else
    counted" -- a node with an EXTRACTED_FROM edge AND a live DEFINES edge
    has degree 2 and must not be flagged; a node with exactly one live edge
    of some OTHER type (not EXTRACTED_FROM) has degree 1 but must also not
    be flagged by this rule. Both are enforced by checking the full distinct
    type-set of the node's live, non-MENTIONED_IN relationships, not just
    "does an EXTRACTED_FROM edge exist".

    The undirected pattern `(n)-[r]-()` binds each relationship once
    regardless of which direction it was written in, which is exactly
    "total degree" -- not double-counted per direction.
    """
    exclude_types = " AND ".join(f"type(r) <> '{rel}'" for rel in _NON_STRUCTURAL_RELS)
    rows = graph.query(
        f"""
        MATCH (n:{label})
        OPTIONAL MATCH (n)-[r]-()
        WHERE {live_fact_cypher('r')} AND {exclude_types}
        WITH n, collect(DISTINCT type(r)) AS rel_types, count(r) AS degree
        RETURN n.uid, degree, rel_types
        """
    ).result_set
    isolated_uids = [
        str(row[0]) for row in rows
        if int(row[1]) == 1 and list(row[2]) == ["EXTRACTED_FROM"]
    ]
    return IsolatedNodeCount(label, len(isolated_uids), len(rows), isolated_uids)


def isolated_node_report(
    graph: Graph, ledger: ConnectorLedger, *, graph_name: str = "default", run_id: str | None = None,
) -> dict[str, IsolatedNodeCount]:
    """§6.1: run the four isolated-node checks and record their counts into
    `hygiene_runs` via `ledger.record_hygiene_counts`.

    Read + record only: this function never writes to the graph, only to
    the SQLite ledger's `hygiene_runs` table. `dry_run` doesn't apply --
    there is no graph mutation to gate.

    Returns a dict keyed by label (`"Document"`, `"Decision"`, `"Commit"`,
    `"Term"`, `"System"`) so a caller can look up one category directly
    without re-deriving the label list.
    """
    run_id = run_id or _new_run_id()
    results: dict[str, IsolatedNodeCount] = {
        DOCUMENT: _isolated_document_report(graph),
        DECISION: _isolated_decision_report(graph),
        COMMIT: _isolated_commit_report(graph),
        TERM: _isolated_degree_one_report(graph, TERM),
        SYSTEM: _isolated_degree_one_report(graph, SYSTEM),
    }
    ledger.record_hygiene_counts(
        run_id,
        [
            HygieneCount(label, count.isolated_count, count.total_count)
            for label, count in results.items()
        ],
        graph_name=graph_name,
    )
    return results


# --------------------------------------------------------------------- §6.3


def cardinality_violations(
    graph: Graph, axioms: AxiomSet = DEFAULT_AXIOMS,
) -> list[CardinalityViolation]:
    """§6.3: for every relation the axioms declare `functional` (at most one
    live object per subject), find any subject node with MORE THAN ONE live
    edge of that relation type. This should never happen -- a hit is a bug
    or an unresolved conflict, never an auto-fix target; this function only
    surfaces it.

    Uses `graph.fact_predicates.live_fact_cypher`, not a bare
    `invalid_at IS NULL`, so a `corrected`/`pending_review` duplicate is
    never mistaken for a second live edge.
    """
    violations: list[CardinalityViolation] = []
    for relation in axioms.functional_relations():
        rows = graph.query(
            f"""
            MATCH (a)-[r:{relation}]->(b)
            WHERE {live_fact_cypher('r')}
            WITH a, collect(DISTINCT b.uid) AS object_uids
            WHERE size(object_uids) > 1
            RETURN a.uid, labels(a)[0], a.name, object_uids
            """
        ).result_set
        violations.extend(
            CardinalityViolation(
                relation=relation,
                subject_uid=str(row[0]),
                subject_label=str(row[1]) if row[1] else "",
                subject_name=row[2],
                object_uids=[str(uid) for uid in row[3]],
            )
            for row in rows
        )
    return violations


def open_disputes(graph: Graph) -> list[OpenDispute]:
    """§6.3: every live `DISPUTED_WITH` pair. `graph.resolve_text_fact
    .link_disputed` writes this as a single directed edge
    `a -[:DISPUTED_WITH]-> b` between two Decision nodes (MERGE, so at most
    one edge per ordered pair) and never sets `invalid_at` on it -- there is
    no "closed dispute" concept in how it's written today, so "open" means
    the edge exists. `live_fact_cypher` is still applied here (defense in
    depth / future-proofing per this module's docstring); it is a no-op
    today since `link_disputed` never sets any of the three fields it
    checks.
    """
    rows = graph.query(
        f"""
        MATCH (a:Decision)-[r:DISPUTED_WITH]->(b:Decision)
        WHERE {live_fact_cypher('r')}
        RETURN a.uid, a.name, b.uid, b.name, r.first_seen_at, r.last_confirmed_at
        """
    ).result_set
    return [
        OpenDispute(
            a_uid=str(row[0]), a_name=row[1],
            b_uid=str(row[2]), b_name=row[3],
            first_seen_at=row[4], last_confirmed_at=row[5],
        )
        for row in rows
    ]


# --------------------------------------------------------------- orchestration


def run_hygiene_checks(
    graph: Graph, ledger: ConnectorLedger, *, graph_name: str = "default",
    axioms: AxiomSet = DEFAULT_AXIOMS, record_audit_counts: bool = True,
) -> HygieneReport:
    """Top-level entry point: run §6.1's isolated-node report and §6.3's
    cardinality/dispute audit, and return the combined result. This is the
    "run after each sync and nightly" function the plan's Phase 6 intro
    describes -- wiring an actual sync/scheduler caller to invoke it is out
    of scope for this module; it only needs to exist and be callable.

    One `run_id` (uuid4, same idiom as `run_semantic_pass`) covers the
    whole combined run, so every row this call produces -- §6.1's five
    `hygiene_runs` rows and, if `record_audit_counts`, §6.3's two -- can be
    grouped by it.

    `record_audit_counts` (default True): §6.3's plan text does not
    explicitly require a ledger write the way §6.1's "Store counts in
    hygiene_runs" does, but §6.6's dashboard explicitly lists "cardinality
    violations, open disputes" as rows to show, and `hygiene_runs` already
    has the right shape (`label`, `isolated_count`, `total_count`) to carry
    them -- so by default this also writes two more `hygiene_runs` rows:
    `label="cardinality_violation"` and `label="open_dispute"`, with
    `isolated_count` = the violation/dispute count and `total_count` set
    equal to `isolated_count` (there is no natural "total possible"
    denominator for a violation/dispute count the way there is for isolated
    nodes -- documented convention, not a measured population). Pass
    `record_audit_counts=False` to skip this and keep §6.3 pure reporting
    with no ledger write at all.
    """
    run_id = _new_run_id()
    isolated = isolated_node_report(graph, ledger, graph_name=graph_name, run_id=run_id)
    violations = cardinality_violations(graph, axioms)
    disputes = open_disputes(graph)
    if record_audit_counts:
        ledger.record_hygiene_counts(
            run_id,
            [
                HygieneCount("cardinality_violation", len(violations), len(violations)),
                HygieneCount("open_dispute", len(disputes), len(disputes)),
            ],
            graph_name=graph_name,
        )
    return HygieneReport(
        run_id=run_id,
        isolated=isolated,
        cardinality_violations=violations,
        open_disputes=disputes,
    )
