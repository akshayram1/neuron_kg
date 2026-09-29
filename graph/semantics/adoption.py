"""Ontology adoption: let counting widen the vocabulary, with an undo.

The graph refuses any (subject_kind, relation, object_kind) its rulebook does
not carry, records the refusal with its evidence, and counts the shape in
`ontology_misses`. That turns "the ontology is too narrow" into a ranked list
with a cost attached — but the list only helps if somebody reads it, and in
practice nobody does. On this graph 490 facts sat refused across 25 shapes,
untouched, because acting on them meant hand-writing SQL.

This module closes that loop **by counting, not by judgement**:

    shapes refused >= MIN_DOCS distinct documents  ->  allowed triple

Utopia runs the same rule with `auto_extend_ontology` defaulting ON, and their
reason for the document bar is the load-bearing part:

    "A statement appearing in only one document is that document's wording,
     not this organization's vocabulary — and the ontology feeds back into
     the extraction prompt, so one accident becomes a standing instruction."

Three constraints make it safe enough to run unattended:

**Only the allow-list widens.** `functional`, `is_transitive`, `is_symmetric`
are never inferred from counts. Utopia's carve-out is the sharp end: a wrong
`functional` drives the temporal engine to auto-close facts, and by the time
anyone notices, the closures are already a chain of supersedes. Frequency
tells you a shape is common. It tells you nothing about its cardinality.

**Every adoption is one revertible batch.** Their precondition, not a nicety:
*"the axis is not how confident we are, but how expensive it is if wrong."*
`unadopt` drops the axiom rows and deletes exactly the edges they let in.

**A dismissed shape is never re-proposed**, however often it recurs. With this
running unattended, re-proposing something a human declined is the system
overruling them — the mistake `dismissed_at` exists to prevent.

What counting cannot catch, and why the switch exists: on this graph the
widest candidate by document count is `Document -DEFINES-> System`, and 19 of
its 84 facts say a thing defines itself ("AWS-backed DataOS Lakehouse defines
AWS-backed DataOS Lakehouse"). Tautologies are filtered here explicitly, but
that fix was found by reading samples, not by a threshold — which is the whole
argument for `auto_extend_ontology` being a setting a human can turn off, and
for showing samples before adopting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from falkordb import Graph

from connectors.core.ledger import ConnectorLedger

logger = logging.getLogger("neuron.adoption")

# See the module docstring: a shape seen in one document is that document's
# wording. Utopia's constant, and it holds on this data — raising the bar from
# 1 to 2 documents drops 48% of the shapes and only 3.5% of the facts.
MIN_DOCS = 2
# Below this, a shape is not worth a re-extraction pass.
MIN_FACTS = 3

SETTING_AUTO_EXTEND = "auto_extend_ontology"


@dataclass
class AdoptionResult:
    """What one adoption run did, in the terms a caller needs to report it."""

    batch_id: str | None = None
    shapes: list[dict] = field(default_factory=list)
    facts_expected: int = 0
    chunks_requeued: int = 0
    skipped_reason: str | None = None

    @property
    def adopted(self) -> bool:
        return self.batch_id is not None


def auto_extend_enabled(ledger: ConnectorLedger, default: bool = False) -> bool:
    """Whether this graph adopts unattended.

    Defaults OFF here where Utopia defaults ON, for one reason: their misses
    are free-text predicates, so their threshold measures "is this the
    organization's word for it". Ours are already-typed triples, so the same
    threshold only measures "is this shape common" — a weaker claim, as the
    tautology case shows. Until a few batches have been reviewed by hand, the
    human stays in the loop by default.
    """
    value = ledger.setting(SETTING_AUTO_EXTEND)
    if value is None:
        return default
    return value.strip().lower() in {"1", "on", "true", "yes"}


def set_auto_extend(ledger: ConnectorLedger, enabled: bool) -> None:
    ledger.set_setting(SETTING_AUTO_EXTEND, "on" if enabled else "off")


def pending_report(
    ledger: ConnectorLedger, *, min_docs: int = MIN_DOCS, min_facts: int = MIN_FACTS,
) -> dict:
    """What a sync is currently leaving out of the graph, and what adopting
    would recover. Read-only — this is what the UI shows after a sync so the
    choice is informed rather than blind.
    """
    eligible = ledger.adoption_candidates(min_docs=min_docs, min_facts=min_facts)
    everything = ledger.adoption_candidates(min_docs=1, min_facts=1)
    return {
        "eligible": eligible,
        "eligible_shapes": len(eligible),
        "eligible_facts": sum(int(s["facts"]) for s in eligible),
        "total_shapes": len(everything),
        "total_facts": sum(int(s["facts"]) for s in everything),
        "min_docs": min_docs,
        "min_facts": min_facts,
        "auto_extend": auto_extend_enabled(ledger),
    }


def adopt(
    ledger: ConnectorLedger, *,
    min_docs: int = MIN_DOCS, min_facts: int = MIN_FACTS, force: bool = False,
) -> AdoptionResult:
    """Adopt every eligible shape as one batch and re-open the chunks it frees.

    `force` runs regardless of the switch — that is the explicit "adopt now"
    button, not the unattended path.
    """
    if not force and not auto_extend_enabled(ledger):
        return AdoptionResult(skipped_reason="auto_extend_ontology is off")

    shapes = ledger.adoption_candidates(min_docs=min_docs, min_facts=min_facts)
    if not shapes:
        return AdoptionResult(skipped_reason="no shape clears the threshold")

    batch_id = ledger.adopt_shapes(shapes, min_docs=min_docs)
    requeued = ledger.requeue_chunks_for_shapes(shapes)
    expected = sum(int(s["facts"]) for s in shapes)
    logger.info(
        "adopted %d shapes as batch %s (%d facts expected, %d chunks requeued)",
        len(shapes), batch_id, expected, requeued,
    )
    for shape in shapes:
        logger.info(
            "  + %s -%s-> %s  facts=%s docs=%s  e.g. %s",
            shape["subject_kind"], shape["relation"], shape["object_kind"],
            shape["facts"], shape["docs"], shape.get("example"),
        )
    return AdoptionResult(
        batch_id=batch_id, shapes=shapes,
        facts_expected=expected, chunks_requeued=requeued,
    )


def adoptions_with_edges(graph: Graph, ledger: ConnectorLedger,
                         include_undone: bool = False) -> list[dict]:
    """Each adoption plus the edges it has actually produced so far.

    The count is not cosmetic. Adopting only widens the vocabulary and queues
    the chunks it frees; the facts do not exist until the next extraction
    runs. Without this number a batch that has written 417 edges and one that
    has written none look identical in the list, and "Undo" on the second
    reports "0 edges removed" for what reads like no reason.
    """
    rows = ledger.adoptions(include_undone=include_undone)
    if not rows:
        return []
    counted = graph.query(
        "MATCH ()-[r]->() WHERE r.adopted_batch IS NOT NULL "
        "RETURN r.adopted_batch, count(r)"
    ).result_set
    by_batch = {str(batch): int(n) for batch, n in counted}
    return [{**row, "edges": by_batch.get(row["batch_id"], 0)} for row in rows]


def unadopt(graph: Graph, ledger: ConnectorLedger, batch_id: str) -> dict:
    """Undo one adoption: drop its axioms, then delete the edges it let in.

    Order matters. The ledger goes first so that a failure midway leaves the
    vocabulary narrowed rather than widened — a graph with stale edges and a
    correct rulebook is recoverable by re-running this; the reverse keeps
    writing new edges under a batch nobody can find.

    Only edges carrying this `adopted_batch` are removed. Anything the seeded
    vocabulary produced has a NULL stamp and is never touched, so an undo can
    never reach beyond what its own adoption created.
    """
    shapes = ledger.unadopt(batch_id)
    if not shapes:
        return {"batch_id": batch_id, "shapes": 0, "edges_removed": 0,
                "note": "no live adoption with that id"}
    removed = graph.query(
        "MATCH ()-[r]->() WHERE r.adopted_batch = $batch DELETE r RETURN count(r)",
        params={"batch": batch_id},
    ).result_set
    edges = int(removed[0][0]) if removed and removed[0] else 0
    logger.info("unadopted batch %s: %d shapes, %d edges removed", batch_id, len(shapes), edges)
    return {"batch_id": batch_id, "shapes": len(shapes), "edges_removed": edges}
