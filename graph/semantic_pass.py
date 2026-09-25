"""Jira semantic pass (plan.md §3 Pass B, Block 7) — budgeted LLM extraction
of Decision/Term/System entities and their facts from free text already
chunked and saved by the deterministic pass (Block 6). Structural facts
(assignee, status, project membership) are never re-derived here.

Endpoint resolution is deliberately conservative — verified against a real
extraction call during development, which showed the LLM's rendering of a
WorkItem's own name doesn't reliably match the node's actual `name` property
byte-for-byte (an em dash vs. a hyphen, in one observed case). Rather than
fuzzy-match names, this only resolves:
  - Decision/Term/System endpoints — by normalized name, always (they're
    cross-source entities by design, plan.md §5 Tier 1).
  - the chunk's OWN WorkItem/Project — via the ledger's `primary_node_uid`,
    which is exact by construction, never by name.
  - a Person — only if their name exactly matches (case-insensitive) an
    ASSIGNED_TO/REPORTED_BY neighbor already on that WorkItem.
Anything else (e.g. a different WorkItem referenced by name in free text)
is rejected, not guessed — plan.md: never fabricate an unverified link. That
harder case is exactly what Tier-3 similarity + LLM adjudication (plan.md §5)
is for, once there's a cross-source graph to adjudicate against.
"""

from __future__ import annotations

import logging
import os
import hashlib
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from falkordb import Graph
from openai import OpenAI

from connectors.core.ledger import (
    ConnectorLedger,
    DropReason,
    ExtractionDrop,
    PendingChunk,
    RecordEdgeRef,
    SemanticStatus,
)
from graph import vector_store
from graph import writer as w
from graph.axioms import SWAPPED, AxiomSet, DEFAULT_AXIOMS, load_axioms
from graph.dates import stated_dates
from graph.profiles import WorkManagementExtraction, profile_for_record_key
from graph.token_usage import TokenUsage

logger = logging.getLogger("neuron.semantic_pass")


# ---------------------------------------------------------------------------
# Admission gates (plan.md §3.4 "Admission gates as an explicit list").
#
# Every extracted fact is admitted (written as a live edge) only after
# passing, in this fixed order, the gates below. Each gate that rejects a
# fact records an `ExtractionDrop` (a `connectors.core.ledger.DropReason`)
# via `ledger.record_drops` and logs at INFO which gate number/name rejected
# it, so "why is this fact missing" always has an answer in the log/ledger
# (plan.md design principle #9).
#
#   1. Selective admission + optional Laya triage -- before `_call_llm`,
#      in `run_semantic_pass`. The "selective admission" half is real and
#      existing: a chunk only reaches `_call_llm` if its record already has
#      a resolved `primary_node_uid`, and only up to the run's
#      `budget`/`record_prefix` selection (`ledger.pending_chunks`). The
#      "optional Laya triage" half (skip a chunk by `chunk_type` /
#      `has_durable_fact`, plan.md §3.1 shadow mode / §3.2 enforce) is
#      blocked on an unresolved Laya-packaging decision -- see QUERIES.md
#      ("Phase 3.1/3.2 skipped this wave") and the `LayaReranker` stub in
#      `graph/rerank.py`. `_gate_laya_triage` below is a documented no-op
#      placeholder for it, called from the same place a real triage check
#      would run, but it never skips a chunk today.
#   2. Evidence verbatim       -- `evidence_in_chunk`, in `_write_extraction`
#      (exists).
#   3. Relation allowed / direction -- `axioms.resolve_direction`, in
#      `_write_extraction` (exists).
#   4. Merge candidate         -- the entity-resolution ladder (plan.md
#      Phase 4, §4.1). Not built yet; `_gate_merge_candidate` is a
#      documented no-op placeholder.
#   5. Conflict classification -- `resolve_text_fact` (plan.md Phase 5,
#      §5.2-5.3). Not built yet; `_gate_conflict_classification` is a
#      documented no-op placeholder.
#   6. Projection eligibility  -- low-confidence facts written as review
#      candidates rather than live edges (plan.md Phase 5). Not built yet;
#      `_gate_projection_eligibility` is a documented no-op placeholder.
#
# Gates 2 and 3 run per fact inside `_write_extraction`'s fact loop, in this
# order, before a fact's endpoints are resolved. Gates 4-6 are called
# immediately after endpoint resolution succeeds, in the same loop, one call
# site per gate in order -- the natural point where "is this really a new
# fact, not a duplicate" (4), "does it conflict with what's already live"
# (5), and "is it confident enough to write as a live edge rather than a
# review candidate" (6) belong once their real machinery exists. Wiring them
# in now, as no-ops, means Phase 4/5 has one obvious place to plug into
# instead of a second refactor of this admission path.
#
# This tuple exists so the order above is asserted by tests, not only
# described in the comment.
ADMISSION_GATES: tuple[str, ...] = (
    "1:selective_admission_and_laya_triage",
    "2:evidence_verbatim",
    "3:relation_allowed_direction",
    "4:merge_candidate",
    "5:conflict_classification",
    "6:projection_eligibility",
)


def _gate_laya_triage(chunk: PendingChunk) -> ExtractionDrop | None:
    """Gate 1 (optional sub-check): Laya `chunk_type` / `has_durable_fact`
    triage before `_call_llm` (plan.md Phase 3.1 shadow mode, Phase 3.2
    enforce). Blocked on an unresolved decision about how Laya is packaged
    for Neuron (vendored dependency vs. sidecar service -- see QUERIES.md,
    "Phase 3.1/3.2 skipped this wave", and the `LayaReranker` stub in
    `graph/rerank.py`).

    This is a placeholder, not a shadow-mode call: it always returns None
    (never skips a chunk) until Phase 3.1/3.2 are actually implemented.
    """
    return None


def _gate_merge_candidate(
    fact, subject_uid: str, object_uid: str,
) -> ExtractionDrop | None:
    """Gate 4: is this fact a duplicate of an already-live fact, to be
    reinforced rather than written as a new edge (plan.md Phase 4, the
    entity-resolution ladder in §4.1)?

    Not implemented: Phase 4's ladder (mention filter, polarity veto,
    normalized/alias/vector matching, Laya `same_entity`) does not exist in
    this codebase yet. Always returns None (never rejects a fact) until
    Phase 4 lands and this is replaced with the real check.
    """
    return None


def _gate_conflict_classification(
    fact, subject_uid: str, object_uid: str,
) -> ExtractionDrop | None:
    """Gate 5: does this fact conflict with a live fact on the same
    subject/relation/object (duplicate, extends, newer_state, corrects,
    contradicts), per `resolve_text_fact` (plan.md Phase 5, §5.2-5.3)?

    Not implemented: Phase 5's temporal-fact machinery (`valid_at_basis`,
    Laya `fact_update`, the Graphiti-derived date rule) does not exist in
    this codebase yet. Always returns None (never rejects a fact) until
    Phase 5 lands and this is replaced with the real check.
    """
    return None


