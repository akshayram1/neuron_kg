"""25-plan.md Phase 6 §6.4 — multi-signal duplicate collector for Decision
and Term.

Three independent pieces, kept apart on purpose (same shape as
`graph/resolve_text_fact.py`'s own split):

1. Pure scoring signals (`vector_similarity_score`, `lexical_overlap_score`,
   `shared_targets_score`, `shared_source_records_score`, `polarity_veto`)
   and the pure combiner (`duplicate_score`) -- each independently testable,
   none of them touching the graph except where the signal itself is
   inherently graph-shaped (`shared_targets_score`).
2. `find_duplicate_candidates` -- candidate generation: for every node of
   one label, pull `vector_store.search_above` neighbours and score each
   pair once (never both (A, B) and (B, A)).
3. `propose_duplicate_reviews` / `apply_approved_duplicate_merge` -- the
   review-queue boundary. Proposing never mutates the graph; applying an
   *already-approved* merge is the only place that does.

This module composes `graph/writer.py`'s already-merged primitives
(`upsert_fact_edges`, `invalidate_edges_by_uid_pairs`, `link_mentioned_in`,
`reinforce_count`) into merge-execution decision logic, the same way
`resolve_text_fact.py` composes `writer.py`'s §5.0 primitives -- `writer.py`
itself is read-only reference here, never edited.

WEIGHTS (25-plan.md §6.4 table, kept as named constants so they can be
retuned without touching signal logic):

    vector similarity      0.4
    lexical name overlap   0.2
    shared APPLIES_TO/DEFINES targets   0.2
    shared source_record_keys          0.2
    polarity veto                      veto (forces the pair out entirely)

NODE-LEVEL "REINFORCED" (judgment call, flagged in this task's final report
-- the plan says "most reinforced" for survivor selection but never defines
what that means for a NODE; `reinforce_count` in `graph/writer.py` is
defined only for a single fact edge's `source_record_keys`). Chosen
definition, conservative and reversible: the SUM of `reinforce_count`
across every live fact edge (any relation except `MENTIONED_IN`, either
direction) touching the node. Rationale: this is the closest node-level
analogue of "how much external evidence reinforces this node" that reuses
the already-merged, already-tested `reinforce_count` primitive exactly as
written rather than inventing a second definition of "reinforcement".
Tiebreak ("then oldest") reads `first_seen_at`, set once at node creation
and never overwritten (`graph/writer.py::upsert_entities`).

STALE PENDING REVIEWS AFTER A MERGE: `apply_approved_duplicate_merge`
operates on exactly the one approved pair it is given and does nothing else
-- it does not scan for, reject, or rewrite any other pending
`duplicate_pair` review that happens to mention the absorbed uid. Per this
task's instruction to pick the most conservative option when a judgment
call has no single obviously-correct answer: a pending review mentioning an
uid that has since been absorbed is left exactly as it was, for a human
reviewer to notice (its payload's uid still resolves to real node data --
the absorbed node is never deleted, only redirected -- so approving it
later fails softly, or a reviewer can consult `connectors.core.ledger.
ConnectorLedger.merged_into` to see it was already absorbed and reject it
instead). Auto-rejecting or auto-rewriting those reviews would be a second,
more complex judgment call the plan does not specify, better left to a
human than guessed at here.

RECOMPUTING CANDIDATES: "recompute remaining candidates after every
approved merge" (25-plan.md §6.4) is the CALLER's responsibility --
`find_duplicate_candidates` is not re-entrant/self-triggering. Re-run it
for the affected label after each `apply_approved_duplicate_merge` call so
candidates involving the absorbed uid are regenerated against the survivor
instead.

TRANSITIVITY: deliberately never applied. `apply_approved_duplicate_merge`
merges exactly the two uids named on the one approved review it is given --
nothing here ever chains A-B and B-C into a three-way merge, matches
25-plan.md §6.4's explicit "do not merge connected components transitively".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from falkordb import Graph

from connectors.core.ledger import ReviewState
from graph import vector_store
from graph import writer as w
from graph.fact_predicates import live_fact_cypher
from graph.semantic_pass import _normalize_identity, polarity_conflict
from storage.postgres import PostgresVectorClient

logger = logging.getLogger("neuron.duplicate_collector")

# --------------------------------------------------------------------- weights

# 25-plan.md §6.4 table. Named constants, not magic numbers, so retuning
# never touches the signal functions themselves. Must sum to 1.0.
WEIGHT_VECTOR_SIMILARITY = 0.4
WEIGHT_LEXICAL_OVERLAP = 0.2
WEIGHT_SHARED_TARGETS = 0.2
WEIGHT_SHARED_SOURCE_RECORDS = 0.2

# Labels §6.4 names for the collector.
CANDIDATE_LABELS = ("Decision", "Term")

DEFAULT_SIMILARITY_THRESHOLD = 0.8
DEFAULT_SCORE_THRESHOLD = 0.6
# The plan states an explicit ceiling for §6.2's link-candidate embedding
# search (10) but not for this call. 20 is chosen to match the ceiling
# already established for the same "vector-candidate shortlist per node"
# shape elsewhere in this codebase (`graph/semantic_pass.py`'s rung 4/5
# `search_above(..., limit=20)`), rather than inventing a third number.
DEFAULT_SEARCH_LIMIT = 20


# --------------------------------------------------------------------- signals


def vector_similarity_score(sim: float) -> float:
    """The raw cosine similarity from `search_above` IS the signal --
    weighting (`WEIGHT_VECTOR_SIMILARITY`) is applied by the caller
    (`duplicate_score`), not baked in here."""
    return sim


def lexical_overlap_score(name_a: str, name_b: str) -> float:
    """Jaccard similarity of the two names' token sets, using the exact
    same normalize-then-split convention `graph/semantic_pass.py`'s
    `_normalize_identity` already establishes for scoped identity keys and
    the mention filter -- reused directly here, not reimplemented, so this
    signal can never quietly drift from what "the same name" means
    elsewhere in this codebase."""
    tokens_a = set(_normalize_identity(name_a).split())
    tokens_b = set(_normalize_identity(name_b).split())
    return _jaccard(tokens_a, tokens_b)


def shared_targets_score(
    graph: Graph, uid_a: str, uid_b: str,
    relations: tuple[str, ...] = ("APPLIES_TO", "DEFINES"),
) -> float:
    """Jaccard similarity of the SET of target uids each node points to via
    `relations` (default `APPLIES_TO`/`DEFINES`, the two relations 25-plan.md
    §6.4 names -- both are valid outgoing relations from Decision and Term
    per `graph/ontology.py::RELATION_TYPE_MAP`). Live edges only, via
    `graph/fact_predicates.py`'s centralized live-fact predicate, never a
    bare `invalid_at IS NULL`."""
    return _jaccard(
        _outgoing_targets(graph, uid_a, relations),
        _outgoing_targets(graph, uid_b, relations),
    )


def shared_source_records_score(source_keys_a: list[str], source_keys_b: list[str]) -> float:
    """Jaccard of the two `source_record_keys` lists, as sets."""
    return _jaccard(set(source_keys_a or []), set(source_keys_b or []))


def polarity_veto(text_a: str, text_b: str) -> bool:
    """Literally `graph/resolve_text_fact.py`'s (in practice defined in and
    re-exported by `graph/semantic_pass.py`, where it is actually
    implemented -- see the note below) `polarity_conflict`, imported and
    reused, never reimplemented.

    NOTE ON LOCATION: this task's brief names `graph/resolve_text_fact.py`
    as `polarity_conflict`'s home. Verified against the real file: the
    function is defined in `graph/semantic_pass.py` (§4.1) and merely
    *used* by `resolve_text_fact.py` (see that module's `_resolve_conflict`
    docstring, which explicitly credits `_propose_polarity_conflict_review`
    in `semantic_pass.py` as the pattern it mirrors). Imported from its
    real location so this stays the same function, not a second copy under
    a different name.
    """
    return polarity_conflict(text_a, text_b)


def duplicate_score(
    vector_sim: float, lexical: float, targets: float, source_records: float, *, veto: bool,
) -> float | None:
    """Weighted sum of the four signals, or `None` (never a candidate) if
    `veto` is True. Weights are `WEIGHT_*` module constants, not inlined,
    so they can be retuned in one place."""
    if veto:
        return None
    return (
        WEIGHT_VECTOR_SIMILARITY * vector_sim
        + WEIGHT_LEXICAL_OVERLAP * lexical
        + WEIGHT_SHARED_TARGETS * targets
        + WEIGHT_SHARED_SOURCE_RECORDS * source_records
    )


def _jaccard(a: set, b: set) -> float:
    """0.0 for two empty sets (no shared evidence, not "identical by
    vacuous truth" -- the convention that matters most for
    `shared_source_records_score`, where two nodes that simply have no
    recorded source records must not score as if they were the same
    record)."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


