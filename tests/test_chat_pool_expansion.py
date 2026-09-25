"""Unit tests for `graph/chat.py`'s plan.md §1.1 (wide pool), §1.3/§1.4 (one-hop
expansion wiring) and the pool-then-cut boundary in `run_chat_turn`. Pure
monkeypatch, no live FalkorDB/Qdrant/OpenAI -- same pattern
`tests/test_chat_knowledge_layers.py` already uses for `graph/chat.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

from graph import chat
from graph.search import SearchHit


def _hit(uid: str, label: str = "WorkItem", name: str | None = None, score: float = 0.5) -> SearchHit:
    return SearchHit(uid, label, name or uid, f"summary for {uid}", score, ["vector"])


def _neutralize_lanes(monkeypatch, *, expand=None, pair=None, named_persons=None):
    """No structured/wisdom/pair/expansion/named-person/time-window noise --
    isolates whichever single lane a test is actually exercising."""
    monkeypatch.setattr(chat, "resolve_structured", lambda *a, **k: None)
    monkeypatch.setattr(chat, "_requested_knowledge_layers", lambda *a, **k: (False, False))
    monkeypatch.setattr(chat, "find_named_persons", lambda *a, **k: named_persons or [])
    monkeypatch.setattr(chat, "expand_neighbors", expand or (lambda *a, **k: []))
    monkeypatch.setattr(chat, "_two_entity_lane", pair or (lambda *a, **k: []))


# --- 1.1 wide pool -----------------------------------------------------------

def test_else_branch_requests_pool_size_not_the_caller_limit(monkeypatch):
    _neutralize_lanes(monkeypatch)
    captured = {}

    def fake_search(*args, limit, **kwargs):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(chat, "hybrid_search", fake_search)

    chat.retrieve(object(), object(), "what changed in argus", limit=6, providers=None, scope=object())

    assert captured["limit"] == chat.POOL_SIZE
    assert captured["limit"] != 6  # the caller's `limit` must not leak through unchanged


def test_pool_size_env_override_changes_the_fetched_candidate_count(monkeypatch):
    """plan.md §1.1 exit criteria: NEURON_POOL_SIZE actually changes the pool
    hybrid_search is asked for."""
    _neutralize_lanes(monkeypatch)
    monkeypatch.setattr(chat, "POOL_SIZE", 5)
    captured = {}

    def fake_search(*args, limit, **kwargs):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(chat, "hybrid_search", fake_search)
    chat.retrieve(object(), object(), "what changed in argus", limit=6, providers=None, scope=object())
    assert captured["limit"] == 5


def test_wide_pool_is_behavior_preserving_after_the_final_cut(monkeypatch):
    """plan.md §1.1's own risk note: 'larger pool with the old fixed cut
    changes nothing (expected until Phase 2)'. A stable global ranking
    (independent of how many results were asked for, same as real
    `hybrid_search`/RRF) must produce the SAME top-`search_limit` whether the
    pool was fetched wide (POOL_SIZE) or narrow (the pre-1.1 `limit`-only
    behaviour), once both are cut to the same `search_limit`."""
    _neutralize_lanes(monkeypatch)
    full_ranking = [_hit(f"n{i}", score=1.0 - i * 0.01) for i in range(40)]

    def fake_search(*args, limit, **kwargs):
        return full_ranking[:limit]

    monkeypatch.setattr(chat, "hybrid_search", fake_search)

    search_limit = 6
    _structured, wide_hits = chat.retrieve(
        object(), object(), "what changed", limit=search_limit, providers=None, scope=object(),
    )
    wide_cut = wide_hits[:search_limit]

    monkeypatch.setattr(chat, "POOL_SIZE", search_limit)  # emulate the pre-1.1 narrow pool
    _structured, narrow_hits = chat.retrieve(
        object(), object(), "what changed", limit=search_limit, providers=None, scope=object(),
    )
    narrow_cut = narrow_hits[:search_limit]

    assert [hit.uid for hit in wide_cut] == [hit.uid for hit in narrow_cut]


# --- 1.3/1.4 expansion wiring -------------------------------------------------

def test_expansion_candidates_are_merged_and_deduped(monkeypatch):
    pool = [_hit("n1"), _hit("n2")]
    # n1 is already in the pool -- must not be duplicated. n3/n4 are new.
    neighbors = [_hit("n1", label="Commit"), _hit("n3", label="Commit"), _hit("n4", label="Document")]

    def fake_search(*args, **kwargs):
        return pool

    def fake_expand(graph, seed_uids, scope, providers, *, min_tier, exclude_edges):
        assert seed_uids == ["n1", "n2"]
        return neighbors

    _neutralize_lanes(monkeypatch, expand=fake_expand)
    monkeypatch.setattr(chat, "hybrid_search", fake_search)

    _structured, hits = chat.retrieve(
        object(), object(), "what changed", limit=6, providers=None, scope=object(),
    )

    uids = [hit.uid for hit in hits]
    assert uids == ["n1", "n2", "n3", "n4"]  # pool first, then new neighbours only, no dup n1
    assert uids.count("n1") == 1


def test_expansion_seeds_are_capped_at_max_seeds(monkeypatch):
    pool = [_hit(f"n{i}") for i in range(20)]
    captured = {}

    def fake_expand(graph, seed_uids, scope, providers, *, min_tier, exclude_edges):
        captured["seed_uids"] = seed_uids
        return []

    _neutralize_lanes(monkeypatch, expand=fake_expand)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: pool)

    chat.retrieve(object(), object(), "what changed", limit=6, providers=None, scope=object())

    from graph.expand import MAX_SEEDS
    assert len(captured["seed_uids"]) == MAX_SEEDS
    assert captured["seed_uids"] == [hit.uid for hit in pool[:MAX_SEEDS]]


def test_min_tier_is_derived_since_structured_is_always_none_here(monkeypatch):
    """plan.md §1.4: 'primary for lookup questions..., derived otherwise'.
    `resolve_structured` already returns above for any structured/lookup
    question, so by the time `expand_neighbors` is called, `structured` is
    always `None` in this control flow -- see the QUERY flagged in the task
    report."""
    captured = {}

    def fake_expand(graph, seed_uids, scope, providers, *, min_tier, exclude_edges):
        captured["min_tier"] = min_tier
        return []

    _neutralize_lanes(monkeypatch, expand=fake_expand)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: [_hit("n1")])

    chat.retrieve(object(), object(), "DATAOS-4192 status", limit=6, providers=None, scope=object())
    assert captured["min_tier"] == "derived"


def test_exclude_edges_threads_through_to_expand_neighbors(monkeypatch):
    captured = {}

    def fake_expand(graph, seed_uids, scope, providers, *, min_tier, exclude_edges):
        captured["exclude_edges"] = exclude_edges
        return []

    _neutralize_lanes(monkeypatch, expand=fake_expand)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: [_hit("n1")])

    sentinel = frozenset({("a", "IMPLEMENTS", "b")})
    chat.retrieve(
        object(), object(), "what changed", limit=6, providers=None, scope=object(),
        exclude_edges=sentinel,
    )
    assert captured["exclude_edges"] is sentinel


def test_pool_changes_are_logged(monkeypatch, caplog):
    pool = [_hit("n1")]
    neighbors = [_hit("n2", label="Commit")]

    _neutralize_lanes(monkeypatch, expand=lambda *a, **k: neighbors)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: pool)

    with caplog.at_level("INFO", logger="neuron.chat"):
        chat.retrieve(object(), object(), "what changed", limit=6, providers=None, scope=object())

    assert any("pool" in record.message and "1 -> 2" in record.message for record in caplog.records)


# --- 1.7 two-entity lane wiring (at the retrieve() level) --------------------

def test_two_entity_lane_hits_are_prepended_with_pair_method(monkeypatch):
    pool = [_hit("n1")]
    pair_hits = [
        SearchHit("a", "WorkItem", "DATAOS-1", "pair text a", 0.0, ["pair"]),
        SearchHit("b", "Person", "Someone", "pair text b", 0.0, ["pair"]),
    ]

    _neutralize_lanes(monkeypatch, pair=lambda *a, **k: pair_hits)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: pool)

    _structured, hits = chat.retrieve(
        object(), object(), "DATAOS-1 and Someone", limit=6, providers=None, scope=object(),
    )

    assert [hit.uid for hit in hits[:2]] == ["a", "b"]
    assert all(hit.methods == ["pair"] for hit in hits[:2])


def test_two_entity_lane_dedupes_against_the_existing_pool(monkeypatch):
    pool = [_hit("a")]  # already a candidate from hybrid search
    pair_hits = [
        SearchHit("a", "WorkItem", "DATAOS-1", "pair text a", 0.0, ["pair"]),
        SearchHit("b", "Person", "Someone", "pair text b", 0.0, ["pair"]),
    ]

    _neutralize_lanes(monkeypatch, pair=lambda *a, **k: pair_hits)
    monkeypatch.setattr(chat, "hybrid_search", lambda *a, **k: pool)

    _structured, hits = chat.retrieve(
        object(), object(), "DATAOS-1 and Someone", limit=6, providers=None, scope=object(),
    )

    uids = [hit.uid for hit in hits]
    assert uids.count("a") == 1
    assert uids[0] == "b"  # only the new pair hit is prepended


# --- pool-then-cut boundary (run_chat_turn) -----------------------------------

class _FakeResponses:
    def parse(self, **kwargs):
        return SimpleNamespace(usage=None, output_parsed=SimpleNamespace(answer="ok", used_sources=[]))


class _FakeClient:
    def __init__(self):
        self.responses = _FakeResponses()


def test_run_chat_turn_cuts_to_search_limit_after_retrieve(monkeypatch):
    wide_pool = [_hit(f"n{i}") for i in range(10)]

    monkeypatch.setattr(chat, "retrieve", lambda *a, **k: (None, wide_pool))
    monkeypatch.setattr(chat, "_entity_evidence", lambda *a, **k: ([], [], set()))
    monkeypatch.setattr(chat, "_knowledge_metadata", lambda *a, **k: "")
    monkeypatch.setattr(chat, "_mentioned_record_keys", lambda *a, **k: set())
    monkeypatch.setattr(chat, "_resolve_records", lambda *a, **k: {})
    monkeypatch.setattr(chat, "_resolve_knowledge_citations", lambda *a, **k: [])

    result = chat.run_chat_turn(
        object(), _FakeClient(), "what changed", search_limit=4, scope=object(),
    )

    assert len(result.highlighted_nodes) == 4
    assert result.highlighted_nodes == [hit.uid for hit in wide_pool[:4]]
