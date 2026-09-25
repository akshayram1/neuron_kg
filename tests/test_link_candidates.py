"""Tests for graph/link_candidates.py (25-plan.md §6.2, "Candidate links for
isolated nodes") and graph/derived.py's `_shared_concept_documents`, which
§6.2 explicitly moves onto the same candidate-producing path instead of
writing a direct edge.

Conventions, matching this codebase's established patterns rather than
inventing new ones:
  - real FalkorDB for graph queries, one uniquely-named scratch graph per
    test (tests/test_expand.py);
  - real Qdrant for `find_semantic_candidates`'s embedding search, with the
    `_vec(similarity)` trick for an exact, controllable cosine similarity
    without a real embedding model (tests/test_vector_store.py);
  - a real temp-file `ConnectorLedger` for candidate storage
    (tests/test_hygiene_ledger.py);
  - a `FakeAgent` double for `LayaRelationClassifier`, exactly like
    tests/test_rerank.py's `FakeAgent` for `LayaReranker` -- these tests
    never load the real ~800MB Laya checkpoint.

All gated behind NEURON_INTEGRATION=1 (real FalkorDB/Qdrant required).
"""

from __future__ import annotations

import json
import math
import os
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from connectors.core.ledger import ConnectorLedger
from graph import derived, vector_store
from graph.falkor_client import build_client
from graph.link_candidates import (
    LayaRelationClassifier,
    _propose,
    apply_approved_link_candidate,
    find_semantic_candidates,
    find_two_hop_candidates,
    propose_semantic_candidates,
    propose_two_hop_candidates,
)

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB/Qdrant",
)


# --------------------------------------------------------------- fixtures


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_link_candidates_{uuid.uuid4().hex}")
    try:
        yield g
    finally:
        g.delete()


@pytest.fixture
def ledger(tmp_path):
    return ConnectorLedger(tmp_path / "l.sqlite3")