# ------------------------------------------------------------- graph readers


def _outgoing_targets(graph: Graph, uid: str, relations: tuple[str, ...]) -> set[str]:
    rows = graph.query(
        f"""
        MATCH (n {{uid: $uid}})-[r]->(target)
        WHERE type(r) IN $relations AND {live_fact_cypher('r')}
        RETURN DISTINCT target.uid
        """,
        params={"uid": uid, "relations": list(relations)},
    ).result_set
    return {row[0] for row in rows if row[0]}


def _node_source_record_keys(graph: Graph, uid: str) -> list[str]:
    """The source records this node itself is `MENTIONED_IN` -- the only
    place a node-level notion of `source_record_keys` exists (fact EDGES
    carry `source_record_keys` directly; nodes do not, so this is the
    provenance-edge equivalent, same query shape as
    `graph/chat.py::_mentioned_record_keys` / `graph/entity.py`'s
    MENTIONED_IN reads, without their ACL/provider filtering since this is
    an internal hygiene computation, not a scoped read)."""
    rows = graph.query(
        """
        MATCH (n {uid: $uid})-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL
        RETURN DISTINCT sr.record_key
        """,
        params={"uid": uid},
    ).result_set
    return [row[0] for row in rows if row[0]]


def _node_name_and_text(graph: Graph, uid: str) -> tuple[str, str]:
    rows = graph.query(
        "MATCH (n {uid: $uid}) RETURN n.name, coalesce(n.search_text, n.name, '')",
        params={"uid": uid},
    ).result_set
    if not rows:
        return "", ""
    name, text = rows[0]
    return name or "", text or ""


