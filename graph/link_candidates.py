"""Phase 6 §6.2 — candidate links for isolated nodes (25-plan.md).

Two candidate sources, both producing *candidates*, never a direct graph
edge:

  - `find_two_hop_candidates`: DICE two-hop co-occurrence -- pairs (A, B),
    neither in `HUB_LABELS`, with no live edge between them, sharing a
    common non-hub neighbour Z (`Term`/`System`/`Decision`) whose own degree
    is small enough that it isn't a fan-out hub itself.
  - `find_semantic_candidates`: embedding similarity between an isolated
    `Document`/`Decision` and a `WorkItem`/`PullRequest`, via
    `graph.vector_store.search_above` (merged Phase 4).

Every candidate from either source is classified by Laya's trained
`relation_type` question (`LayaRelationClassifier`) before it is ever
written to `link_candidates` (`connectors/core/ledger.py`): only
`relation != "none"` with `confidence >= 0.6` gets persisted, with
`derived_rule` recording which source produced it and `confidence` fixed at
0.5 (DICE-neutral -- the classifier's own confidence gates *whether* a
candidate is written, it is not stored as the candidate's confidence; see
`_propose` below).

Approval is a separate step: `apply_approved_link_candidate` promotes one
`approved` row into a real graph edge, `derived=True`,
`extraction_method="derived"`, via `graph.writer.upsert_fact_edges(...,
revive=False)` -- never reopening a closed edge, matching that function's
own text-fact convention (25-plan.md §5.2).

Deliberately self-contained: this module does not import `graph/hygiene.py`
(a parallel, in-flight task) even though "isolated Document/Decision" is
also §6.1's concern there -- `_isolated_document_uids`/
`_isolated_decision_uids` below duplicate that small query rather than
create a cross-task import dependency on a module that may not exist yet or
whose shape may still change.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

import tiktoken
from falkordb import Graph
from qdrant_client import QdrantClient

from connectors.core.ledger import ConnectorLedger
from graph import writer as w
from graph.expand import HUB_LABELS
from graph.text_window import best_window
from graph.vector_store import CONTENT_VECTOR, COLLECTION, search_above
from storage.postgres import PostgresVectorClient

logger = logging.getLogger("neuron.link_candidates")

# derived_rule values written to link_candidates (plan.md §6.2).
TWO_HOP = "two_hop"
SEMANTIC_CANDIDATE = "semantic_candidate"

# The write gate (plan.md §6.2): "If relation != none and confidence >= 0.6".
CONFIDENCE_GATE = 0.6

# Laya's combined state/question sequence caps at 512 tokens (same
# constraint graph/rerank.py's DEFAULT_CANDIDATE_WINDOW_TOKENS documents);
# 300 leaves headroom for head/tail names plus the trained instruction.
_STATE_TEXT_WINDOW_TOKENS = 300
_encoding = tiktoken.get_encoding("cl100k_base")

# Relation types allowed to seed a two-hop bridge, plan.md §6.2: "a shared
# non-hub neighbour Z (Term, System, Decision)".
_TWO_HOP_Z_LABELS = ("Term", "System", "Decision")


# ----------------------------------------------------------------- two-hop


def find_two_hop_candidates(
    graph: Graph, *, degree_cap: int = 50,
) -> list[tuple[str, str, str]]:
    """DICE two-hop candidates (plan.md §6.2).

    Returns `(from_uid, to_uid, shared_z_uid)` triples where:
      - neither endpoint's label is in `HUB_LABELS` (imported from
        `graph/expand.py`, not redefined here);
      - the endpoints share at least one live, non-`MENTIONED_IN` edge to a
        common `Term`/`System`/`Decision` node Z;
      - Z's own degree (count of its own live relationships, any type,
        either direction) is `<= degree_cap` -- a Z with many neighbours is
        itself acting as a hub and would multiply nonsense bridges;
      - there is no live edge (any relation type, either direction) already
        between the endpoints;
      - `from_uid < to_uid` lexicographically, so (A, B) and (B, A) are the
        same candidate, never proposed twice, and self-pairs (A == A) are
        impossible.

    A pair sharing more than one qualifying Z produces one row per Z --
    intentional, matches the plan's per-source-Z candidate shape; callers
    that want one candidate per pair can dedupe on `(from_uid, to_uid)`
    themselves.
    """
    rows = graph.query(
        """
        MATCH (z) WHERE (z:Term OR z:System OR z:Decision)
        MATCH (a)-[r1]-(z)
        WHERE r1.invalid_at IS NULL AND type(r1) <> 'MENTIONED_IN'
          AND NOT labels(a)[0] IN $hub_labels
        MATCH (b)-[r2]-(z)
        WHERE r2.invalid_at IS NULL AND type(r2) <> 'MENTIONED_IN'
          AND NOT labels(b)[0] IN $hub_labels
          AND a.uid < b.uid
        OPTIONAL MATCH (a)-[existing]-(b)
        WHERE existing.invalid_at IS NULL
        WITH a, b, z, existing
        WHERE existing IS NULL
        RETURN DISTINCT a.uid, b.uid, z.uid
        """,
        params={"hub_labels": list(HUB_LABELS)},
    ).result_set

    triples = [(row[0], row[1], row[2]) for row in rows]
    if not triples:
        return []

    z_uids = list({z for _a, _b, z in triples})
    degree_rows = graph.query(
        """
        UNWIND $z_uids AS zuid
        MATCH (z {uid: zuid})-[r]-()
        WHERE r.invalid_at IS NULL
        RETURN z.uid, count(r)
        """,
        params={"z_uids": z_uids},
    ).result_set
    degree_by_z = {row[0]: row[1] for row in degree_rows}

    return [
        (a, b, z) for a, b, z in triples
        if degree_by_z.get(z, 0) <= degree_cap
    ]


# --------------------------------------------------------------- semantic


def _isolated_document_uids(graph: Graph) -> list[str]:
    """`Document` with no live outgoing `DOCUMENTS` edge (plan.md §6.1's
    definition, reimplemented locally -- see module docstring).

    The node MATCH and the relationship OPTIONAL MATCH are deliberately two
    separate clauses, not one combined `OPTIONAL MATCH (d:Document)-[e]->()`
    -- verified directly against this codebase's real FalkorDB: when the
    label+relationship pattern is combined into a single OPTIONAL MATCH,
    a Document with zero DOCUMENTS edges comes back with `d` itself null
    (not just `e`), silently dropping every isolated node instead of
    finding it. Binding `d` in its own `MATCH` first avoids that."""
    rows = graph.query(
        """
        MATCH (d:Document)
        OPTIONAL MATCH (d)-[e:DOCUMENTS]->()
        WHERE e.invalid_at IS NULL
        WITH d, count(e) AS live_out
        WHERE live_out = 0
        RETURN d.uid
        """
    ).result_set
    return [row[0] for row in rows]


def _isolated_decision_uids(graph: Graph) -> list[str]:
    """`Decision` with no live outgoing `APPLIES_TO` edge (plan.md §6.1).
    See `_isolated_document_uids` for why the node/relationship MATCHes are
    kept separate."""
    rows = graph.query(
        """
        MATCH (d:Decision)
        OPTIONAL MATCH (d)-[e:APPLIES_TO]->()
        WHERE e.invalid_at IS NULL
        WITH d, count(e) AS live_out
        WHERE live_out = 0
        RETURN d.uid
        """
    ).result_set
    return [row[0] for row in rows]


def _fetch_own_embedding(
    client: QdrantClient | PostgresVectorClient, uid: str, *, collection: str,
) -> list[float] | None:
    """The node's own already-computed content embedding, keyed by uid --
    NOT a fresh re-embed. `graph/vector_store.py` has no "fetch by uid"
    helper today (its functions all take a query vector, never look one up),
    so this reads each backend's storage directly, mirroring how
    `graph/vector_store.py::search_above` itself branches on
    `isinstance(client, PostgresVectorClient)`."""
    if isinstance(client, PostgresVectorClient):
        try:
            with client.store.connect() as connection:
                row = connection.execute(
                    "SELECT content_embedding FROM entity_embeddings "
                    "WHERE collection = %s AND uid = %s",
                    (collection, uid),
                ).fetchone()
        except Exception:
            logger.exception("pgvector own-embedding fetch failed for uid=%s", uid)
            return None
        return list(row[0]) if row else None
    try:
        records = client.retrieve(collection_name=collection, ids=[uid], with_vectors=True)
    except Exception:
        logger.exception("Qdrant own-embedding fetch failed for uid=%s", uid)
        return None
    if not records:
        return None
    vector = records[0].vector
    if isinstance(vector, dict):
        return vector.get(CONTENT_VECTOR)
    return vector


def find_semantic_candidates(
    graph: Graph,
    client: QdrantClient | PostgresVectorClient,
    *,
    similarity_threshold: float = 0.65,
    limit: int = 10,
    collection: str = COLLECTION,
) -> list[tuple[str, str, float]]:
    """Embedding candidates (plan.md §6.2): for each isolated
    `Document`/`Decision`, `WorkItem`s/`PullRequest`s above
    `similarity_threshold`, via `search_above` (Phase 4, merged), ceiling
    `limit`.

    `similarity_threshold` default 0.65: the plan gives exact calibrated
    numbers for rung 4 (0.90, near-duplicate) and rung 5 (0.75, gray zone)
    of the *entity resolution* ladder (§4.2), but explicitly leaves this
    threshold to judgment ("above a similarity threshold" only). 0.65 sits
    below both of those -- this is a *bridge suggestion* for a human
    reviewer, not an auto-merge decision, so it can afford to be looser than
    0.75 while still being a real filter, not a rubber stamp (a value near
    0 would return nearly everything). Flagged in QUERIES.md as a judgment
    call the repo owner may want to retune once real candidates are seen.

    Returns `(from_uid, to_uid, similarity)` triples, `from_uid` the
    isolated Document/Decision, `to_uid` the WorkItem/PullRequest,
    best-first, deduplicated by `to_uid` per `from_uid`, at most `limit`
    total per isolated node (both labels combined, each individually
    fetched up to `limit` and then merged/truncated -- so the ceiling is on
    the candidate's own fan-out, not the raw per-label query).

    `collection` defaults to `graph.vector_store.COLLECTION` (every real
    caller's implicit choice today) and is overridable -- mainly so tests
    can point this at an isolated scratch collection instead of the shared
    default.
    """
    isolated = [(uid, "Document") for uid in _isolated_document_uids(graph)]
    isolated += [(uid, "Decision") for uid in _isolated_decision_uids(graph)]
    if not isolated:
        return []

    results: list[tuple[str, str, float]] = []
    for uid, _label in isolated:
        embedding = _fetch_own_embedding(client, uid, collection=collection)
        if embedding is None:
            continue
        hits: list[tuple[str, float]] = []
        for target_label in ("WorkItem", "PullRequest"):
            hits.extend(search_above(
                client, target_label, embedding, min_similarity=similarity_threshold,
                limit=limit, collection=collection,
            ))
        hits.sort(key=lambda pair: pair[1], reverse=True)
        seen: set[str] = set()
        for target_uid, similarity in hits:
            if target_uid == uid or target_uid in seen:
                continue
            seen.add(target_uid)
            results.append((uid, target_uid, similarity))
            if len(seen) >= limit:
                break
    return results


# -------------------------------------------------------- Laya classification


class LayaRelationClassifier:
    """Lazy, process-local adapter for Laya's trained `relation_type`
    question (`personal_exp/laya/ingest/schema.py`'s
    `QUESTIONS["relation_type"]`).

    Real, not a stub -- deliberately, and a departure from this project's
    usual "raise NotImplementedError" posture for unverified Laya
    integration points (see `graph/resolve_text_fact.py::classify_fact_update`
    and `graph/rerank.py`'s docstrings for that default). This task
    independently verified, inside its own sandbox, that:
      1. the `laya` package imports (`pyproject.toml` already lists it,
         confirmed importable here, unlike the fact_update task's sandbox);
      2. a trained checkpoint carrying a `relation_type` question exists at
         `personal_exp/laya/model/laya-ingest/` (`questions.json` has the
         entry, verbatim-reproduced below as `RELATION_TYPE_QUESTION`);
      3. `laya.Agent(model_dir, device="cpu").predict_batch([state],
         {"relation_type": question}, batch_size=1)` against that real
         checkpoint returns, e.g.:
         `[{"model": "laya-rl-agent", "answers": {"relation_type": {"type":
         "choice", "choice": "authored", "probabilities": {...},
         "confidence": 0.9856, "answer_confidence": 0.9959, "action":
         {...}}}, "usage": {...}}]`
         -- i.e. for a `"type": "choice"` question the per-state answer key
         is `"choice"` (the predicted label) plus `"confidence"`, distinct
         from `graph.rerank.LayaReranker`'s `"noul"` key (that class reads a
         differently-shaped, boolean/probability question,
         `retrieval_relevance`). `"confidence"` falling back to
         `"answer_confidence"` when absent is the exact fallback
         `personal_exp/laya/ingest/ingest_raw_to_falkor.py` itself uses for
         the same "choice"-type answer shape -- copied here, not guessed.

    Same lazy-load-and-validate-against-checkpoint architecture as
    `LayaReranker`: nothing about `laya` or a real checkpoint is touched
    until `classify`/`classify_batch` is first called, so importing this
    module never requires the package or a checkpoint to be present.

    RELATION VOCABULARY GAP (flagged, not silently papered over): Laya's
    trained `relation_type` labels (`owns`, `assigned_to`, `blocks`,
    `depends_on`, `authored`, `reviews`, `attends`, `part_of`, `references`,
    `none`) come from a generic people/task ontology and only partially
    overlap Neuron's own graph relation vocabulary
    (`graph/ontology.py::RelationName`, `graph/expand.py::EXPAND_RELS`,
    `graph/axioms.py`'s `BLOCKS`). Writing an unmapped Laya label straight
    into a live Cypher relationship type would create edges nothing else in
    this codebase recognizes (wrong case, wrong ontology, invisible to
    `expand_neighbors`/axioms). `_RELATION_MAP` below maps only the labels
    with an unambiguous, already-real Neuron relation; every other label
    (including every one that would only make sense with a `Person`
    endpoint -- `assigned_to`, `reviews`, `attends`, `authored` -- which
    never occurs here since `Person` is in `HUB_LABELS` and excluded from
    every candidate pair this module builds) is treated exactly like
    `"none"`: no candidate is ever written for it. Conservative on purpose,
    same under-merge-by-default posture as `graph/semantic_pass.py`'s
    resolution ladder and `graph/resolve_text_fact.py`'s Laya mapping.
    Flagged in QUERIES.md as a judgment call worth revisiting once real
    candidates are observed.
    """

    RELATION_TYPE_QUESTION = {
        "type": "choice",
        "instructions": "Which relation from head to tail does the text state?",
        "criteria": {
            "owns": "head is responsible for tail",
            "assigned_to": "head task is assigned to tail person",
            "blocks": "head blocks tail",
            "depends_on": "head depends on tail",
            "authored": "head created or wrote tail",
            "reviews": "head reviews tail",
            "attends": "head attends tail meeting",
            "part_of": "head belongs to tail",
            "references": "head links to or cites tail",
            "none": "no relation stated",
        },
    }

    _RELATION_MAP = {
        "references": "REFERENCES",
        "owns": "OWNS",
        "part_of": "PARENT_OF",
        "blocks": "BLOCKS",
    }

    def __init__(
        self,
        model_dir: str | None = None,
        device: str | None = None,
        batch_size: int = 8,
        *,
        agent_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        self.model_dir = model_dir or os.getenv("LAYA_MODEL_DIR")
        self.device = device or os.getenv("LAYA_DEVICE", "cpu")
        self.batch_size = batch_size
        self._agent_factory = agent_factory
        self._agent = None
        self._lock = threading.Lock()

    def _load(self):
        if self._agent is not None:
            return self._agent
        if not self.model_dir:
            raise RuntimeError(
                "LAYA_MODEL_DIR is required to classify link_candidates relations"
            )
        model_dir = Path(self.model_dir).expanduser()
        question_path = model_dir / "questions.json"
        if not question_path.is_file():
            raise RuntimeError(f"Laya checkpoint is missing questions.json: {model_dir}")
        questions = json.loads(question_path.read_text())
        trained_question = questions.get("relation_type")
        if trained_question != self.RELATION_TYPE_QUESTION:
            raise RuntimeError(
                "Laya relation_type schema does not match the trained Neuron contract"
            )
        if self._agent_factory is None:
            try:
                import laya
            except ImportError as exc:
                raise RuntimeError(
                    "classifying link_candidates relations requires the `laya` package"
                ) from exc
            self._agent = laya.Agent(str(model_dir), device=self.device)
        else:
            self._agent = self._agent_factory(str(model_dir), self.device)
        return self._agent

    def classify_batch(self, states: list[dict[str, str]]) -> list[tuple[str, float]]:
        """One `(relation, confidence)` pair per state, in input order.
        `relation` is already mapped through `_RELATION_MAP` (or `"none"`
        for anything unmapped/absent) -- callers never see Laya's raw label."""
        if not states:
            return []
        agent = self._load()
        with self._lock:
            predictions = agent.predict_batch(
                states, {"relation_type": self.RELATION_TYPE_QUESTION},
                batch_size=self.batch_size,
                sort_by_length=len(states) > self.batch_size,
            )
        if len(predictions) != len(states):
            raise RuntimeError(
                f"Laya returned {len(predictions)} predictions for {len(states)} states"
            )
        results = []
        for prediction in predictions:
            answer = prediction["answers"]["relation_type"]
            raw = answer.get("choice", "none")
            confidence = float(answer.get("confidence", answer.get("answer_confidence", 0.0)))
            results.append((self._RELATION_MAP.get(raw, "none"), confidence))
        return results

    def classify(self, text: str, head: str, tail: str) -> tuple[str, float]:
        return self.classify_batch([{"text": text, "head": head, "tail": tail}])[0]


# ------------------------------------------------------------- write gate


def _node_name_and_text(graph: Graph, uid: str) -> tuple[str, str]:
    rows = graph.query(
        "MATCH (n {uid: $uid}) "
        "RETURN n.name, coalesce(n.search_text, n.definition, n.statement, '')",
        params={"uid": uid},
    ).result_set
    if not rows:
        return "", ""
    return rows[0][0] or "", rows[0][1] or ""


def _build_state(graph: Graph, from_uid: str, to_uid: str) -> dict[str, str]:
    """plan.md §6.2: `{"text": best_window of A's text, "head": A.name,
    "tail": B.name}`. The window is scored against B's name so the slice of
    A's text most likely to mention B (if any) is what Laya sees."""
    head_name, head_text = _node_name_and_text(graph, from_uid)
    tail_name, _tail_text = _node_name_and_text(graph, to_uid)
    window = (
        best_window(tail_name, head_text, _STATE_TEXT_WINDOW_TOKENS, _encoding)
        if head_text else head_text
    )
    return {"text": window, "head": head_name, "tail": tail_name}


def _propose(
    graph: Graph,
    ledger: ConnectorLedger,
    classifier: LayaRelationClassifier,
    pairs: list[tuple[str, str]],
    *,
    derived_rule: str,
) -> list[int]:
    """Shared write gate for both candidate sources (plan.md §6.2): classify
    every pair's Laya `relation_type`, and only persist
    (`ledger.create_link_candidate`) when `relation != "none"` AND
    `confidence >= CONFIDENCE_GATE` (0.6). The stored `confidence` is always
    0.5 (DICE-neutral, per the plan) regardless of Laya's own confidence --
    Laya's confidence only gates *whether* a row is written, exactly like
    every other Laya integration point in this codebase gates a decision
    without the model's own score leaking into a stored field."""
    if not pairs:
        return []
    states = [_build_state(graph, from_uid, to_uid) for from_uid, to_uid in pairs]
    classifications = classifier.classify_batch(states)
    created: list[int] = []
    for (from_uid, to_uid), (relation, confidence) in zip(pairs, classifications):
        if relation == "none" or confidence < CONFIDENCE_GATE:
            continue
        created.append(ledger.create_link_candidate(
            from_uid, to_uid, relation, derived_rule=derived_rule, confidence=0.5,
        ))
    return created


def propose_two_hop_candidates(
    graph: Graph,
    ledger: ConnectorLedger,
    classifier: LayaRelationClassifier,
    *,
    degree_cap: int = 50,
) -> list[int]:
    """Find + classify + (maybe) persist two-hop candidates. Returns the
    `link_candidates` ids actually created/found (idempotent per
    `create_link_candidate`)."""
    triples = find_two_hop_candidates(graph, degree_cap=degree_cap)
    pairs = list({(a, b) for a, b, _z in triples})
    return _propose(graph, ledger, classifier, pairs, derived_rule=TWO_HOP)


def propose_semantic_candidates(
    graph: Graph,
    ledger: ConnectorLedger,
    client: QdrantClient | PostgresVectorClient,
    classifier: LayaRelationClassifier,
    *,
    similarity_threshold: float = 0.65,
    limit: int = 10,
    collection: str = COLLECTION,
) -> list[int]:
    """Find + classify + (maybe) persist semantic candidates."""
    triples = find_semantic_candidates(
        graph, client, similarity_threshold=similarity_threshold, limit=limit,
        collection=collection,
    )
    pairs = [(from_uid, to_uid) for from_uid, to_uid, _sim in triples]
    return _propose(graph, ledger, classifier, pairs, derived_rule=SEMANTIC_CANDIDATE)


# -------------------------------------------------------- approval -> edge


def _mentioned_record_keys(graph: Graph, uid: str) -> list[str]:
    rows = graph.query(
        "MATCH (n {uid: $uid})-[:MENTIONED_IN]->(sr:SourceRecord) "
        "WHERE sr.deleted_at IS NULL RETURN DISTINCT sr.record_key",
        params={"uid": uid},
    ).result_set
    return [row[0] for row in rows if row[0]]


def apply_approved_link_candidate(
    graph: Graph, ledger: ConnectorLedger, candidate_id: int,
) -> bool:
    """Promote one `approved` `link_candidates` row into a real graph edge
    (plan.md §6.2): `derived=True`, `extraction_method="derived"`,
    provenance = both nodes' `SourceRecord`s.

    "Both nodes' records" is read as the UNION of each endpoint's own
    `MENTIONED_IN` record keys (not the intersection/shared-only set) --
    matching the closest existing precedent in this codebase,
    `graph/chat.py::_two_entity_lane`'s `MATCH (a)-[:MENTIONED_IN]->(sr)
    <-[:MENTIONED_IN]-(b)` pattern for "what a pair of nodes is jointly
    evidenced by", generalized here to "everything either endpoint is
    evidenced by" since a derived link between two isolated/sparse nodes
    should carry forward what's known about EACH of them, not only the
    (likely empty, since they were never connected before) overlap.
    `graph/derived.py`'s existing derived-edge writers source provenance
    from a *premise fact edge's* `source_record_keys` instead -- not
    reusable here since these candidates (especially semantic ones) have no
    premise edge at all, only two node identities.

    Returns `False` (no write) if the candidate does not exist, is not in
    `approved` state, or either endpoint node no longer exists in the graph
    (e.g. merged/deleted since the candidate was proposed) -- `True` once
    the edge write completes.
    """
    candidate = ledger.get_link_candidate(candidate_id)
    if candidate is None or candidate.state != "approved":
        return False

    label_rows = graph.query(
        "OPTIONAL MATCH (a {uid: $from_uid}) OPTIONAL MATCH (b {uid: $to_uid}) "
        "RETURN labels(a), labels(b)",
        params={"from_uid": candidate.from_uid, "to_uid": candidate.to_uid},
    ).result_set
    if not label_rows:
        return False
    a_labels, b_labels = label_rows[0]
    if not a_labels or not b_labels:
        return False
    from_label, to_label = a_labels[0], b_labels[0]

    provenance = sorted(set(
        _mentioned_record_keys(graph, candidate.from_uid)
        + _mentioned_record_keys(graph, candidate.to_uid)
    ))

    row = {
        "from_uid": candidate.from_uid,
        "to_uid": candidate.to_uid,
        "source_record_keys": provenance,
        "evidence": (
            f"Approved link candidate #{candidate.id} "
            f"({candidate.derived_rule}, Laya relation_type={candidate.relation})."
        ),
        "extraction_method": "derived",
        "confidence": candidate.confidence,
        "derived": True,
        "derived_rule": candidate.derived_rule,
    }
    w.upsert_fact_edges(graph, candidate.relation, from_label, to_label, [row], revive=False)
    return True