@pytest.fixture
def qdrant_collection():
    client = QdrantClient(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    collection = f"test_link_candidates_{uuid.uuid4().hex}"
    vector_store.ensure_collection(client, collection)
    try:
        yield client, collection
    finally:
        client.delete_collection(collection_name=collection)


def _node(graph, label, uid, *, name=None, search_text=None):
    graph.query(
        f"CREATE (n:{label} {{uid: $uid, name: $name, search_text: $search_text}})",
        params={"uid": uid, "name": name or uid, "search_text": search_text or ""},
    )


def _edge(graph, from_uid, rel, to_uid, *, invalid_at=None):
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}})
        CREATE (a)-[r:{rel} {{invalid_at: $invalid_at}}]->(b)
        """,
        params={"from_uid": from_uid, "to_uid": to_uid, "invalid_at": invalid_at},
    )


def _mentioned_in(graph, uid, record_key):
    graph.query(
        """
        MERGE (sr:SourceRecord {record_key: $record_key})
        SET sr.deleted_at = null
        WITH sr
        MATCH (n {uid: $uid})
        CREATE (n)-[:MENTIONED_IN]->(sr)
        """,
        params={"uid": uid, "record_key": record_key},
    )


_DIM = vector_store.EMBEDDING_DIMENSION


def _vec(similarity: float) -> list[float]:
    """A vector at exact cosine `similarity` to `_BASE` -- same construction
    as tests/test_vector_store.py, so a test can target a precise threshold
    boundary without depending on a real embedding model."""
    theta = math.acos(max(-1.0, min(1.0, similarity)))
    return [math.cos(theta), math.sin(theta)] + [0.0] * (_DIM - 2)


def _upsert_embedding(client, collection, uid, label, similarity):
    client.upsert(
        collection_name=collection,
        points=[PointStruct(
            id=uid,
            vector={
                vector_store.CONTENT_VECTOR: _vec(similarity),
                vector_store.NAME_VECTOR: _vec(similarity),
            },
            payload={"label": label, "uid": uid},
        )],
    )


class FakeAgent:
    """Deterministic `relation_type` answers, in call order -- exactly the
    tests/test_rerank.py `FakeAgent` pattern, injected via
    `LayaRelationClassifier(agent_factory=...)`."""

    def __init__(self, answers: list[tuple[str, float]]):
        self.answers = answers
        self.calls = []

    def predict_batch(self, states, questions, **kwargs):
        self.calls.append((states, questions, kwargs))
        assert len(states) == len(self.answers)
        return [
            {"answers": {"relation_type": {"choice": choice, "confidence": confidence}}}
            for choice, confidence in self.answers
        ]


def _classifier(tmp_path, fake_agent) -> LayaRelationClassifier:
    (tmp_path / "questions.json").write_text(
        json.dumps({"relation_type": LayaRelationClassifier.RELATION_TYPE_QUESTION})
    )
    return LayaRelationClassifier(
        str(tmp_path), device="cpu", agent_factory=lambda _dir, _device: fake_agent,
    )


# ------------------------------------------------------------------ two-hop


def test_two_hop_finds_shared_term_pair(graph):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "b", "REFERENCES", "z")

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph)}
    assert ("a", "b") in pairs


def test_two_hop_excludes_pair_with_existing_edge(graph):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "b", "REFERENCES", "z")
    _edge(graph, "a", "BLOCKS", "b")  # already directly connected

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph)}
    assert ("a", "b") not in pairs


def test_two_hop_excludes_pair_with_existing_reverse_edge(graph):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "b", "REFERENCES", "z")
    _edge(graph, "b", "BLOCKS", "a")  # reverse direction must also count

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph)}
    assert ("a", "b") not in pairs


def test_two_hop_excludes_hub_labels_as_endpoints(graph):
    """A Repository (HUB_LABELS) endpoint is excluded even though it shares
    a qualifying Term with a legitimate WorkItem."""
    _node(graph, "WorkItem", "a")
    _node(graph, "Repository", "hub")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "hub", "REFERENCES", "z")

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph)}
    assert not any("hub" in pair for pair in pairs)


def test_two_hop_excludes_hub_shared_neighbour(graph):
    """A shared Repository is never even eligible as Z -- the query only
    ever treats Term/System/Decision as Z, so a pair whose only shared
    neighbour is a hub produces no candidate at all."""
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Repository", "hub")
    _edge(graph, "a", "PARENT_OF", "hub")
    _edge(graph, "b", "PARENT_OF", "hub")

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph)}
    assert ("a", "b") not in pairs


def test_two_hop_excludes_high_degree_shared_neighbour(graph):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "b", "REFERENCES", "z")
    for i in range(55):  # pushes z's degree to 57, above the default cap
        uid = f"filler-{i}"
        _node(graph, "WorkItem", uid)
        _edge(graph, uid, "REFERENCES", "z")

    pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph, degree_cap=50)}
    assert ("a", "b") not in pairs

    # Sanity: a generous cap lets the same pair back in.
    loose_pairs = {(a, b) for a, b, _z in find_two_hop_candidates(graph, degree_cap=1000)}
    assert ("a", "b") in loose_pairs


def test_two_hop_no_duplicate_unordered_pairs(graph):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z1")
    _node(graph, "System", "z2")
    _edge(graph, "a", "REFERENCES", "z1")
    _edge(graph, "b", "REFERENCES", "z1")
    _edge(graph, "a", "REFERENCES", "z2")
    _edge(graph, "b", "REFERENCES", "z2")

    triples = find_two_hop_candidates(graph)
    # Every row is canonically ordered (a < b) -- never both (a,b) and (b,a).
    assert all(a < b for a, b, _z in triples)
    assert len([t for t in triples if {t[0], t[1]} == {"a", "b"}]) == len({t[2] for t in triples})


# ------------------------------------------------------------ semantic


def test_semantic_candidate_above_threshold_found(graph, qdrant_collection):
    client, collection = qdrant_collection
    doc_uid, wi_uid = str(uuid.uuid4()), str(uuid.uuid4())
    _node(graph, "Document", doc_uid)
    _node(graph, "WorkItem", wi_uid)
    _upsert_embedding(client, collection, doc_uid, "Document", 1.0)
    _upsert_embedding(client, collection, wi_uid, "WorkItem", 0.80)

    pairs = {
        (a, b) for a, b, _sim in
        find_semantic_candidates(graph, client, collection=collection)
    }
    assert (doc_uid, wi_uid) in pairs


def test_semantic_candidate_below_threshold_excluded(graph, qdrant_collection):
    client, collection = qdrant_collection
    doc_uid, wi_uid = str(uuid.uuid4()), str(uuid.uuid4())
    _node(graph, "Document", doc_uid)
    _node(graph, "WorkItem", wi_uid)
    _upsert_embedding(client, collection, doc_uid, "Document", 1.0)
    _upsert_embedding(client, collection, wi_uid, "WorkItem", 0.40)  # below default 0.65

    pairs = {
        (a, b) for a, b, _sim in
        find_semantic_candidates(graph, client, collection=collection)
    }
    assert (doc_uid, wi_uid) not in pairs


def test_semantic_candidate_ignores_non_isolated_document(graph, qdrant_collection):
    """A Document that already has a live DOCUMENTS edge is not isolated --
    it must not show up as a semantic-candidate source at all."""
    client, collection = qdrant_collection
    doc_uid, wi_uid, other_wi_uid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    _node(graph, "Document", doc_uid)
    _node(graph, "WorkItem", wi_uid)
    _node(graph, "WorkItem", other_wi_uid)
    _edge(graph, doc_uid, "DOCUMENTS", other_wi_uid)  # no longer isolated
    _upsert_embedding(client, collection, doc_uid, "Document", 1.0)
    _upsert_embedding(client, collection, wi_uid, "WorkItem", 0.90)

    pairs = {
        (a, b) for a, b, _sim in
        find_semantic_candidates(graph, client, collection=collection)
    }
    assert (doc_uid, wi_uid) not in pairs


def test_semantic_candidate_limit_is_respected(graph, qdrant_collection):
    client, collection = qdrant_collection
    doc_uid = str(uuid.uuid4())
    _node(graph, "Document", doc_uid)
    _upsert_embedding(client, collection, doc_uid, "Document", 1.0)
    for _ in range(15):
        uid = str(uuid.uuid4())
        _node(graph, "WorkItem", uid)
        _upsert_embedding(client, collection, uid, "WorkItem", 0.90)

    triples = find_semantic_candidates(graph, client, collection=collection, limit=10)
    from_doc = [t for t in triples if t[0] == doc_uid]
    assert len(from_doc) == 10


# --------------------------------------------------------- write gate


def test_relation_classifier_maps_known_laya_labels(tmp_path):
    """The mapped, always-real subset (`_RELATION_MAP`) documented in
    `LayaRelationClassifier`'s docstring."""
    fake_agent = FakeAgent([
        ("references", 0.95), ("owns", 0.70), ("part_of", 0.61), ("blocks", 0.90),
    ])
    classifier = _classifier(tmp_path, fake_agent)
    results = classifier.classify_batch([{"text": "t", "head": "a", "tail": "b"}] * 4)
    assert results == [
        ("REFERENCES", 0.95), ("OWNS", 0.70), ("PARENT_OF", 0.61), ("BLOCKS", 0.90),
    ]