def _gate_projection_eligibility(
    fact, subject_uid: str, object_uid: str,
) -> ExtractionDrop | None:
    """Gate 6: is this fact confident enough to write as a live edge, or
    should it be written as a review candidate instead (plan.md Phase 5,
    the low-confidence-as-candidate rule)?

    Not implemented: the confidence/candidate-projection machinery this
    depends on is part of Phase 5 and does not exist in this codebase yet.
    Always returns None (never rejects a fact) until Phase 5 lands and this
    is replaced with the real check.
    """
    return None


def evidence_in_chunk(evidence: str | None, chunk_text: str) -> bool:
    """True when the claimed quote actually appears in the source chunk.

    The extraction schema asks for a verbatim span. Constrained decoding
    still lets a weak model paraphrase; that would silently break provenance
    if we stored it. Empty evidence is treated as missing, not verbatim.
    """
    if not evidence or not evidence.strip():
        return False
    needle = " ".join(evidence.casefold().split())
    haystack = " ".join(chunk_text.casefold().split())
    return bool(needle) and needle in haystack

_SEMANTIC_LABELS = {"Decision", "Term", "System", "Api", "Endpoint"}
_ENTITY_TYPE_TO_LABEL = {
    "work_item": "WorkItem", "project": "Project", "repository": "Repository",
    "source_file": "SourceFile", "commit": "Commit", "pull_request": "PullRequest",
    "page": "Document", "workspace": "Workspace",
}
# Must match graph.schema.VECTOR_LABELS -- System has no vector index (it's
# usually just a proper noun with little embeddable text), Project isn't a
# content-bearing label either.
_EMBEDDABLE_LABELS = {
    "WorkItem", "Document", "Decision", "Term", "Api", "Endpoint",
    "PullRequest", "Commit", "SourceFile",
}


def _embed(
    client: OpenAI, model: str, texts: list[str], token_usage: TokenUsage,
) -> list[list[float]]:
    if not texts:
        return []
    response = client.embeddings.create(
        model=model, input=[vector_store.truncate_for_embedding(t) for t in texts]
    )
    token_usage.add(response.usage)
    return [item.embedding for item in response.data]


def semantic_uid(kind: str, name: str) -> str:
    """Same name -> same uid regardless of which record mentioned it, so
    entities merge across sources instead of duplicating (plan.md §5 Tier 1)."""
    return w.make_uid(kind, name.strip().lower())


def _record_own_kind(record_key: str) -> str | None:
    # record_key = "provider:connection_id:entity_type:external_id" and
    # external_id itself may contain ':' (e.g. "cloud1:10045") -- maxsplit=3
    # keeps the first three fields exact regardless.
    parts = record_key.split(":", 3)
    return _ENTITY_TYPE_TO_LABEL.get(parts[2]) if len(parts) >= 3 else None


# ---------------------------------------------------------------------------
# Phase 4 -- entity resolution ladder (25-plan.md §4.0-§4.8).
#
# Everything below replaces the old `find_similar_uid(...) or
# semantic_uid(...)` two-line identity resolution for the five
# `_SEMANTIC_LABELS` kinds with the scoped, six-rung ladder the plan
# specifies. See `_resolve_semantic_entity` for the ladder itself; the
# smaller pieces above it are its building blocks (§4.0 scoped identity,
# §4.1 polarity veto, §4.5 mention filter, §4.0's namespace derivation).


