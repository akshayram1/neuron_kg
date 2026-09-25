"""Tests for `graph/chat.py`'s plan.md §1.7 two-entity lane.

`_file_path_candidates` is a pure regex, tested without a live graph. The
anchor-resolution Cypher (`_pair_anchor_candidates`/`_two_entity_lane`) is
exercised against a real FalkorDB graph, same pattern as `tests/test_expand.py`
(a uniquely-named graph per test, created and torn down).
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from graph import chat
from graph.access import AccessScope
from graph.falkor_client import build_client

# --- _file_path_candidates: pure regex, no graph -----------------------------

def test_file_path_candidates_matches_known_extensions():
    assert chat._file_path_candidates("what changed in graph/chat.py recently") == ["graph/chat.py"]


def test_file_path_candidates_ignores_non_path_dotted_tokens():
    assert chat._file_path_candidates("e.g. version 1.0 was released") == []


def test_file_path_candidates_dedupes_repeats():
    text = "graph/chat.py was touched, then graph/chat.py again"
    assert chat._file_path_candidates(text) == ["graph/chat.py"]


# --- live-graph integration ---------------------------------------------------
# Per-function skipif (NOT a module-level `pytestmark`) -- this file also
# holds the pure `_file_path_candidates` tests above, which must always run.

_needs_live_graph = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)

PUBLIC_SCOPE = AccessScope(allow_public=True)


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_pair_lane_{uuid4().hex}")
    try:
        yield g
    finally:
        g.delete()


def _work_item(graph, uid: str, issue_key: str, *, search_text: str = "") -> None:
    graph.query(
        "CREATE (n:WorkItem {uid: $uid, name: $name, issue_key: $issue_key, search_text: $search_text})",
        params={"uid": uid, "name": issue_key, "issue_key": issue_key, "search_text": search_text},
    )


def _mentioned_in(graph, uid: str, record_key: str, *, record_name: str = "", public: bool = True) -> None:
    graph.query(
        """
        MERGE (sr:SourceRecord {record_key: $record_key})
        SET sr.public = $public, sr.provider = 'jira', sr.connection_id = 'conn1',
            sr.deleted_at = null, sr.name = $record_name
        WITH sr
        MATCH (n {uid: $uid})
        CREATE (n)-[:MENTIONED_IN]->(sr)
        """,
        params={"uid": uid, "record_key": record_key, "public": public, "record_name": record_name},
    )


@_needs_live_graph
def test_two_ticket_keys_resolve_and_fire_the_pair_lane(graph):
    _work_item(graph, "wi1", "DATAOS-1", search_text="DATAOS-1 body text")
    _work_item(graph, "wi2", "DATAOS-2", search_text="DATAOS-2 body text")
    _mentioned_in(graph, "wi1", "shared-sr", record_name="DATAOS-1 mentions DATAOS-2")
    _mentioned_in(graph, "wi2", "shared-sr", record_name="DATAOS-1 mentions DATAOS-2")

    anchors = chat._pair_anchor_candidates(
        graph, "How does DATAOS-1 relate to DATAOS-2?", PUBLIC_SCOPE, None,
    )
    uids = {uid for uid, _label, _name in anchors}
    assert {"wi1", "wi2"} <= uids

    hits = chat._two_entity_lane(graph, "How does DATAOS-1 relate to DATAOS-2?", PUBLIC_SCOPE, None)
    assert {hit.uid for hit in hits} == {"wi1", "wi2"}
    assert all(hit.methods == ["pair"] for hit in hits)
    assert all(hit.score == 0.0 for hit in hits)
    for hit in hits:
        assert "PAIR EVIDENCE" in hit.summary
        assert "DATAOS-1 mentions DATAOS-2" in hit.summary


@_needs_live_graph
def test_pair_lane_returns_nothing_for_a_single_anchor(graph):
    _work_item(graph, "wi1", "DATAOS-1")
    _mentioned_in(graph, "wi1", "sr1")

    hits = chat._two_entity_lane(graph, "What is DATAOS-1 about?", PUBLIC_SCOPE, None)
    assert hits == []


@_needs_live_graph
def test_pair_lane_wires_through_retrieve_end_to_end(graph, monkeypatch):
    _work_item(graph, "wi1", "DATAOS-1", search_text="DATAOS-1 body text")
    _work_item(graph, "wi2", "DATAOS-2", search_text="DATAOS-2 body text")
    _mentioned_in(graph, "wi1", "shared-sr", record_name="shared ticket")
    _mentioned_in(graph, "wi2", "shared-sr", record_name="shared ticket")

    monkeypatch.setattr(chat, "resolve_structured", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_requested_knowledge_layers", lambda *a, **k: (False, False))
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: [])
    monkeypatch.setattr(chat, "expand_neighbors", lambda *a, **k: [])
    monkeypatch.setattr(chat, "find_named_persons", lambda *a, **k: [])

    _structured, hits = chat.retrieve(
        graph, object(), "How does DATAOS-1 relate to DATAOS-2?",
        limit=6, providers=None, scope=PUBLIC_SCOPE,
    )
    pair_hits = [hit for hit in hits if hit.methods == ["pair"]]
    assert {hit.uid for hit in pair_hits} == {"wi1", "wi2"}