def test_relation_classifier_maps_everything_else_to_none(tmp_path):
    """Laya's Person-shaped labels (never structurally reachable here since
    Person is HUB_LABELS-excluded), plus its own literal "none", plus any
    future/unrecognized label, all collapse to "none" -- conservative,
    documented, and exercised explicitly rather than left untested."""
    fake_agent = FakeAgent([
        ("assigned_to", 0.99), ("attends", 0.99), ("authored", 0.99),
        ("reviews", 0.99), ("depends_on", 0.99), ("none", 0.99),
    ])
    classifier = _classifier(tmp_path, fake_agent)
    results = classifier.classify_batch([{"text": "t", "head": "a", "tail": "b"}] * 6)
    assert [relation for relation, _confidence in results] == ["none"] * 6


def test_propose_writes_candidate_when_gate_passes(graph, ledger, tmp_path):
    _node(graph, "WorkItem", "a", search_text="a mentions b directly")
    _node(graph, "WorkItem", "b")
    classifier = _classifier(tmp_path, FakeAgent([("references", 0.90)]))

    created = _propose(graph, ledger, classifier, [("a", "b")], derived_rule="two_hop")

    assert len(created) == 1
    candidates = ledger.list_link_candidates(state=None)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.from_uid == "a" and candidate.to_uid == "b"
    assert candidate.relation == "REFERENCES"
    assert candidate.derived_rule == "two_hop"
    # DICE-neutral 0.5 is always stored, regardless of Laya's own confidence.
    assert candidate.confidence == 0.5
    assert candidate.state == "pending"


def test_propose_skips_when_relation_is_none(graph, ledger, tmp_path):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    classifier = _classifier(tmp_path, FakeAgent([("none", 0.99)]))

    created = _propose(graph, ledger, classifier, [("a", "b")], derived_rule="two_hop")

    assert created == []
    assert ledger.list_link_candidates(state=None) == []


def test_propose_skips_when_confidence_below_gate(graph, ledger, tmp_path):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    classifier = _classifier(tmp_path, FakeAgent([("references", 0.40)]))  # < 0.6 gate

    created = _propose(graph, ledger, classifier, [("a", "b")], derived_rule="two_hop")

    assert created == []
    assert ledger.list_link_candidates(state=None) == []


def test_propose_two_hop_candidates_end_to_end(graph, ledger, tmp_path):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _node(graph, "Term", "z")
    _edge(graph, "a", "REFERENCES", "z")
    _edge(graph, "b", "REFERENCES", "z")
    classifier = _classifier(tmp_path, FakeAgent([("references", 0.90)]))

    created = propose_two_hop_candidates(graph, ledger, classifier)

    assert len(created) == 1
    candidates = ledger.list_link_candidates(derived_rule="two_hop")
    assert len(candidates) == 1
    assert {candidates[0].from_uid, candidates[0].to_uid} == {"a", "b"}


