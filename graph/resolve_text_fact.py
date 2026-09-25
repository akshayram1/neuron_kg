"""25-plan.md Phase 5 §5.2 — `resolve_text_fact`: classify-before-write for
text facts.

This module is deliberately separate from `graph/writer.py` (rather than
appended to that already-large file) because it composes §5.0's writer
primitives (`close_fact`, `correct_fact`, `confirm_fact`,
`upsert_fact_edges(..., revive=...)`, `mark_ended_unknown`,
`set_projection_status`, all in `graph/writer.py`) into one piece of
*decision logic*, rather than adding another low-level Cypher primitive.
Keeping that decision logic out of `writer.py` also means `writer.py`'s
existing primitives stay untouched (this task's rule): everything here is a
new caller of them, never an edit to them.

Two independent things live here, on purpose kept apart:

1. `classify_fact_update` / `_map_laya_kind_to_resolve_kind` — turning new
   evidence plus an existing fact into one of six conflict kinds. This is
   the one part of §5.2 that needs a real Laya call, which this task's
   sandbox cannot make (see `classify_fact_update`'s docstring).
2. `resolve_text_fact` — the state machine that decides what to DO with an
   already-classified `kind`. This needs no Laya dependency at all, which is
   why it is fully unit-testable (see tests/test_resolve_text_fact.py) even
   though (1) is not implemented for real yet.

`find_conflict_candidates` is the third piece: the blocking (not
graph-wide) Cypher query that finds `old` candidates for `resolve_text_fact`
to run against.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from falkordb import Graph

from graph import writer as w
from graph.axioms import DEFAULT_AXIOMS, AxiomSet
from graph.time_axis import parse_iso

logger = logging.getLogger("neuron.resolve_text_fact")

# The six conflict kinds `resolve_text_fact` dispatches on (25-plan.md §5.0
# table). Laya's real trained `fact_update` schema only has five — see
# `_map_laya_kind_to_resolve_kind` for how the sixth (`corrects`) is reached
# today only by a direct caller, never by `classify_fact_update`'s output.
RESOLVE_KINDS: tuple[str, ...] = (
    "duplicate", "extends", "unrelated", "newer_state", "corrects", "contradicts",
)

# `NEURON_FACT_UPDATE_MODE` values (25-plan.md §5.2). `suggest` is the
# default everywhere this module reads the env var from, matching the
# plan's own "`auto` is allowed only after review precision meets §10.4"
# framing -- nothing in this codebase has measured that bar yet.
_VALID_MODES = ("suggest", "auto")


# ---------------------------------------------------------------------------
# Data shapes.
#
# `old` mirrors `graph/writer.py::_find_live_fact`'s return shape (the
# closest existing precedent for "what fields are realistically available
# for a fact already in the graph") plus the handful of fields that
# function's callers so far haven't needed: `fact_uid` (its caller already
# has it, since it's the lookup key), `pinned`, `invalid_at` (always None
# for a genuinely live candidate, but carried so `windows_disjoint` has a
# uniform two-sided signature), and `from_label`/`to_label` (needed to tell
# which endpoint is the Decision node for `link_disputed`, and which
# `_find_live_fact` doesn't return because none of its current callers
# needed a label). `find_conflict_candidates` below is what actually
# populates this shape from a real graph.
#
# `new` is the equivalent shape for a fact that has NOT reached the graph
# yet -- the extraction-time equivalent of the same row shape
# `upsert_fact_edges` already accepts (`from_uid`/`to_uid`/`rel_type`/
# `evidence`/`confidence`/`extraction_method`/`pinned`/`decay_class`), plus
# the handful of fields `resolve_text_fact`'s own logic needs that a plain
# `upsert_fact_edges` row doesn't carry: `source_time` (record time --
# 25-plan.md §5.1 -- used for `confirm`/`correct_fact`/`mark_ended_unknown`
# when a fact has no stated world date of its own), `source_record_key`
# (singular -- one record triggered this particular resolution, even though
# the eventual write accumulates a list), and `invalid_at`, which starts
# `None` and is the one field the pseudocode's own "newer_state, backfill"
# branch *mutates in place* before the fact is written (`new.invalid_at =
# old.valid_at`) -- which is exactly why this is a mutable dataclass and not
# a frozen one.
# ---------------------------------------------------------------------------


@dataclass
class OldFact:
    """A live fact edge already in the graph -- one candidate row from
    `find_conflict_candidates`."""

    fact_uid: str
    from_uid: str
    from_label: str
    to_uid: str
    to_label: str
    rel_type: str
    valid_at: str | None = None
    invalid_at: str | None = None
    pinned: bool = False
    source_record_keys: list[str] | None = None
    evidence: str | None = None
    confidence: float | None = None
    extraction_method: str | None = None


@dataclass
class NewFact:
    """An incoming, not-yet-written fact -- built by the caller (the future
    `_write_extraction` wiring, or a test) from an extracted candidate plus
    §5.1 timing info. `fact_uid` may be left `None`; `resolve_text_fact`
    then derives it the same deterministic way `upsert_fact_edges` does."""

    from_uid: str
    from_label: str
    to_uid: str
    to_label: str
    rel_type: str
    source_time: str
    source_record_key: str | None = None
    valid_at: str | None = None
    invalid_at: str | None = None
    evidence: str | None = None
    confidence: float | None = None
    extraction_method: str | None = "llm"
    fact_uid: str | None = None
    pinned: bool = False
    decay_class: str | None = None


# ---------------------------------------------------------------------------
# `classify_fact_update` -- the Laya-dependent half. Kept entirely separate
# from `resolve_text_fact` per this task's instructions: everything below
# this section needs no Laya dependency at all.
# ---------------------------------------------------------------------------


def classify_fact_update(
    existing_fact: str,
    existing_valid_from: str | None,
    new_evidence: str,
    new_timestamp: str | None,
) -> str:
    """Laya `fact_update` classification -- PLACEHOLDER, not implemented.

    Same shape as the trained question in the sibling `personal_exp/laya`
    project's checkpoint (`ingest/schema.py`'s `QUESTIONS["fact_update"]`):
    state `{"existing_fact": existing_fact, "existing_valid_from":
    existing_valid_from, "new_evidence": new_evidence, "new_timestamp":
    new_timestamp}`, answer one of `duplicate` / `updates` / `contradicts`
    / `extends` / `unrelated`.

    Why this raises instead of guessing: the `laya` package is installed in
    the repo owner's live working environment (via `pyproject.toml`, which
    this task is not allowed to touch), but this task runs in an isolated
    worktree that does not have it, and cannot add it. There is no way to
    make a real Laya call from here. Rather than fabricate a fake "working"
    classifier (which would either always guess the same label, silently
    wrong most of the time, or require reverse-engineering behavior no one
    has verified), this raises `NotImplementedError` with exactly what a
    real implementation needs -- same choice already made for
    `graph/rerank.py`'s `LayaReranker.score()` (see QUERIES.md "2.1 — Laya
    reranker is a stub").

    A real implementation needs:
      1. The `laya` package importable (a packaging decision -- vendored
         dependency vs. sidecar service -- still open per QUERIES.md).
      2. `LAYA_MODEL_DIR` (or equivalent) pointing at the trained
         `fact_update` checkpoint (`personal_exp/laya/model/laya-ingest/`).
      3. `laya.Agent(MODEL_DIR, device=...).predict(state)` called with
         exactly the state dict shape documented above, against the
         `fact_update` question.
      4. Its five-way output passed through `_map_laya_kind_to_resolve_kind`
         before it reaches `resolve_text_fact` (which expects six kinds,
         one of which -- `corrects` -- Laya's current trained schema cannot
         produce; see that function's docstring).

    Trade-off, documented per this task's instructions: raising here (rather
    than a conservative always-`"unrelated"` fallback) means a caller that
    invokes this today gets a loud, immediate crash instead of a silently
    wrong classification. A caller that needs to keep running with no real
    Laya available should catch `NotImplementedError` itself and decide its
    own fallback (e.g. `"unrelated"`, which never mutates anything) rather
    than have that choice made invisibly inside this function.
    """
    raise NotImplementedError(
        "classify_fact_update: no real Laya `fact_update` integration exists in "
        "this task's sandbox. See this function's docstring for exactly what a "
        "real implementation needs (the `laya` package, LAYA_MODEL_DIR, and the "
        "{'existing_fact', 'existing_valid_from', 'new_evidence', 'new_timestamp'} "
        "state shape). A caller that must not crash should catch this and choose "
        "its own conservative fallback (e.g. 'unrelated') rather than rely on a "
        "guessed-at implementation here."
    )


# Laya's real trained `fact_update` schema has five options; `resolve_text_fact`
# accepts six kinds. `"updates"` maps to `"newer_state"`, NEVER `"corrects"` --
# Laya cannot currently distinguish "the old fact was true until now" (DICE
# "world progression") from "the old fact was always wrong" (DICE
# "revision"); that split is 25-plan.md §5.3, explicit future work needing
# retraining that hasn't happened. `correct_fact`'s effect (a fact excluded
# from ALL world-time truth, including dates before the correction) is far
# more damaging and harder to reverse than `close_fact`'s effect (the old
# fact was simply true until replaced) -- so under genuine uncertainty
# between the two, the safe default is the less destructive one. This is
# 25-plan.md §4.3's "under-merge/be conservative by default" philosophy
# applied to this decision. `"corrects"` is therefore never reachable from
# this mapping -- it only reaches `resolve_text_fact` via a future §5.3
# caller that classifies it directly, never through this map.
_LAYA_TO_RESOLVE_KIND: dict[str, str] = {
    "duplicate": "duplicate",
    "updates": "newer_state",
    "contradicts": "contradicts",
    "extends": "extends",
    "unrelated": "unrelated",
}


def _map_laya_kind_to_resolve_kind(laya_kind: str) -> str:
    """Laya's 5-way `fact_update` answer -> `resolve_text_fact`'s 6-way
    `kind`. Raises `ValueError` on anything outside Laya's known vocabulary
    (a real integration returning something unexpected should fail loud,
    not silently fall through to some default kind)."""
    try:
        return _LAYA_TO_RESOLVE_KIND[laya_kind]
    except KeyError:
        raise ValueError(f"unknown Laya fact_update kind: {laya_kind!r}") from None


# ---------------------------------------------------------------------------
# `windows_disjoint` -- pure, no graph/Laya dependency at all.
# ---------------------------------------------------------------------------


def windows_disjoint(
    old_valid_at: str | None,
    old_invalid_at: str | None,
    new_valid_at: str | None,
    new_invalid_at: str | None,
) -> bool:
    """True if the two `[valid_at, invalid_at)`-shaped intervals share no
    instant. Either end being `None` means unbounded in that direction (no
    start = always already begun; no end = never ends).

    Two half-open intervals `[a_start, a_end)` and `[b_start, b_end)` are
    disjoint iff `a_end <= b_start` or `b_end <= a_start` -- the standard
    interval-disjointness test, with `None` treated as +/-infinity on
    whichever side it appears.
    """
    old_start, old_end = parse_iso(old_valid_at), parse_iso(old_invalid_at)
    new_start, new_end = parse_iso(new_valid_at), parse_iso(new_invalid_at)
    if old_start is not None and new_end is not None and new_end <= old_start:
        return True
    if new_start is not None and old_end is not None and old_end <= new_start:
        return True
    return False


# ---------------------------------------------------------------------------
# `link_disputed` -- writes the DISPUTED_WITH edge between the two Decision
# nodes carrying the conflicting facts.
# ---------------------------------------------------------------------------


class UnsupportedDisputeError(NotImplementedError):
    """Raised when `link_disputed` is asked to connect two facts where
    neither side is identifiably a Decision-Decision pair -- the merged
    `DISPUTED_WITH` axiom (`graph/axioms.py`) is scoped `Decision`-`Decision`
    only, and writing an edge between kinds it doesn't allow would violate
    the ontology (this task's own instruction). See QUERIES.md's axiom-work
    entry for the open question about widening this."""


def _decision_endpoint(from_uid: str, from_label: str, to_uid: str, to_label: str) -> str | None:
    """Which of the two endpoints is the `Decision` node, or `None` if
    neither is. `graph/ontology.py::RELATION_TYPE_MAP` only ever puts
    `Decision` as the *subject* for the relations `resolve_text_fact`
    conflicts over (`APPLIES_TO`, `CAVEAT_OF`, `DEFINES`, `DECIDED_BY`), so
    `from_uid` is the expected hit in practice -- checking both sides keeps
    this correct even if that convention is ever widened."""
    if from_label == "Decision":
        return from_uid
    if to_label == "Decision":
        return to_uid
    return None


def link_disputed(graph: Graph, old: OldFact, new: NewFact) -> None:
    """§5.4: write the (merged, structural, non-extractable) `DISPUTED_WITH`
    edge between the two DECISION NODES that carry the conflicting facts --
    not between the fact edges themselves, and not between `old`/`new`'s
    other endpoint (the shared target both facts are about).

    Written the same single-directed-edge-plus-undirected-read convention
    `graph/resolver.py` already uses for the other symmetric relation in
    this ontology (`SAME_AS`: written one direction, read with an
    undirected `-[:SAME_AS*0..4]-` pattern in `graph/structured_query.py`) --
    not both directions, so this stays consistent with that precedent
    rather than inventing a second symmetric-write convention. `MERGE`
    (not `CREATE`) makes calling this twice on the same pair a no-op rather
    than a duplicate edge.

    Raises `UnsupportedDisputeError` if neither `old` nor `new` has a
    `Decision`-labelled endpoint -- per this task's instruction, writing a
    `DISPUTED_WITH` edge between kinds the axiom doesn't allow would violate
    the ontology, so this fails loud instead.
    """
    old_decision = _decision_endpoint(old.from_uid, old.from_label, old.to_uid, old.to_label)
    new_decision = _decision_endpoint(new.from_uid, new.from_label, new.to_uid, new.to_label)
    if old_decision is None or new_decision is None:
        raise UnsupportedDisputeError(
            f"link_disputed: DISPUTED_WITH is Decision-Decision only (25-plan.md "
            f"§5.4 axiom). old rel={old.rel_type!r} (from_label={old.from_label!r}, "
            f"to_label={old.to_label!r}), new rel={new.rel_type!r} "
            f"(from_label={new.from_label!r}, to_label={new.to_label!r}) -- neither "
            "side is identifiably a Decision-Decision pair, so no edge was written."
        )
    if old_decision == new_decision:
        return  # the same Decision cannot meaningfully dispute itself
    graph.query(
        """
        MATCH (a {uid: $a}), (b {uid: $b})
        MERGE (a)-[r:DISPUTED_WITH]->(b)
        ON CREATE SET r.first_seen_at = $now
        ON MATCH SET r.last_confirmed_at = $now
        """,
        params={"a": old_decision, "b": new_decision, "now": w.now_iso()},
    )


# ---------------------------------------------------------------------------
# `find_conflict_candidates` -- the blocking (endpoint-scoped, not
# graph-wide) candidate query, 25-plan.md §5.2's own Cypher sketch adapted
# to this codebase's real predicate/labels.
# ---------------------------------------------------------------------------


def find_conflict_candidates(
    graph: Graph,
    subject_uid: str,
    object_uid: str,
    rel_type: str,
    new_fact_uid: str,
    *,
    axioms: AxiomSet = DEFAULT_AXIOMS,
) -> list[OldFact]:
    """Live facts that could conflict with an incoming
    `(subject_uid, rel_type, object_uid)` fact, found by bounded
    (never graph-wide) lookups:

      - every live `rel_type` edge pointing at the same `object_uid`
        (regardless of subject) -- "something else already asserts this
        about the same target";
      - if `rel_type` is single-valued per the axioms (`functional=True`),
        every live `rel_type` edge out of the same `subject_uid`
        (regardless of object) -- "this subject can only have one live
        value of this relation, and it already has a different one";
      - for Decisions specifically, every live `APPLIES_TO` edge from any
        Decision onto the same `object_uid` -- 25-plan.md par 5.2's own
        instruction ("For Decisions, also include live Decisions that
        APPLIES_TO the same target"), independent of `rel_type`.

    The new fact's own `fact_uid` is always excluded from every branch.

    Implementation note: the plan's own Cypher sketch chains these with
    `UNION`. This codebase has no existing precedent for multi-branch
    `UNION` against FalkorDB (grepped: none) and this task's sandbox has no
    reachable FalkorDB instance to verify syntax/column-matching behavior
    against before shipping it, so this runs each branch as its own small,
    bounded `graph.query()` call and merges/de-dupes the results in Python
    by `fact_uid` instead -- same net effect ("blocking, not a graph-wide
    search", each query still scoped to one endpoint), fewer unverified
    moving parts.
    """
    return_clause = (
        "RETURN r.fact_uid, s.uid, labels(s)[0], o.uid, labels(o)[0], type(r), "
        "r.valid_at, r.pinned, r.source_record_keys, r.evidence, r.confidence, "
        "r.extraction_method"
    )
    candidates: dict[str, OldFact] = {}

    def _collect(cypher: str, params: dict[str, Any]) -> None:
        for row in graph.query(cypher, params=params).result_set:
            fact_uid = row[0]
            if fact_uid in candidates:
                continue
            candidates[fact_uid] = OldFact(
                fact_uid=fact_uid, from_uid=row[1], from_label=row[2],
                to_uid=row[3], to_label=row[4], rel_type=row[5],
                valid_at=row[6], invalid_at=None, pinned=bool(row[7]),
                source_record_keys=row[8], evidence=row[9],
                confidence=row[10], extraction_method=row[11],
            )

    _collect(
        f"""
        MATCH (s)-[r:{w._label(rel_type)}]->(o {{uid: $object_uid}})
        WHERE r.invalid_at IS NULL AND r.fact_uid <> $new_fact_uid
        {return_clause}
        """,
        {"object_uid": object_uid, "new_fact_uid": new_fact_uid},
    )

    functional = any(axiom.functional for axiom in axioms.for_relation(rel_type))
    if functional:
        _collect(
            f"""
            MATCH (s {{uid: $subject_uid}})-[r:{w._label(rel_type)}]->(o)
            WHERE r.invalid_at IS NULL AND r.fact_uid <> $new_fact_uid
            {return_clause}
            """,
            {"subject_uid": subject_uid, "new_fact_uid": new_fact_uid},
        )

    _collect(
        f"""
        MATCH (s:Decision)-[r:APPLIES_TO]->(o {{uid: $object_uid}})
        WHERE r.invalid_at IS NULL AND r.fact_uid <> $new_fact_uid
        {return_clause}
        """,
        {"object_uid": object_uid, "new_fact_uid": new_fact_uid},
    )

    return list(candidates.values())


# ---------------------------------------------------------------------------
# `resolve_text_fact` -- the dispatcher. Needs NO Laya dependency: `kind`
# is already classified by the time this runs (by `classify_fact_update`
# for today's five reachable kinds, or a future §5.3 caller for `corrects`).
# ---------------------------------------------------------------------------


def _write_incoming_fact(graph: Graph, new: NewFact, *, projection_status: str = "live") -> str:
    """Write `new` via `upsert_fact_edges(..., revive=False)` -- text facts
    must never silently reopen a closed edge (§5.0's own rule). If
    `new.invalid_at` was set by the caller (the "incoming historical fact"
    backfill branch below), the fact is written live first (that's the only
    shape `upsert_fact_edges` can create) and then immediately closed at
    that instant via `close_fact`, so it never appears live even for the
    instant between the two calls to any reader that isn't mid-transaction
    with this one -- FalkorDB gives no cross-statement isolation guarantee
    either way, so this is documented, not hidden.
    """
    fact_uid = new.fact_uid or w.make_uid("Fact", new.from_uid, new.rel_type, new.to_uid)
    row = {
        "from_uid": new.from_uid, "to_uid": new.to_uid,
        "source_record_keys": [new.source_record_key] if new.source_record_key else [],
        "evidence": new.evidence, "extraction_method": new.extraction_method or "llm",
        "confidence": new.confidence, "valid_at": new.valid_at, "fact_uid": fact_uid,
        "pinned": new.pinned, "decay_class": new.decay_class,
        "projection_status": projection_status,
    }
    w.upsert_fact_edges(graph, new.rel_type, new.from_label, new.to_label, [row], revive=False)
    if new.invalid_at is not None:
        still_live = graph.query(
            "MATCH ()-[r]->() WHERE r.fact_uid = $fact_uid RETURN r.invalid_at",
            params={"fact_uid": fact_uid},
        ).result_set
        if still_live and still_live[0][0] is None:
            w.close_fact(graph, fact_uid, new.invalid_at, reason="backfilled_before_existing")
    return fact_uid


def _resolve_conflict(
    graph: Graph, ledger: Any, new: NewFact, old: OldFact, mode: str, *, action: str,
) -> dict[str, Any]:
    """Shared suggest/auto handling for the three destructive branches
    (`newer_state` when old is superseded, `corrects`, `contradicts` /
    pinned-redirect) -- the ones the plan's own §5.2 text names as needing
    `NEURON_FACT_UPDATE_MODE` gating.

    `suggest` (default): does NOT call `close_fact`/`correct_fact`/
    `link_disputed` at all. Creates a `pending` review via
    `ledger.create_review` (same `type`/`payload`/`identity` shape
    `graph/semantic_pass.py`'s already-merged
    `_propose_polarity_conflict_review` uses for its own review type -- see
    that function for the pattern this mirrors) and writes the incoming
    fact with `projection_status="pending_review"` so it's excluded from
    live world-time reads (`graph/fact_predicates.py`) but durably recorded.

    `auto`: applies the real mutation. Ordering matters here for the
    partial-failure case (see this module's and `graph/writer.py`'s
    `set_projection_status` docstrings, and this task's final report for
    the full reasoning): the incoming fact is written `pending_review`
    FIRST, then the old fact's mutation runs, and only once that succeeds
    is the incoming fact promoted to `live`. A crash between steps 1 and 2
    leaves `old` still live and `new` merely pending (safe: nothing new is
    live). A crash between steps 2 and 3 leaves `old` closed/corrected/
    disputed and `new` still pending -- neither is live, which under-informs
    until the crash is noticed and retried, but never leaves two
    contradictory facts BOTH live at once, which is the one outcome this
    ordering is chosen to prevent.
    """
    identity = f"fact_update:{action}:{old.fact_uid}:{new.from_uid}:{new.rel_type}:{new.to_uid}"
    payload = {
        "action": action,
        "old_fact_uid": old.fact_uid, "old_from_uid": old.from_uid, "old_to_uid": old.to_uid,
        "old_rel_type": old.rel_type, "old_valid_at": old.valid_at,
        "new_from_uid": new.from_uid, "new_to_uid": new.to_uid, "new_rel_type": new.rel_type,
        "new_valid_at": new.valid_at, "new_source_time": new.source_time,
        "new_evidence": (new.evidence or "")[:500],
        "new_source_record_key": new.source_record_key,
    }
    if mode == "suggest":
        review_id = ledger.create_review("fact_update", payload, identity=identity)
        fact_uid = _write_incoming_fact(graph, new, projection_status="pending_review")
        return {
            "action": action, "mode": "suggest", "review_id": review_id,
            "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid,
        }

    # auto -- compensating-state ordering, see docstring above.
    fact_uid = _write_incoming_fact(graph, new, projection_status="pending_review")
    if action == "newer_state_close":
        w.close_fact(graph, old.fact_uid, valid_to=new.valid_at, reason="superseded")
    elif action == "corrects":
        w.correct_fact(graph, old.fact_uid, corrected_by=fact_uid, observed_to=new.source_time)
    elif action == "contradicts":
        link_disputed(graph, old, new)
    else:  # pragma: no cover - defensive, not a reachable action today
        raise ValueError(f"_resolve_conflict: unknown action {action!r}")
    w.set_projection_status(graph, fact_uid, "live")
    return {
        "action": action, "mode": "auto",
        "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid,
    }


def resolve_text_fact(
    graph: Graph,
    ledger: Any,
    new: NewFact,
    old: OldFact,
    kind: str,
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """25-plan.md §5.2 dispatcher. `ledger` (a `connectors.core.ledger.
    ConnectorLedger`) is a deliberate addition to the plan's own sketched
    signature (`resolve_text_fact(graph, new, old, kind, *, mode=None)`):
    `suggest` mode needs somewhere durable to record a proposed action, and
    `ledger.create_review` is that mechanism (§3.0, already merged) -- there
    is no way to implement suggest mode without a ledger handle, so this
    task adds the parameter rather than silently skipping review creation.

    `resolve_text_fact` performs the incoming fact's write itself (rather
    than returning a decision object for the caller to act on) -- chosen
    because every branch except `duplicate` needs to decide not just
    *whether* to write `new`, but with what `projection_status` and
    (for the historical-backfill branch) what `invalid_at`, and keeping
    that decision and its write together in one function is more
    self-contained than round-tripping a decision object through a second
    call site that would have to re-derive the same logic.

    Branch order matches 25-plan.md §5.2's own pseudocode exactly --
    `duplicate` / `extends` / `unrelated` are checked (and return) BEFORE
    the `old.pinned` check, so a pinned fact can still be confirmed as a
    duplicate; only kinds that would otherwise demote/close/correct a
    pinned fact get redirected to `link_disputed`.

    Returns a dict describing what happened: `{"action": ..., "old_fact_uid":
    ..., "new_fact_uid": ...}`, plus `"mode"`/`"review_id"` for the three
    gated branches. No caller in this codebase consumes this yet (wiring
    into `_write_extraction` is out of scope -- `graph/semantic_pass.py` is
    off-limits to this task); the return shape is designed for that future
    caller to log/assert against.
    """
    mode = mode or os.getenv("NEURON_FACT_UPDATE_MODE", "suggest")
    if mode not in _VALID_MODES:
        raise ValueError(f"NEURON_FACT_UPDATE_MODE must be one of {_VALID_MODES}, got {mode!r}")
    if mode == "auto":
        logger.warning(
            "resolve_text_fact: NEURON_FACT_UPDATE_MODE=auto is active. 25-plan.md "
            "§5.2 says auto is allowed only after review precision meets §10.4's bar "
            "(>= 0.95) -- that has not been measured in this codebase, and no real "
            "fact_update classifier is wired in yet either (classify_fact_update is "
            "a placeholder). Proceeding because auto was explicitly configured."
        )

    if kind == "duplicate":
        w.confirm_fact(
            graph, old.fact_uid, at=new.source_time,
            source_record_key=new.source_record_key,
            evidence=new.evidence, confidence=new.confidence,
        )
        return {"action": "duplicate", "old_fact_uid": old.fact_uid, "new_fact_uid": None}

    if kind in ("extends", "unrelated"):
        fact_uid = _write_incoming_fact(graph, new)
        return {"action": kind, "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid}

    if old.pinned:
        # DICE: pinned facts are never demoted, regardless of `kind` -- every
        # kind that reaches this point (contradicts, corrects, or what would
        # have been newer_state) is redirected to a dispute instead.
        return _resolve_conflict(graph, ledger, new, old, mode, action="contradicts")

    if kind == "contradicts":
        return _resolve_conflict(graph, ledger, new, old, mode, action="contradicts")

    if kind == "corrects":
        return _resolve_conflict(graph, ledger, new, old, mode, action="corrects")

    if kind != "newer_state":
        raise ValueError(f"resolve_text_fact: unknown kind {kind!r}")

    # newer_state (Graphiti date rule).
    if old.valid_at and new.valid_at:
        if windows_disjoint(old.valid_at, old.invalid_at, new.valid_at, new.invalid_at):
            fact_uid = _write_incoming_fact(graph, new)
            return {
                "action": "newer_state_disjoint",
                "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid,
            }
        if parse_iso(old.valid_at) < parse_iso(new.valid_at):
            return _resolve_conflict(graph, ledger, new, old, mode, action="newer_state_close")
        # Incoming historical fact is older than `old` -- it is written
        # already closed and must not revive/compete with the live edge.
        # `old` itself is untouched (there is nothing to close/correct/
        # dispute on it), so this branch is NOT suggest/auto-gated -- see
        # module docstring / final report for why only branches that would
        # mutate `old` are gated.
        new.invalid_at = old.valid_at
        fact_uid = _write_incoming_fact(graph, new)
        return {
            "action": "newer_state_backfill",
            "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid,
        }

    # Missing/out-of-order dates on one or both sides -- never guess.
    w.mark_ended_unknown(
        graph, old.fact_uid,
        attested_from=new.source_time, attested_by_record=new.source_record_key,
    )
    fact_uid = _write_incoming_fact(graph, new)
    return {"action": "ended_unknown", "old_fact_uid": old.fact_uid, "new_fact_uid": fact_uid}