def _label_nodes(graph: Graph, label: str) -> list[tuple[str, str, str]]:
    """(uid, name, text) for every node of `label`. `label` is only ever
    one of `CANDIDATE_LABELS` (validated by the one caller,
    `find_duplicate_candidates`), so direct interpolation is safe the same
    way other controlled-vocabulary label interpolation is throughout this
    codebase (see `graph/writer.py::_label`'s docstring)."""
    rows = graph.query(
        f"MATCH (n:{label}) RETURN n.uid, n.name, coalesce(n.search_text, n.name, '')"
    ).result_set
    return [(row[0], row[1] or "", row[2] or "") for row in rows]


def _node_embedding(
    client: Any, uid: str, *, collection: str = vector_store.COLLECTION,
) -> list[float] | None:
    """The node's own stored embedding, read back from the vector store by
    uid. `search_above` needs a query vector to search WITH; the only place
    a Decision/Term node's embedding actually lives is Qdrant/pgvector --
    `graph/writer.py::upsert_entity_embeddings` (which would write
    `n.embedding` onto the FalkorDB node itself) has no caller anywhere in
    this codebase (verified by grep across `graph/*.py`/`scripts/*.py`), so
    FalkorDB nodes never carry a usable `embedding` property in practice.

    `graph/vector_store.py` has no "fetch by id" accessor (deliberately --
    see its own module docstring on keeping the collection minimal), so
    this reads each backend directly: raw SQL through
    `PostgresVectorClient.store.connect()` for Postgres, `client.retrieve`
    for Qdrant. Not a new pattern -- `tests/test_vector_store.py` already
    reaches this far into both backends for its own setup/teardown; this is
    this module's read-side equivalent. Returns `None` if the node has no
    stored embedding yet (not every node has necessarily run through the
    Phase 4 embedding write path), and the caller skips it.
    """
    if isinstance(client, PostgresVectorClient):
        with client.store.connect() as connection:
            row = connection.execute(
                "SELECT content_embedding FROM entity_embeddings WHERE collection = %s AND uid = %s",
                (collection, uid),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        vector = row[0]
        return vector.to_list() if hasattr(vector, "to_list") else list(vector)
    records = client.retrieve(
        collection_name=collection, ids=[uid], with_vectors=[vector_store.CONTENT_VECTOR],
    )
    if not records or not records[0].vector:
        return None
    vector = records[0].vector
    if isinstance(vector, dict):
        vector = vector.get(vector_store.CONTENT_VECTOR)
    return list(vector) if vector is not None else None


# ------------------------------------------------------------ candidate pairs


@dataclass(frozen=True)
class DuplicateCandidate:
    """One scored, non-vetoed pair at or above `score_threshold` -- ready to
    become a review proposal. `uid_a < uid_b` always (sorted), so the same
    physical pair is represented identically regardless of which node's
    neighbourhood search discovered it."""

    label: str
    uid_a: str
    uid_b: str
    vector_similarity: float
    lexical_overlap: float
    shared_targets: float
    shared_source_records: float
    score: float


def find_duplicate_candidates(
    graph: Graph,
    client: Any,
    label: str,
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    search_limit: int = DEFAULT_SEARCH_LIMIT,
    collection: str = vector_store.COLLECTION,
) -> list[DuplicateCandidate]:
    """25-plan.md §6.4 candidate generation, for `label in {"Decision",
    "Term"}`. For each node of that label, `vector_store.search_above` at
    `similarity_threshold` (default 0.8) up to `search_limit` (default 20 --
    see `DEFAULT_SEARCH_LIMIT`'s docstring for why) raw candidates, each
    scored by the four weighted signals plus the polarity veto. Only pairs
    that clear `score_threshold` (default 0.6) and are not vetoed are
    returned -- this function's `score_threshold` parameter IS the "pairs
    >= 0.6 become independent review proposals" gate, not a looser filter
    a caller narrows later.

    Never returns the same unordered pair twice: `(A, B)` and `(B, A)` are
    the same physical pair (A's neighbourhood search finds B and B's finds
    A), deduped via a sorted-pair `seen` set across the whole label, not
    per-node.

    This function never touches the ledger or the graph beyond reads --
    proposing reviews is `propose_duplicate_reviews`'s job, kept separate
    so candidate generation stays independently testable and side-effect
    free.
    """
    if label not in CANDIDATE_LABELS:
        raise ValueError(
            f"find_duplicate_candidates: label must be one of {CANDIDATE_LABELS!r}, got {label!r}"
        )
    nodes = _label_nodes(graph, label)
    node_info = {uid: (name, text) for uid, name, text in nodes}
    seen_pairs: set[tuple[str, str]] = set()
    candidates: list[DuplicateCandidate] = []

    for uid, name, text in nodes:
        embedding = _node_embedding(client, uid, collection=collection)
        if embedding is None:
            continue
        hits = vector_store.search_above(
            client, label, embedding, similarity_threshold, limit=search_limit,
            collection=collection,
        )
        for other_uid, similarity in hits:
            if other_uid == uid:
                continue
            pair = (uid, other_uid) if uid < other_uid else (other_uid, uid)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            if other_uid in node_info:
                other_name, other_text = node_info[other_uid]
            else:
                # Defensive fallback -- search_above is label-filtered, so
                # this should not happen on real data, but a stale/foreign
                # vector-store row must not crash the whole collector run.
                other_name, other_text = _node_name_and_text(graph, other_uid)

            veto = polarity_veto(text, other_text)
            lexical = lexical_overlap_score(name, other_name)
            targets = shared_targets_score(graph, uid, other_uid)
            source_records = shared_source_records_score(
                _node_source_record_keys(graph, uid),
                _node_source_record_keys(graph, other_uid),
            )
            score = duplicate_score(
                vector_similarity_score(similarity), lexical, targets, source_records,
                veto=veto,
            )
            if score is None or score < score_threshold:
                continue
            candidates.append(DuplicateCandidate(
                label=label, uid_a=pair[0], uid_b=pair[1],
                vector_similarity=similarity, lexical_overlap=lexical,
                shared_targets=targets, shared_source_records=source_records,
                score=score,
            ))
    return candidates


# --------------------------------------------------------------- review queue


def propose_duplicate_reviews(ledger: Any, candidates: list[DuplicateCandidate]) -> list[int | None]:
    """`ledger.create_review("duplicate_pair", ...)` for every candidate,
    one review per pair. Identity is `duplicate_pair:{label}:{uid_a}:
    {uid_b}` over the already-sorted pair (`DuplicateCandidate.uid_a <
    uid_b` by construction), the same sorted-pair-identity convention
    `graph/semantic_pass.py`'s `_propose_polarity_conflict_review` /
    `graph/resolve_text_fact.py`'s `_resolve_conflict` already establish for
    their own review types, so `create_review`'s built-in rejection-identity
    cache (25-plan.md §3.0) means a rejected pair is never re-proposed and
    (A, B) can never be proposed twice under a different key ordering.

    "Decisions always require review": true by construction here -- every
    candidate this function is given becomes a `pending` review, unmodified,
    regardless of label. There is no separate auto-apply path for System/
    Term or any other label; nothing in this module ever mutates the graph
    on its own, only `apply_approved_duplicate_merge` does, and only for an
    already-APPROVED review. Confirmed while building this: no implied
    auto-path was found or skipped.

    Returns one review id per candidate, in the same order candidates was
    given (or `None` for an entry whose identity was already rejected --
    `create_review`'s own contract, not something this function adds).
    """
    review_ids: list[int | None] = []
    for candidate in candidates:
        identity = f"duplicate_pair:{candidate.label}:{candidate.uid_a}:{candidate.uid_b}"
        payload = {
            "label": candidate.label,
            "uid_a": candidate.uid_a,
            "uid_b": candidate.uid_b,
            "score": candidate.score,
            "signals": {
                "vector_similarity": candidate.vector_similarity,
                "lexical_overlap": candidate.lexical_overlap,
                "shared_targets": candidate.shared_targets,
                "shared_source_records": candidate.shared_source_records,
            },
        }
        review_ids.append(ledger.create_review("duplicate_pair", payload, identity=identity))
    return review_ids


# ------------------------------------------------------------- merge execution


def _node_reinforcement(graph: Graph, uid: str) -> int:
    """Node-level "reinforced" (see module docstring for the full
    rationale): sum of `graph/writer.py::reinforce_count` across every live
    fact edge (any relation except `MENTIONED_IN`, either direction)
    touching this node."""
    rows = graph.query(
        f"""
        MATCH (n {{uid: $uid}})-[r]-()
        WHERE type(r) <> 'MENTIONED_IN' AND {live_fact_cypher('r')}
        RETURN r.source_record_keys
        """,
        params={"uid": uid},
    ).result_set
    return sum(w.reinforce_count(row[0]) for row in rows)


def _node_first_seen_at(graph: Graph, uid: str) -> str | None:
    rows = graph.query(
        "MATCH (n {uid: $uid}) RETURN n.first_seen_at", params={"uid": uid},
    ).result_set
    return rows[0][0] if rows else None


def _choose_survivor(graph: Graph, uid_a: str, uid_b: str) -> tuple[str, str]:
    """"Most reinforced, then oldest" -- returns `(survivor_uid,
    absorbed_uid)`. `first_seen_at` is an ISO-8601 UTC string
    (`graph/writer.py::now_iso`), so plain string comparison already sorts
    chronologically; a missing `first_seen_at` never wins a tiebreak
    against a real timestamp."""
    reinforcement_a = _node_reinforcement(graph, uid_a)
    reinforcement_b = _node_reinforcement(graph, uid_b)
    if reinforcement_a != reinforcement_b:
        return (uid_a, uid_b) if reinforcement_a > reinforcement_b else (uid_b, uid_a)
    first_seen_a = _node_first_seen_at(graph, uid_a) or ""
    first_seen_b = _node_first_seen_at(graph, uid_b) or ""
    if first_seen_b and (not first_seen_a or first_seen_b < first_seen_a):
        return uid_b, uid_a
    return uid_a, uid_b


def _absorb_fact_edges(graph: Graph, label: str, survivor_uid: str, absorbed_uid: str) -> None:
    """Redirect every live fact edge (any relation except `MENTIONED_IN`)
    touching `absorbed_uid` onto `survivor_uid`.

    VERIFIED AGAINST REAL FALKORDB (not assumed): a relationship's endpoint
    cannot be retargeted in place -- there is no `SET` form for it. The real
    mechanism is exactly what this task's brief predicted: read the live
    edge's full property map (`properties(r)`), MERGE/create a fresh edge
    with the survivor as the moved endpoint via the already-merged
    `graph/writer.py::upsert_fact_edges` (drop the old `fact_uid` from the
    carried-over properties first so a fresh one is computed for the new
    (from, rel, to) triple -- `upsert_fact_edges` does this automatically
    whenever the row has no `fact_uid` key), then invalidate the original
    edge via `graph/writer.py::invalidate_edges_by_uid_pairs`. Both
    `graph/writer.py` primitives are called as-is, never modified.

    `source_record_keys` "merge (union, deduped)": when the survivor
    already has a live edge for the same (relation, other node) pair,
    `upsert_fact_edges`'s own `ON MATCH` branch appends the absorbed edge's
    `source_record_keys` that are not already present -- the union/dedup
    happens inside the already-merged primitive, not reimplemented here.

    An edge directly between the absorbed node and the survivor itself
    (e.g. a rare self-referencing APPLIES_TO) is left untouched rather than
    redirected into a meaningless self-loop -- `other.uid <> $survivor`
    excludes it from both queries below.
    """
    outgoing = graph.query(
        f"""
        MATCH (n {{uid: $absorbed}})-[r]->(other)
        WHERE type(r) <> 'MENTIONED_IN' AND {live_fact_cypher('r')} AND other.uid <> $survivor
        RETURN type(r), other.uid, labels(other)[0], properties(r)
        """,
        params={"absorbed": absorbed_uid, "survivor": survivor_uid},
    ).result_set
    for rel_type, other_uid, other_label, props in outgoing:
        row = dict(props)
        row.pop("fact_uid", None)
        row["from_uid"] = survivor_uid
        row["to_uid"] = other_uid
        w.upsert_fact_edges(graph, rel_type, label, other_label, [row])
        w.invalidate_edges_by_uid_pairs(
            graph, [{"from_uid": absorbed_uid, "to_uid": other_uid, "rel_type": rel_type}],
        )

    incoming = graph.query(
        f"""
        MATCH (other)-[r]->(n {{uid: $absorbed}})
        WHERE type(r) <> 'MENTIONED_IN' AND {live_fact_cypher('r')} AND other.uid <> $survivor
        RETURN type(r), other.uid, labels(other)[0], properties(r)
        """,
        params={"absorbed": absorbed_uid, "survivor": survivor_uid},
    ).result_set
    for rel_type, other_uid, other_label, props in incoming:
        row = dict(props)
        row.pop("fact_uid", None)
        row["from_uid"] = other_uid
        row["to_uid"] = survivor_uid
        w.upsert_fact_edges(graph, rel_type, other_label, label, [row])
        w.invalidate_edges_by_uid_pairs(
            graph, [{"from_uid": other_uid, "to_uid": absorbed_uid, "rel_type": rel_type}],
        )


def _absorb_mentions(graph: Graph, label: str, survivor_uid: str, absorbed_uid: str) -> None:
    """The survivor gains every `SourceRecord` the absorbed node was
    `MENTIONED_IN` (union -- `graph/writer.py::link_mentioned_in`'s `MERGE`
    is naturally idempotent, so this never duplicates an edge the survivor
    already has). The absorbed node's own `MENTIONED_IN` edges are left in
    place -- non-destructive, keeps its history intact as an audit
    artifact -- only the survivor is given the union."""
    keys = _node_source_record_keys(graph, absorbed_uid)
    if keys:
        w.link_mentioned_in(
            graph, label, [{"uid": survivor_uid, "record_key": key} for key in keys],
        )


def apply_approved_duplicate_merge(graph: Graph, ledger: Any, review_id: int) -> dict:
    """25-plan.md §6.4 merge execution: given an APPROVED `duplicate_pair`
    review, choose a survivor, absorb the loser's live fact edges and
    source records onto it, and record the merge trace.

    Operates on exactly the ONE approved pair named by `review_id` -- never
    walks connected components, never touches any other review (pending or
    otherwise). See the module docstring's "TRANSITIVITY" and "STALE
    PENDING REVIEWS" sections for why, and "RECOMPUTING CANDIDATES" for why
    re-running `find_duplicate_candidates` after this call is the caller's
    job, not this function's.

    Raises `ValueError` if `review_id` does not exist, is not a
    `duplicate_pair` review, or is not (yet) APPROVED -- mirrors
    `graph/writer.py::_find_live_fact`'s fail-loud-never-silent-no-op
    convention for a missing/wrong-shaped precondition.
    """
    review = ledger.get_review(review_id)
    if review is None:
        raise ValueError(f"apply_approved_duplicate_merge: no review with id={review_id!r}")
    if review.type != "duplicate_pair":
        raise ValueError(
            f"apply_approved_duplicate_merge: review {review_id} has type={review.type!r}, "
            "expected 'duplicate_pair'"
        )
    if review.state != ReviewState.APPROVED:
        raise ValueError(
            f"apply_approved_duplicate_merge: review {review_id} has state={review.state!r}, "
            "expected 'approved'"
        )

    label = review.payload["label"]
    uid_a = review.payload["uid_a"]
    uid_b = review.payload["uid_b"]
    survivor_uid, absorbed_uid = _choose_survivor(graph, uid_a, uid_b)

    _absorb_fact_edges(graph, label, survivor_uid, absorbed_uid)
    _absorb_mentions(graph, label, survivor_uid, absorbed_uid)
    merge_trace_id = ledger.record_merge_trace(
        survivor_uid, absorbed_uid, label, reason="duplicate_pair_merge",
    )
    logger.info(
        "duplicate_pair merge: review=%s label=%s survivor=%s absorbed=%s trace=%s",
        review_id, label, survivor_uid, absorbed_uid, merge_trace_id,
    )
    return {
        "review_id": review_id,
        "label": label,
        "survivor_uid": survivor_uid,
        "absorbed_uid": absorbed_uid,
        "merge_trace_id": merge_trace_id,
    }