def test_propose_semantic_candidates_end_to_end(graph, ledger, qdrant_collection, tmp_path):
    client, collection = qdrant_collection
    doc_uid, wi_uid = str(uuid.uuid4()), str(uuid.uuid4())
    _node(graph, "Document", doc_uid)
    _node(graph, "WorkItem", wi_uid)
    _upsert_embedding(client, collection, doc_uid, "Document", 1.0)
    _upsert_embedding(client, collection, wi_uid, "WorkItem", 0.80)
    classifier = _classifier(tmp_path, FakeAgent([("references", 0.90)]))

    created = propose_semantic_candidates(graph, ledger, client, classifier, collection=collection)

    assert len(created) == 1
    candidates = ledger.list_link_candidates(derived_rule="semantic_candidate")
    assert len(candidates) == 1
    assert candidates[0].from_uid == doc_uid
    assert candidates[0].to_uid == wi_uid


# ------------------------------------------------------- approval -> edge


def test_apply_approved_creates_derived_edge_with_union_provenance(graph, ledger):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    _mentioned_in(graph, "a", "sr-a")
    _mentioned_in(graph, "b", "sr-b")
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    ledger.approve_link_candidate(candidate_id)

    assert apply_approved_link_candidate(graph, ledger, candidate_id) is True

    rows = graph.query(
        "MATCH (a {uid:'a'})-[r:REFERENCES]->(b {uid:'b'}) "
        "RETURN r.derived, r.extraction_method, r.source_record_keys"
    ).result_set
    assert len(rows) == 1
    is_derived, method, keys = rows[0]
    assert is_derived is True
    assert method == "derived"
    assert set(keys) == {"sr-a", "sr-b"}


def test_apply_approved_returns_false_for_pending_candidate(graph, ledger):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")

    assert apply_approved_link_candidate(graph, ledger, candidate_id) is False
    rows = graph.query("MATCH (:WorkItem {uid:'a'})-[r:REFERENCES]->(:WorkItem {uid:'b'}) RETURN r").result_set
    assert rows == []


def test_apply_approved_returns_false_for_rejected_candidate(graph, ledger):
    _node(graph, "WorkItem", "a")
    _node(graph, "WorkItem", "b")
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    ledger.reject_link_candidate(candidate_id)

    assert apply_approved_link_candidate(graph, ledger, candidate_id) is False
    rows = graph.query("MATCH (:WorkItem {uid:'a'})-[r:REFERENCES]->(:WorkItem {uid:'b'}) RETURN r").result_set
    assert rows == []


def test_apply_approved_returns_false_when_endpoint_missing(graph, ledger):
    _node(graph, "WorkItem", "a")
    # "b" never created.
    candidate_id = ledger.create_link_candidate("a", "b", "REFERENCES", derived_rule="two_hop")
    ledger.approve_link_candidate(candidate_id)

    assert apply_approved_link_candidate(graph, ledger, candidate_id) is False


# --------------------------------------------- _shared_concept_documents


def test_shared_concept_documents_creates_candidate_not_direct_edge(graph, ledger):
    """25-plan.md §6.2's explicit derived.py behavior change: a shared
    Term/System no longer writes a direct DOCUMENTS edge, it proposes a
    link_candidates row instead."""
    _node(graph, "Document", "doc1")
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Term", "term1")
    _edge(graph, "doc1", "DEFINES", "term1")
    _edge(graph, "wi1", "REFERENCES", "term1")

    written = derived.materialize_around(graph, "doc1", "record-1", ledger=ledger)
    assert written == 1

    edge_rows = graph.query(
        "MATCH (:Document {uid:'doc1'})-[r:DOCUMENTS]->(:WorkItem {uid:'wi1'}) RETURN r"
    ).result_set
    assert edge_rows == []

    candidates = [c for c in ledger.list_link_candidates(state=None) if c.derived_rule == "shared_concept"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.from_uid == "doc1"
    assert candidate.to_uid == "wi1"
    assert candidate.relation == "DOCUMENTS"
    assert candidate.confidence == 0.5
    assert candidate.state == "pending"


def test_shared_concept_documents_without_ledger_is_a_documented_noop(graph):
    """Existing callers (graph/jira_pipeline.py, graph/resolver.py) don't
    pass a ledger yet -- this must not crash, and must not write anything."""
    _node(graph, "Document", "doc1")
    _node(graph, "WorkItem", "wi1")
    _node(graph, "Term", "term1")
    _edge(graph, "doc1", "DEFINES", "term1")
    _edge(graph, "wi1", "REFERENCES", "term1")

    written = derived.materialize_around(graph, "doc1", "record-1")  # no ledger
    assert written == 0

    edge_rows = graph.query(
        "MATCH (:Document {uid:'doc1'})-[r:DOCUMENTS]->(:WorkItem {uid:'wi1'}) RETURN r"
    ).result_set
    assert edge_rows == []