def _normalize_identity(value: object) -> str:
    """Casefold, collapse runs of non-alphanumerics to a single space, strip
    -- the same style as `_candidate_identity_matches`'s local `normalized()`
    closure below, reused here (not reinvented) for §4.0's scoped identity
    keys and §4.5's mention filter."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


# §4.1: "Before any merge (vector or Laya), compare negation markers in the
# two texts. If polarity_conflict, never merge." Verbatim from the plan.
_NEG = re.compile(
    r"\b(not|no longer|don't|do not|never|instead of|rejected|avoid|deprecated|stop(ped)? using)\b",
    re.I,
)


def polarity_conflict(a: str, b: str) -> bool:
    return bool(_NEG.search(a)) != bool(_NEG.search(b))


def _namespace_uid_for_record(
    graph: Graph, record_own_kind: str | None, record_primary_uid: str,
) -> str | None:
    """The structural half of §4.0's namespace derivation: reuses the exact
    same BELONGS_TO/CONTAINS traversal `_resolve_endpoint`'s Project /
    Repository / Workspace structural-fallback branches below already use to
    go from a record to its Project/Repository/Workspace, rather than a
    second path to the same graph shape. Returns `None` when nothing
    structural applies -- the caller (`_derive_namespace_uid`) supplies the
    workspace/connection-level fallback the plan asks for in that case."""
    if record_own_kind in {"Project", "Workspace", "Repository"}:
        # The record itself IS the namespace-bearing node.
        return record_primary_uid
    if record_own_kind == "WorkItem":
        rows = graph.query(
            "MATCH (n {uid: $uid})-[:BELONGS_TO]->(p:Project) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        ).result_set
        if rows:
            return rows[0][0]
    elif record_own_kind in {"SourceFile", "Commit"}:
        rows = graph.query(
            "MATCH (p:Repository)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        ).result_set
        if rows:
            return rows[0][0]
    elif record_own_kind == "Document":
        rows = graph.query(
            "MATCH (p:Workspace)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        ).result_set
        if rows:
            return rows[0][0]
    return None


def _derive_namespace_uid(
    graph: Graph, record_key: str, record_own_kind: str | None, record_primary_uid: str,
) -> str:
    """§4.0: "Use project when the source maps unambiguously to one project;
    otherwise use workspace/connection namespace." Tries the structural path
    first (the record's own Project/Repository/Workspace); when nothing
    structural applies, falls back to the connection-level scope
    (`provider:connection_id`), parsed from `record_key` the same way
    `_record_own_kind` parses it ("provider:connection_id:entity_type:
    external_id")."""
    structural = _namespace_uid_for_record(graph, record_own_kind, record_primary_uid)
    if structural:
        return structural
    parts = record_key.split(":", 3)
    return ":".join(parts[:2]) if len(parts) >= 2 else record_key


def _passes_mention_filter(ledger: ConnectorLedger, label: str, name: str) -> bool:
    """Rung 1 of the resolution ladder (§4.2, §4.5), `Term`/`System` only: a
    mention that is just ingestion-machinery vocabulary ("data", "pipeline",
    ... -- the ledger-backed stoplist), or too short/generic to be a real
    entity, never mints a node. Every other label always passes -- the
    mention filter is scoped to exactly these two per §4.5."""
    if label not in {"Term", "System"}:
        return True
    norm = _normalize_identity(name)
    tokens = norm.split()
    if len(norm) < 3 or not tokens:
        return False
    if ledger.is_stoplisted(norm, label=label):
        return False
    # "at least one non-generic token": a multi-word mention where EVERY
    # token is itself stoplisted ("data pipeline") is exactly as generic as
    # a bare stoplisted word, even though the full phrase isn't itself a
    # literal stoplist entry.
    if all(ledger.is_stoplisted(tok, label=label) for tok in tokens):
        return False
    return True


def _candidate_text(graph: Graph, uid: str) -> str:
    """The text a vector candidate is represented by, for the §4.1 polarity
    check -- same `search_text` (falling back to `name`) that
    `_write_extraction` stores on every semantic node, see its comment
    there for why `search_text` is the field to read."""
    rows = graph.query(
        "MATCH (n {uid: $uid}) RETURN n.search_text, n.name", params={"uid": uid},
    ).result_set
    if not rows:
        return ""
    search_text, name = rows[0]
    return str(search_text or name or "")


def _propose_polarity_conflict_review(
    ledger: ConnectorLedger, subject_uid: str, object_uid: str,
    mention_text: str, candidate_text: str,
) -> None:
    """§4.1: a polarity-vetoed Decision merge candidate is "handed to Phase 5
    as a conflict candidate." Phase 5 (temporal facts, `resolve_text_fact`)
    does not exist in this codebase yet -- nothing consumes this today, same
    "no consumer yet, but an auditable trail exists" shape as
    `_gate_conflict_classification`'s placeholder above -- but the
    already-merged §3.0 review queue means the pair is not silently lost.
    `subject_uid` is the new node about to be minted; `object_uid` is the
    existing node it was vetoed against, matching the `possibly_same_as`
    review payload convention (`subject_uid`/`object_uid`) already used
    elsewhere in this codebase.
    """
    ledger.create_review(
        "polarity_conflict_candidate",
        {
            "label": "Decision", "subject_uid": subject_uid, "object_uid": object_uid,
            "mention_text": mention_text[:500], "candidate_text": candidate_text[:500],
        },
        identity=f"polarity_conflict_candidate:{subject_uid}:{object_uid}",
    )


def _rung6_laya_same_entity(
    label: str, item: object, candidates: list[tuple[str, float]],
) -> tuple[str | None, float]:
    """Rung 6 (§4.2): Laya `same_entity`, scored against every candidate
    rungs 4/5 gathered (blocked by label + namespace). BLOCKED (placeholder):
    no real Laya call exists in this codebase yet -- the same unresolved
    Laya-packaging decision as `_gate_laya_triage`'s docstring above and the
    `LayaReranker` stub in `graph/rerank.py` (see QUERIES.md, "Phase 3.1/3.2
    skipped this wave").

    Always returns `(None, 0.0)` -- "no candidate reached top p >= 0.85 and
    top - second >= 0.15" -- so the ladder always falls through to a new
    node, `resolved_by="new"`. This is not just a stand-in for a missing
    call: it is also exactly what §4.3's "under-merge by default" policy
    wants even once Laya is real ("Thresholds are set so that doubt produces
    a new node"), so shipping the placeholder this way is never wrong, only
    incomplete.

    The `NEURON_RESOLVE_MODE=suggest/auto` branches and the
    `POSSIBLY_SAME_AS` review-proposal path §4.2 describes for a REAL Laya
    score are deliberately NOT scaffolded here as dead if/else branches --
    there is no score yet to gate them on, and an unreachable branch is
    untested noise, not readiness. When real Laya scoring lands, this
    function is the one place to fill in.
    """
    return None, 0.0


def _resolve_semantic_entity(
    graph: Graph,
    ledger: ConnectorLedger,
    label: str,
    item: object,
    vector: list[float] | None,
    *,
    namespace_uid: str,
    record_key: str,
    semantic_uids: dict[tuple[str, str, str], str],
    decision_name_index: dict[str, str],
    collection: str,
) -> tuple[str, str, bool]:
    """Rungs 2-6 of the Phase 4 scoped entity-resolution ladder (§4.2) for
    one Term/Decision/System/Api/Endpoint mention already past the rung-1
    mention filter (§4.5, applied by the caller -- a dropped mention never
    reaches this function).

    Returns `(uid, resolved_by, is_new)`. `resolved_by` is one of
    `scoped_exact` / `alias` / `vector` / `new` (§4.2's vocabulary --
    `laya`/`laya_suggest`/`review_required` never fire today, see
    `_rung6_laya_same_entity`). `is_new` is True exactly when `uid` was just
    minted rather than reused from an existing node.

    Every outcome is written into `semantic_uids`/`decision_name_index`
    before returning. Both dicts are owned and threaded through by
    `run_semantic_pass` (§4.4), not recreated per chunk, so a later mention
    in the SAME run -- this chunk or a later one -- resolves from memory
    instead of re-querying the graph/vector store.
    """
    name_norm = _normalize_identity(item.name)
    if label == "Decision":
        # §4.0: Decision identity is record + statement scoped, not
        # name-scoped -- a real behavior change from the old global
        # `semantic_uid("Decision", name)`.
        identity_norm = _normalize_identity(getattr(item, "statement", None) or item.name)
        scope = record_key
    elif label in {"System", "Term"}:
        # §4.0: namespace + normalized name.
        identity_norm = name_norm
        scope = namespace_uid
    else:
        # Api, Endpoint: "retain their current deterministic identity until
        # a type-specific scope is defined" (§4.0) -- unchanged from before.
        identity_norm = name_norm
        scope = ""
    key = (label, scope, identity_norm)

    def _remember(uid: str) -> None:
        semantic_uids[key] = uid
        if label == "Decision":
            # Facts only ever carry a bare name (`ExtractedFact.object_name`
            # etc, never a statement), so `_resolve_endpoint` cannot
            # reconstruct `key` (which needs the statement) to look a
            # Decision endpoint up by name alone. This name-keyed index is
            # the deliberate, documented fallback that keeps fact-endpoint
            # resolution for Decision working within a run -- see the
            # QUERIES note in the report this task produced.
            decision_name_index[name_norm] = uid

    # Rung 2: scoped identity hit.
    if key in semantic_uids:
        return semantic_uids[key], "scoped_exact", False
    if label in {"System", "Term"}:
        scoped_uid = w.make_uid(label, scope, identity_norm)
    elif label == "Decision":
        scoped_uid = w.make_uid("Decision", scope, identity_norm)
    else:
        scoped_uid = semantic_uid(label, item.name)
    rows = graph.query(
        "MATCH (n {uid: $uid}) RETURN n.uid LIMIT 1", params={"uid": scoped_uid},
    ).result_set
    if rows:
        _remember(scoped_uid)
        return scoped_uid, "scoped_exact", False

    # Rung 3: scoped alias table hit.
    alias_namespace = scope if label in {"System", "Term"} else None
    alias_uid = ledger.lookup_alias(label, alias_namespace, name_norm)
    if alias_uid:
        _remember(alias_uid)
        return alias_uid, "alias", False

    mention_text = _embedding_text(label, item)
    candidates: list[tuple[str, float]] = []
    if vector is not None:
        # Rung 4: sim >= 0.90.
        high = vector_store.search_above(
            vector_store.client(), label, vector, min_similarity=0.90,
            namespace_uid=namespace_uid, limit=20, collection=collection,
        )
        if len(high) == 1:
            candidate_uid, _score = high[0]
            candidate_text = _candidate_text(graph, candidate_uid)
            if not polarity_conflict(mention_text, candidate_text):
                _remember(candidate_uid)
                return candidate_uid, "vector", False
            # §4.1: polarity veto -- never merge; fall through to a new
            # node, and for a Decision, hand the pair to Phase 5.
            if label == "Decision":
                _propose_polarity_conflict_review(
                    ledger, scoped_uid, candidate_uid, mention_text, candidate_text,
                )
        else:
            # 0 or >1 candidates >= 0.90: rung 4 is not confident either way
            # -- "more than one" per §4.2 goes to rung 6 with all of them;
            # 0 falls through the same way. Rung 5's gray zone always goes
            # to rung 6 regardless, so it is queried here too.
            candidates.extend(high)
            candidates.extend(vector_store.search_above(
                vector_store.client(), label, vector, min_similarity=0.75,
                max_similarity=0.90, namespace_uid=namespace_uid, limit=20,
                collection=collection,
            ))

    # Rung 6: Laya `same_entity` -- placeholder, see its own docstring.
    top_uid, _confidence = _rung6_laya_same_entity(label, item, candidates)
    if top_uid is not None:  # pragma: no cover -- placeholder never returns a candidate today
        _remember(top_uid)
        return top_uid, "laya", False

    _remember(scoped_uid)
    return scoped_uid, "new", True


@dataclass
class SemanticPassResult:
    chunks_processed: int = 0
    llm_calls: int = 0
    entities_written: int = 0
    facts_written: int = 0
    facts_rejected: int = 0
    records_completed: int = 0
    findings_written: int = 0
    token_usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True)
class SemanticContext:
    text: str
    candidate_uids: frozenset[str] = frozenset()


def _resolve_endpoint(
    graph: Graph,
    kind: str,
    name: str,
    record_primary_uid: str,
    record_own_kind: str | None,
    semantic_uids: dict[tuple[str, str, str], str],
    candidate_uid: str | None = None,
    allowed_candidate_uids: frozenset[str] = frozenset(),
    *,
    namespace_uid: str = "",
    decision_name_index: dict[str, str] | None = None,
) -> str | None:
    if candidate_uid:
        if candidate_uid not in allowed_candidate_uids:
            return None
        rows = graph.query(
            "MATCH (n {uid: $uid}) RETURN labels(n), n.name, n.path, n.method, "
            "n.version, n.issue_key, n.sha, n.pr_ref",
            params={"uid": candidate_uid},
        ).result_set
        if rows and kind in rows[0][0] and _candidate_identity_matches(kind, name, rows[0][1:]):
            return candidate_uid
        return None
    if kind in _SEMANTIC_LABELS:
        # Mirrors `_resolve_semantic_entity`'s §4.0 scoped identity exactly,
        # so a fact endpoint resolves to the SAME uid the entity-writing
        # loop would (or did, earlier in this same run) mint for the same
        # mention -- see that function's docstring for the key shape.
        #
        # Decision is the one exception: `name` here is a fact's bare
        # `subject_name`/`object_name`, never the full statement §4.0 scopes
        # Decision identity by, so the scoped key cannot be reconstructed
        # from a name alone. `decision_name_index` (also run-owned, kept in
        # sync by `_resolve_semantic_entity._remember`) is the documented,
        # conservative fallback for exactly that gap -- see the QUERIES note
        # in this task's report.
        name_norm = _normalize_identity(name)
        if kind == "Decision":
            return (decision_name_index or {}).get(name_norm)
        scope = namespace_uid if kind in {"System", "Term"} else ""
        key = (kind, scope, name_norm)
        if key in semantic_uids:
            return semantic_uids[key]
        uid = (
            w.make_uid(kind, scope, name_norm) if kind in {"System", "Term"}
            else semantic_uid(kind, name)
        )
        rows = graph.query(
            "MATCH (n {uid: $uid}) RETURN n.uid LIMIT 1", params={"uid": uid}
        ).result_set
        return rows[0][0] if rows else None
    if kind == record_own_kind:
        return record_primary_uid
    if kind == "Person":
        result = graph.query(
            "MATCH (n {uid: $uid})-[:ASSIGNED_TO|REPORTED_BY|AUTHORED_BY]->(p:Person) "
            "WHERE toLower(p.name) = toLower($name) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid, "name": name},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Project" and record_own_kind == "WorkItem":
        result = graph.query(
            "MATCH (n {uid: $uid})-[:BELONGS_TO]->(p:Project) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Repository" and record_own_kind in {"SourceFile", "Commit"}:
        result = graph.query(
            "MATCH (p:Repository)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Workspace" and record_own_kind == "Document":
        result = graph.query(
            "MATCH (p:Workspace)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    return None


def _candidate_identity_matches(kind: str, proposed_name: str, properties: list) -> bool:
    """A same-kind vector candidate is not automatically the same entity.

    This guard prevents a retrieved ``POST /v1/auth`` Endpoint from being
    attached to new evidence that explicitly says ``POST /v2/auth``.
    """
    node_name, path, method, version, issue_key, sha, pr_ref = properties
    proposed = proposed_name.strip().casefold()
    if kind == "Endpoint":
        proposed_path = re.search(r"/v\d+/[a-z0-9_./-]+", proposed)
        if path:
            if not proposed_path:
                return proposed == str(node_name or "").strip().casefold()
            if proposed_path.group(0).rstrip(".,`") != str(path).casefold().rstrip(".,`"):
                return False
        proposed_method = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\b", proposed_name, re.I)
        return not proposed_method or not method or proposed_method.group(1).upper() == str(method).upper()
    if kind == "Api" and version:
        proposed_version = re.search(r"\bv\d+\b", proposed)
        if proposed_version:
            return proposed_version.group(0) == str(version).casefold()

    def normalized(value) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    proposed_norm = normalized(proposed_name)
    identities = [normalized(value) for value in (node_name, path, issue_key, sha, pr_ref) if value]
    return any(
        proposed_norm == identity
        or (min(len(proposed_norm), len(identity)) >= 8
            and (proposed_norm in identity or identity in proposed_norm))
        for identity in identities
    )



def _drop(reason: str, fact, detail: str | None = None) -> ExtractionDrop:
    """One discarded (or corrected) extraction, with enough of the original
    triple to judge it later without re-reading the source chunk."""
    return ExtractionDrop(
        reason=str(reason),
        subject_kind=fact.subject_kind, subject_name=fact.subject_name,
        relation=fact.relation,
        object_kind=fact.object_kind, object_name=fact.object_name,
        detail=str(detail)[:500] if detail else None,
    )


def _embedding_text(label: str, item) -> str:
    if label == "Decision":
        return " — ".join(filter(None, [item.name, item.statement, item.rationale]))
    if label == "Term":
        return " — ".join(filter(None, [item.name, item.definition]))
    if label == "Api":
        return " — ".join(filter(None, [item.name, item.version, item.status]))
    if label == "Endpoint":
        return " ".join(filter(None, [item.method, item.path])) or item.name
    return item.name


def _write_extraction(
    graph: Graph,
    ledger: ConnectorLedger,
    chunk: PendingChunk,
    extraction: WorkManagementExtraction,
    primary_uid: str,
    record_own_kind: str | None,
    client: OpenAI,
    embedding_model: str,
    extraction_model: str,
    profile_name: str,
    token_usage: TokenUsage,
    collection: str = vector_store.COLLECTION,
    axioms: AxiomSet = DEFAULT_AXIOMS,
    allowed_candidate_uids: frozenset[str] = frozenset(),
    *,
    run_id: str | None = None,
    semantic_uids: dict[tuple[str, str, str], str] | None = None,
    decision_name_index: dict[str, str] | None = None,
) -> tuple[int, int, int]:
    # §4.4/§4.6: `run_id`, `semantic_uids` and `decision_name_index` are
    # normally owned by `run_semantic_pass` and threaded through every chunk
    # of a run (so dedup memory spans the run, not just this chunk). The
    # `None` defaults exist only so direct callers (tests) don't have to
    # construct a run context -- each such call gets a fresh, run-of-one
    # scope, matching this function's behavior before Phase 4.
    if run_id is None:
        run_id = str(uuid.uuid4())
    if semantic_uids is None:
        semantic_uids = {}
    if decision_name_index is None:
        decision_name_index = {}
    namespace_uid = _derive_namespace_uid(graph, chunk.record_key, record_own_kind, primary_uid)
    source_rows = graph.query(
        "MATCH (sr:SourceRecord {record_key: $record_key}) RETURN sr.source_time LIMIT 1",
        params={"record_key": chunk.record_key},
    ).result_set
    source_time = source_rows[0][0] if source_rows else None
    # An entity with no fact connecting it to anything is graph noise, not
    # knowledge -- a floating "Redis" node nobody can query into is worse
    # than not having it at all. Rather than trust the LLM to always attach
    # a fact to every entity it names (unreliable in practice: real syncs
    # showed System/Term entities extracted with zero accompanying facts),
    # enforce it structurally: only keep an extracted entity if it actually
    # appears as a fact's subject or object. This makes "every semantic node
    # has a reason to exist" a property of the write path, not a prompt hope.
    drops: list[ExtractionDrop] = []
    referenced: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        # `resolve_direction`, not `is_relation_allowed`: a fact that is only
        # valid reversed still names real endpoints, and counting it as
        # unreferenced here would drop those entities before the write loop
        # below ever gets the chance to swap it.
        if axioms.resolve_direction(fact.subject_kind, fact.relation, fact.object_kind) is None:
            continue
        # A retrieved candidate UID means "attach to this existing node";
        # do not also create a second semantic node from the model's wording.
        if fact.subject_candidate_uid is None:
            referenced.add((fact.subject_kind, fact.subject_name.strip().lower()))
        if fact.object_candidate_uid is None:
            referenced.add((fact.object_kind, fact.object_name.strip().lower()))

    entities_written = 0
    edges_supported: list[RecordEdgeRef] = []
    for label, all_items in (
        ("Term", extraction.terms), ("Decision", extraction.decisions),
        ("System", extraction.systems), ("Api", extraction.apis),
        ("Endpoint", extraction.endpoints),
    ):
        items = []
        for item in all_items:
            # Rung 1 of the resolution ladder (§4.2, §4.5): Term/System only,
            # applied before the connecting-fact check below -- a generic
            # mention is rejected on its own terms, not because it happens
            # to lack a fact too.
            if not _passes_mention_filter(ledger, label, item.name):
                logger.info("  dropped %s (generic mention): %r", label, item.name)
                drops.append(ExtractionDrop(
                    reason=DropReason.GENERIC_MENTION,
                    subject_kind=label, subject_name=item.name,
                ))
                continue
            if (label, item.name.strip().lower()) in referenced:
                items.append(item)
            else:
                logger.info("  dropped %s (no connecting fact): %r", label, item.name)
                drops.append(ExtractionDrop(
                    reason=DropReason.ENTITY_NO_CONNECTING_FACT,
                    subject_kind=label, subject_name=item.name,
                ))
        if not items:
            continue

        if label in _EMBEDDABLE_LABELS:
            # Embed BEFORE deciding uid: a near-duplicate re-extraction (same
            # real thing, different LLM wording -- e.g. "use Redis-backed
            # sliding-window rate limiting" vs "...rate limiter" from two
            # near-identical tickets) won't share a normalized name, so
            # `semantic_uid` alone would silently create a second node for
            # the same real-world entity every time the wording drifts.
            # Verified against real duplicate tickets (see CHECKLIST).
            vectors = _embed(
                client, embedding_model,
                [_embedding_text(label, item) for item in items], token_usage,
            )
        else:
            vectors = [None] * len(items)

        # Rungs 2-6 of the resolution ladder (§4.2), one call per mention --
        # see `_resolve_semantic_entity`'s docstring for the full ladder.
        resolved = [
            _resolve_semantic_entity(
                graph, ledger, label, item, vector,
                namespace_uid=namespace_uid, record_key=chunk.record_key,
                semantic_uids=semantic_uids, decision_name_index=decision_name_index,
                collection=collection,
            )
            for item, vector in zip(items, vectors)
        ]
        uids = [uid for uid, _resolved_by, _is_new in resolved]
        for _uid, resolved_by, _is_new in resolved:
            # §4.6: diagnostics -- how this run's entities resolved, by
            # label x resolved_by. Rung-1 (GENERIC_MENTION) drops above are
            # deliberately not counted here: `resolution_stats` is about
            # outcomes for entities that WERE resolved, and a dropped
            # mention was never one of those.
            ledger.record_resolution(run_id, label, resolved_by)

        # `search_text` is the text this node is *represented by*, and two
        # separate things depend on it existing:
        #   - the FalkorDB fulltext (BM25) index is built on `search_text`, so
        #     without it a Decision/Term can never match the keyword leg of
        #     hybrid search — only the vector leg (observed: every Decision hit
        #     came back `methods=['vector']`, never 'fulltext').
        #   - `scripts/rebuild_vectors` re-embeds from `search_text`, so
        #     without it the Qdrant projection is NOT rebuildable for exactly
        #     the semantic entities that matter most (observed: a rebuild after
        #     wiping Qdrant reported "Decision: 0 nodes with text").
        # Storing the same string that was embedded keeps both in agreement.
        rows = [
            {
                "uid": uid,
                "props": {
                    **item.model_dump(exclude={"name"}),
                    "name": item.name,
                    "search_text": _embedding_text(label, item),
                    # §4.0: namespace_uid is stored explicitly on System/Term
                    # nodes -- their identity is scoped by it. Decision/Api/
                    # Endpoint are not namespace-scoped, so it's omitted for
                    # them rather than written as a misleading property.
                    **({"namespace_uid": namespace_uid} if label in {"System", "Term"} else {}),
                },
            }
            for uid, item in zip(uids, items)
        ]
        w.upsert_entities(graph, label, rows)
        w.link_mentioned_in(graph, label, [{"uid": row["uid"], "record_key": chunk.record_key} for row in rows])
        entities_written += len(rows)
        for (uid, _resolved_by, is_new), item in zip(resolved, items):
            merged_note = "" if is_new else " (merged into existing node)"
            logger.info("  extracted %s: %r%s", label, item.name, merged_note)

        # SourceRecord nodes are hidden from the product canvas. This visible
        # lineage edge keeps semantic knowledge attached to the Jira
        # WorkItem/Project it came from, yielding one connected project graph.
        if record_own_kind:
            lineage_rows = [
                {
                    "from_uid": uid,
                    "to_uid": primary_uid,
                    "source_record_keys": [chunk.record_key],
                    "evidence": None,
                    "extraction_method": "deterministic",
                    "confidence": 1.0,
                    "chunk_id": chunk.chunk_id,
                    "chunk_hash": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                    "extractor_version": profile_name,
                    "model": extraction_model,
                    "valid_at": source_time,
                }
                for uid in uids
            ]
            w.upsert_fact_edges(graph, "EXTRACTED_FROM", label, record_own_kind, lineage_rows)
            edges_supported.extend(
                RecordEdgeRef("EXTRACTED_FROM", uid, primary_uid) for uid in uids
            )

        if label in _EMBEDDABLE_LABELS:
            # The name channel gets the entity's bare name; `_embedding_text`
            # already put name + definition/statement into the content one.
            name_vectors = _embed(
                client, embedding_model, [item.name for item in items], token_usage,
            )
            vector_store.upsert_vectors(vector_store.client(), [
                {"uid": uid, "label": label, "embedding": vector,
                 "name_embedding": name_vector, "embedded_text": _embedding_text(label, item)[:400],
                 "embedded_model": embedding_model}
                for uid, item, vector, name_vector in zip(uids, items, vectors, name_vectors)
            ], collection=collection)

    facts_written = 0
    facts_rejected = 0
    for fact in extraction.facts:
        # Gate 2: evidence verbatim (ADMISSION_GATES[1]).
        if not evidence_in_chunk(fact.evidence, chunk.text):
            facts_rejected += 1
            logger.info(
                "  gate 2 (evidence_verbatim) rejected fact: (%s) %r -%s-> (%s) %r  evidence=%r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
                fact.evidence,
            )
            drops.append(_drop(DropReason.EVIDENCE_NOT_IN_CHUNK, fact, detail=fact.evidence))
            continue

        # Gate 3: relation allowed / direction (ADMISSION_GATES[2]).
        direction = axioms.resolve_direction(fact.subject_kind, fact.relation, fact.object_kind)
        if direction is None:
            facts_rejected += 1
            logger.info(
                "  gate 3 (relation_allowed_direction) rejected fact: (%s) %r -%s-> (%s) %r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
            )
            # Neither direction is in the ontology. The triple is kept here
            # with its evidence rather than written as some vague catch-all
            # relation: an unnamed relation is honest, an invented one is an
            # assertion nobody made.
            #
            # The evidence is the whole point of keeping it. Without the
            # sentence, a reviewer sees `Term GA -APPLIES_TO-> System Nilus`
            # and cannot tell whether the ontology is too narrow or the model
            # was wrong -- which is the one judgement this row exists to
            # support. (This argument was missing at first: 490 rows were
            # stored with no evidence at all.)
            drops.append(_drop(DropReason.RELATION_NOT_ALLOWED, fact, detail=fact.evidence))
            # ...and counted as a gap in the vocabulary, so "the ontology is
            # too narrow" becomes a number someone can act on rather than a
            # suspicion. A term seen once is noise; the same one seen forty
            # times is a missing relation.
            ledger.record_miss(
                "relation_type",
                f"{fact.subject_kind} -{fact.relation}-> {fact.object_kind}",
                example=f"{fact.subject_name} -> {fact.object_name}",
            )
            continue

        # The ontology says this relation runs the other way. Swap the
        # endpoints and say so -- never silently.
        subject_kind, subject_name = fact.subject_kind, fact.subject_name
        object_kind, object_name = fact.object_kind, fact.object_name
        subject_candidate_uid = fact.subject_candidate_uid
        object_candidate_uid = fact.object_candidate_uid
        if direction == SWAPPED:
            subject_kind, object_kind = object_kind, subject_kind
            subject_name, object_name = object_name, subject_name
            subject_candidate_uid, object_candidate_uid = object_candidate_uid, subject_candidate_uid
            logger.info(
                "  gate 3 (relation_allowed_direction) corrected direction: "
                "(%s) %r -%s-> (%s) %r  [as extracted: %s -> %s]",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
                fact.subject_kind, fact.object_kind,
            )
            drops.append(_drop(
                DropReason.DIRECTION_CORRECTED, fact,
                detail=f"written as ({subject_kind}) -{fact.relation}-> ({object_kind})",
            ))

        subject_uid = _resolve_endpoint(
            graph, subject_kind, subject_name,
            primary_uid, record_own_kind, semantic_uids,
            subject_candidate_uid, allowed_candidate_uids,
            namespace_uid=namespace_uid, decision_name_index=decision_name_index,
        )
        object_uid = _resolve_endpoint(
            graph, object_kind, object_name,
            primary_uid, record_own_kind, semantic_uids,
            object_candidate_uid, allowed_candidate_uids,
            namespace_uid=namespace_uid, decision_name_index=decision_name_index,
        )
        if subject_uid is None or object_uid is None:
            facts_rejected += 1
            logger.info(
                "  rejected fact (endpoint unresolved): (%s) %r -%s-> (%s) %r",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
            )
            drops.append(_drop(
                DropReason.ENDPOINT_UNRESOLVED, fact,
                detail=("subject" if subject_uid is None else "") +
                       ("+object" if object_uid is None else ""),
            ))
            continue

        # Gates 4-6 (ADMISSION_GATES[3:6]): documented no-op placeholders
        # (see their docstrings above) -- called here, once endpoints are
        # resolved but before the edge is written, so Phase 4/5 has this
        # exact call site to fill in rather than needing to find one.
        merge_drop = _gate_merge_candidate(fact, subject_uid, object_uid)
        if merge_drop is not None:  # pragma: no cover -- placeholder never rejects today
            facts_rejected += 1
            logger.info(
                "  gate 4 (merge_candidate) rejected fact: (%s) %r -%s-> (%s) %r",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
            )
            drops.append(merge_drop)
            continue
        conflict_drop = _gate_conflict_classification(fact, subject_uid, object_uid)
        if conflict_drop is not None:  # pragma: no cover -- placeholder never rejects today
            facts_rejected += 1
            logger.info(
                "  gate 5 (conflict_classification) rejected fact: (%s) %r -%s-> (%s) %r",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
            )
            drops.append(conflict_drop)
            continue
        projection_drop = _gate_projection_eligibility(fact, subject_uid, object_uid)
        if projection_drop is not None:  # pragma: no cover -- placeholder never rejects today
            facts_rejected += 1
            logger.info(
                "  gate 6 (projection_eligibility) rejected fact: (%s) %r -%s-> (%s) %r",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
            )
            drops.append(projection_drop)
            continue

        # §5.1 (25-plan.md "Stated vs record time"): parse this fact's own
        # verbatim evidence span for a stated start/end date, anchored to
        # the record's `source_time`. No LLM call -- `stated_dates` is a
        # pure regex/keyword parser (graph/dates.py, Phase 5.1, merged).
        #
        # `valid_at` is NEVER left null: a stated start takes it, otherwise
        # it falls back to `source_time` exactly as before this change --
        # `graph/time_axis.py` and every temporal query read `valid_at`, so
        # this is additive, not a semantic change (see 25-plan.md §5.1's own
        # "why not change valid_at semantics outright").
        stated = stated_dates(fact.evidence, source_time)
        if stated.start:
            valid_at = stated.start
            valid_at_basis = "stated"
        else:
            valid_at = source_time
            valid_at_basis = "record_time"
        ended_unknown = stated.end_stated_but_unresolved
        invalid_at = stated.end  # only set (below) when a real end date resolved

        fact_row: dict = {
            "from_uid": subject_uid, "to_uid": object_uid, "source_record_keys": [chunk.record_key],
            "evidence": fact.evidence, "extraction_method": "llm", "confidence": 0.9,
            "chunk_id": chunk.chunk_id,
            "chunk_hash": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
            "extractor_version": profile_name,
            "model": extraction_model,
            "valid_at": valid_at,
            "valid_at_basis": valid_at_basis,
            "ended_unknown": ended_unknown,
            "direction_corrected": direction == SWAPPED,
            # Stamped only when this triple entered the vocabulary through an
            # adoption. It is what makes the batch revertible: `unadopt` drops
            # the axiom rows, then deletes exactly the edges they let in --
            # edges from the seeded vocabulary carry NULL and are never touched.
            "adopted_batch": axioms.adopted_batch_for(subject_kind, fact.relation, object_kind),
        }
        if invalid_at is not None:
            fact_row["invalid_at"] = invalid_at
        w.upsert_fact_edges(graph, fact.relation, subject_kind, object_kind, [fact_row])

        # STOPGAP -- see QUERIES.md ("§5.1 valid_at_basis/invalid_at not yet
        # accepted by upsert_fact_edges"). Verified empirically (real
        # FalkorDB): `w.upsert_fact_edges`'s UNWIND/SET Cypher only reads the
        # row keys it explicitly names -- `valid_at_basis` and `invalid_at`
        # above are silently ignored (no error, but also never written) by
        # today's graph/writer.py, which is off-limits to this task (a
        # parallel §5.2 task owns it). Until writer.py grows two additive
        # ON CREATE/ON MATCH lines for these fields, persist them here
        # directly with a small supplementary write, matched by `fact_uid`
        # (same deterministic id `upsert_fact_edges` just computed) so this
        # feature's data is actually readable from the graph today. Skipped
        # entirely -- zero extra queries -- for the common case (no date
        # stated, no end resolved), which is today's exact unchanged
        # behavior. Delete this block once graph/writer.py accepts the
        # fields natively; `fact_row` above already needs no change then.
        if valid_at_basis == "stated" or invalid_at is not None:
            fact_uid = w.make_uid("Fact", str(subject_uid), fact.relation, str(object_uid))
            set_clause = "r.valid_at_basis = $valid_at_basis"
            supp_params = {
                "from_uid": subject_uid, "to_uid": object_uid, "fact_uid": fact_uid,
                "valid_at_basis": valid_at_basis,
            }
            if invalid_at is not None:
                set_clause += ", r.invalid_at = $invalid_at"
                supp_params["invalid_at"] = invalid_at
            graph.query(
                "MATCH (a {uid: $from_uid})-[r]->(b {uid: $to_uid}) "
                "WHERE r.fact_uid = $fact_uid "
                f"SET {set_clause}",
                params=supp_params,
            )

        edges_supported.append(RecordEdgeRef(fact.relation, subject_uid, object_uid))
        facts_written += 1
        logger.info(
            "  wrote fact: (%s) %r -%s-> (%s) %r  evidence=%r",
            subject_kind, subject_name, fact.relation, object_kind, object_name, fact.evidence,
        )

    if edges_supported:
        ledger.record_edges_batch(chunk.record_key, edges_supported)
    # Always written, even when empty: a chunk re-extracted after an ontology
    # change must clear the drops its previous run recorded, or the counts
    # describe a system that no longer exists.
    ledger.record_drops(chunk.record_key, chunk.chunk_id, drops)
    accepted_assessments = []
    for assessment in extraction.assessments:
        if not evidence_in_chunk(assessment.evidence, chunk.text):
            continue
        if any(uid not in allowed_candidate_uids for uid in assessment.related_candidate_uids):
            continue
        accepted_assessments.append(assessment.model_dump())
    recorder = getattr(ledger, "record_ingestion_assessments", None)
    if recorder is not None:
        recorder(chunk.record_key, chunk.chunk_id, accepted_assessments)
    return entities_written, facts_written, facts_rejected


def _call_llm(
    client: OpenAI, model: str, chunk: PendingChunk,
    related_context: str | SemanticContext | None = None,
):
    """The only part of a chunk's processing that's safe to run concurrently:
    a pure network round-trip with no graph/ledger side effects."""
    profile = profile_for_record_key(chunk.record_key)
    user_content = chunk.text
    context_text = related_context.text if isinstance(related_context, SemanticContext) else related_context
    if context_text:
        user_content += (
            "\n\n[RELATED EXISTING EVIDENCE + CANDIDATE NODES]\n"
            "The following small set was retrieved from knowledge that existed before "
            "this source update. Use it only to disambiguate identity and notice additions "
            "or conflicts. Do not extract a fact unless its verbatim evidence occurs in "
            "the NEW SOURCE above.\n\n" + context_text
        )
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": profile.instructions},
            {"role": "user", "content": user_content},
        ],
        text_format=profile.schema,
    )
    return profile, response


def run_semantic_pass(
    graph: Graph,
    ledger: ConnectorLedger,
    *,
    budget: int | None = None,
    client: OpenAI | None = None,
    model: str | None = None,
    record_prefix: str | None = None,
    on_progress: Callable[[int, int, str, SemanticPassResult], None] | None = None,
    max_concurrency: int | None = None,
    collection: str = vector_store.COLLECTION,
    context_provider: Callable[[PendingChunk], str | SemanticContext | None] | None = None,
) -> SemanticPassResult:
    """Process up to `budget` pending chunks (default: $LLM_BUDGET_PER_RUN).
    A chunk that fails its LLM call is left 'pending' and retried on a later
    run rather than dropped — no retry-count/backoff yet (known v1 gap: a
    persistently failing chunk keeps consuming one budget slot per run).

    LLM calls run concurrently (`LLM_CONCURRENCY`, default 6) -- per-chunk
    latency is 30-50s and almost entirely network wait, so this is the actual
    lever on wall-clock time (a smaller/denser chunk still costs about the
    same latency per call; only the number of *sequential* round-trips does).
    Every write -- the write-time similarity dedup in `_write_extraction`, the
    ledger commit -- stays on the main thread, one chunk at a time, in
    completion order: two chunks racing to decide "is this a new Decision or
    an existing one" concurrently could each conclude "new" and create a
    duplicate, since the first one's node/vector isn't visible to the second
    until it's actually written.
    """
    client = client or OpenAI()
    model = model or os.getenv("LLM_MODEL", "gpt-5.6-luna")
    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    budget = budget if budget is not None else int(os.getenv("LLM_BUDGET_PER_RUN", "200"))
    max_concurrency = max_concurrency or int(os.getenv("LLM_CONCURRENCY", "6"))
    # Read the vocabulary once per run, not once per fact: it is per-graph
    # data now (seeded from code on first use), so it can differ between
    # graphs and can be edited without a redeploy.
    axioms = load_axioms(ledger)

    # §4.4: dedup memory owned by the run, not the chunk. `run_id` (§4.6) is
    # generated once here -- `run_semantic_pass` had no existing run
    # identifier to reuse (checked: `SemanticPassResult`/the ledger's chunk
    # bookkeeping are keyed by record/chunk, not by run) -- and both caches
    # are threaded into every `_write_extraction` call below so an entity
    # resolved in an earlier chunk of this run is visible to a later one
    # without re-querying the graph/vector store. This is safe without
    # locking only because writes are serial on the main thread (verified
    # just above in this function's own docstring and in the
    # `ThreadPoolExecutor`/`as_completed` loop below: `_call_llm` is the only
    # part that runs concurrently; `_write_extraction` runs one chunk at a
    # time as futures complete).
    run_id = str(uuid.uuid4())
    semantic_uids: dict[tuple[str, str, str], str] = {}
    decision_name_index: dict[str, str] = {}

    result = SemanticPassResult()
    touched_records: set[str] = set()

    chunks = ledger.pending_chunks(budget, record_prefix=record_prefix)
    total_chunks = len(chunks)

    # Gate 1: selective admission + optional Laya triage (ADMISSION_GATES[0]),
    # run once per chunk before it is scheduled for `_call_llm`.
    runnable: list[tuple[PendingChunk, object, str | SemanticContext | None]] = []
    for chunk in chunks:
        entry = ledger.get(chunk.record_key)
        if entry is None or entry.primary_node_uid is None:
            logger.warning(
                "gate 1 (selective_admission_and_laya_triage) skipped chunk %s of %s: "
                "no primary_node_uid",
                chunk.chunk_id, chunk.record_key,
            )
            continue
        if _gate_laya_triage(chunk) is not None:
            # Unreachable today -- `_gate_laya_triage` is a documented no-op
            # placeholder (plan.md Phase 3.1/3.2, blocked on Laya packaging).
            # This branch exists so a real triage implementation has exactly
            # one place to plug into.
            logger.info(
                "gate 1 (selective_admission_and_laya_triage) skipped chunk %s of %s: "
                "laya triage",
                chunk.chunk_id, chunk.record_key,
            )
            continue
        related_context = context_provider(chunk) if context_provider else None
        runnable.append((chunk, entry, related_context))

    completed = 0
    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {
            pool.submit(_call_llm, client, model, chunk, related_context): (
                chunk, entry, related_context,
            )
            for chunk, entry, related_context in runnable
        }
        for future in as_completed(futures):
            chunk, entry, related_context = futures[future]
            result.llm_calls += 1
            try:
                profile, response = future.result()
            except Exception:
                logger.exception("LLM extraction failed for chunk %s of %s", chunk.chunk_id, chunk.record_key)
                continue
            result.token_usage.add(response.usage)
            extraction: WorkManagementExtraction = response.output_parsed

            entities, facts, rejected = _write_extraction(
                graph, ledger, chunk, extraction, entry.primary_node_uid,
                _record_own_kind(chunk.record_key), client, embedding_model, model, profile.name,
                result.token_usage, collection=collection, axioms=axioms,
                allowed_candidate_uids=(
                    related_context.candidate_uids
                    if isinstance(related_context, SemanticContext) else frozenset()
                ),
                run_id=run_id, semantic_uids=semantic_uids, decision_name_index=decision_name_index,
            )
            result.findings_written += sum(
                1 for item in extraction.assessments
                if item.should_flag
                and evidence_in_chunk(item.evidence, chunk.text)
                and all(uid in (
                    related_context.candidate_uids
                    if isinstance(related_context, SemanticContext) else frozenset()
                ) for uid in item.related_candidate_uids)
            )
            result.chunks_processed += 1
            result.entities_written += entities
            result.facts_written += facts
            result.facts_rejected += rejected
            logger.info(
                "chunk %s of %s: %d terms, %d decisions, %d semantic entities extracted, %d facts written, %d rejected",
                chunk.chunk_index, chunk.record_key,
                len(extraction.terms), len(extraction.decisions),
                len(extraction.systems) + len(extraction.apis) + len(extraction.endpoints),
                facts, rejected,
            )

            ledger.commit_chunk(chunk.record_key, chunk.chunk_id, SemanticStatus.DONE)
            touched_records.add(chunk.record_key)
            completed += 1
            if on_progress is not None:
                on_progress(completed, total_chunks, chunk.record_key, result)

    for record_key in touched_records:
        if not ledger.record_fully_processed(record_key):
            continue
        ledger.set_semantic_status(record_key, SemanticStatus.DONE)
        result.records_completed += 1

        # The record's own WorkItem gets one embedding over its full
        # search_text once semantic processing completes -- not per chunk,
        # since a multi-chunk record's embedding should represent the whole
        # thing, not one fragment.
        own_label = _record_own_kind(record_key)
        entry = ledger.get(record_key)
        if own_label in _EMBEDDABLE_LABELS and entry and entry.primary_node_uid:
            rows = graph.query(
                "MATCH (n {uid: $uid}) RETURN n.search_text, n.name",
                params={"uid": entry.primary_node_uid},
            ).result_set
            search_text = rows[0][0] if rows else None
            node_name = (rows[0][1] if rows else None) or search_text
            if search_text:
                vectors = _embed(
                    client, embedding_model, [search_text, node_name], result.token_usage,
                )
                vector_store.upsert_vectors(vector_store.client(), [{
                    "uid": entry.primary_node_uid, "label": own_label,
                    "embedding": vectors[0], "name_embedding": vectors[1],
                    "embedded_text": search_text[:400], "embedded_model": embedding_model,
                }], collection=collection)

    logger.info(
        "semantic pass done: %d chunks, %d llm calls, %d entities, %d facts (%d rejected), %d records completed",
        result.chunks_processed, result.llm_calls, result.entities_written,
        result.facts_written, result.facts_rejected, result.records_completed,
    )
    return result
