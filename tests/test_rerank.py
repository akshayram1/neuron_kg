"""Tests for graph/rerank.py (25-plan.md §2.1 common reranker interface).

No generic cross-encoder implementation lives in graph/rerank.py (removed 25
Sep 2026 -- no `sentence-transformers`/`torch` dependency, per the repo
owner). All tests here are pure/offline: they use a `FakeReranker` to
exercise the `Reranker` protocol shape, `select_final`'s selection
mechanism, and `LayaReranker`'s stub contract, none of which need a real
model or network access.
"""

from __future__ import annotations

import pytest

from graph.rerank import (
    LayaReranker,
    RerankCandidate,
    RerankScore,
    Reranker,
    candidates_from_hits,
    select_final,
)
from graph.search import SearchHit


def _candidates(n: int) -> list[RerankCandidate]:
    return [
        RerankCandidate(uid=f"u{i}", window=f"candidate window text {i}", label="WorkItem", name=f"n{i}")
        for i in range(n)
    ]


class FakeReranker:
    """A minimal, deterministic `Reranker` -- exercises the protocol shape
    without needing a real model. Scores by input order (later == higher),
    so ordering/determinism assertions have a known-correct expectation."""

    def __init__(self, model: str = "fake", model_version: str = "v1") -> None:
        self.model = model
        self.model_version = model_version

    def score(self, question: str, candidates: list[RerankCandidate]) -> list[RerankScore]:
        return [
            RerankScore(uid=c.uid, score=float(i), model=self.model, model_version=self.model_version)
            for i, c in enumerate(candidates)
        ]


# --- Reranker protocol shape ------------------------------------------------

def test_fake_reranker_satisfies_the_reranker_protocol():
    assert isinstance(FakeReranker(), Reranker)


def test_laya_reranker_satisfies_the_reranker_protocol():
    assert isinstance(LayaReranker(), Reranker)


# --- common input equality across implementations ---------------------------

def test_common_candidate_list_produces_well_formed_output_from_fake_reranker():
    """Stand-in for "a real scorer produces well-formed, same-shaped output
    from the same RerankCandidate list" -- there is no cross-encoder
    implementation in this module to exercise for real (see module
    docstring); LayaReranker's equivalent is that it raises cleanly
    (test_laya_reranker_raises_not_implemented), not that it scores
    anything, per this task's explicit instructions."""
    candidates = _candidates(4)
    scores = FakeReranker().score("some question", candidates)
    assert len(scores) == len(candidates)
    for s in scores:
        assert isinstance(s, RerankScore)
        assert isinstance(s.uid, str)
        assert isinstance(s.score, float)
        assert isinstance(s.model, str) and s.model
        assert isinstance(s.model_version, str) and s.model_version
    assert [s.uid for s in scores] == [c.uid for c in candidates]


def test_laya_reranker_raises_not_implemented_cleanly():
    """Per this task's scope: LayaReranker is a best-effort stub. The test
    is that calling it raises NotImplementedError cleanly (not some other
    exception, not a silent wrong score), not that it scores anything."""
    reranker = LayaReranker()
    candidates = _candidates(3)
    with pytest.raises(NotImplementedError):
        reranker.score("some question", candidates)


def test_laya_reranker_question_and_state_shape_match_discovered_schema():
    """RETRIEVAL_RELEVANCE_QUESTION is reproduced verbatim from
    personal_exp/laya/ingest/schema.py's QUESTIONS["retrieval_relevance"] --
    pin this shape so it can't silently drift even while the class itself
    stays a stub."""
    assert LayaReranker.RETRIEVAL_RELEVANCE_QUESTION == {
        "type": "noul",
        "instructions": "Is this graph node needed to answer the user's question?",
    }


# --- deterministic batching / order -----------------------------------------

def test_fake_reranker_is_deterministic_across_repeated_calls():
    candidates = _candidates(5)
    first = FakeReranker().score("q", candidates)
    second = FakeReranker().score("q", candidates)
    assert first == second


def test_fake_reranker_output_order_matches_input_order():
    candidates = _candidates(6)
    scores = FakeReranker().score("q", candidates)
    assert [s.uid for s in scores] == [c.uid for c in candidates]


def test_empty_candidate_list_returns_empty_scores():
    assert FakeReranker().score("q", []) == []


# --- candidates_from_hits ----------------------------------------------------

def test_candidates_from_hits_preserves_uid_label_name_and_windows_the_summary():
    long_summary = " ".join(f"word{i}" for i in range(2000))
    hits = [
        SearchHit(uid="a", label="WorkItem", name="Ticket A", summary=long_summary, score=1.0),
        SearchHit(uid="b", label="Document", name="Doc B", summary="short text", score=0.5),
    ]
    candidates = candidates_from_hits("what does word5 refer to", hits, tokens=20)
    assert [c.uid for c in candidates] == ["a", "b"]
    assert candidates[0].label == "WorkItem"
    assert candidates[0].name == "Ticket A"
    # The long summary must actually have been windowed down, not passed
    # through untouched.
    assert candidates[0].window != long_summary
    # The already-short summary fits within budget and passes through
    # unchanged, per best_window's own contract.
    assert candidates[1].window == "short text"


# --- select_final: mechanism, faithfully ordered (threshold -> diversity/dedupe -> caps) --

def _scores(pairs: list[tuple[str, float]]) -> list[RerankScore]:
    return [RerankScore(uid=uid, score=score, model="m", model_version="v1") for uid, score in pairs]


def test_select_final_threshold_filters_low_scores():
    scores = _scores([("a", 0.9), ("b", 0.8), ("c", 0.1)])
    kept = select_final(scores, threshold=0.5, min_keep=0, max_keep=10)
    assert {s.uid for s in kept} == {"a", "b"}


def test_select_final_dedupes_by_uid_keeping_the_higher_score():
    # Two RerankScores for the same uid (e.g. re-scored across a retry) --
    # the diversity placeholder is "dedupe by uid only"; the globally
    # highest-scoring row for that uid must be the one kept.
    scores = _scores([("a", 0.9), ("a", 0.2), ("b", 0.5)])
    kept = select_final(scores, threshold=0.0, min_keep=0, max_keep=10)
    assert [s.uid for s in kept] == ["a", "b"]
    assert [s.score for s in kept] == [0.9, 0.5]


def test_select_final_never_returns_fewer_than_min_keep_when_enough_candidates_exist():
    # Only "a" clears threshold, but 5 distinct candidates exist overall --
    # min_keep=3 must be backfilled from the next-highest scores regardless
    # of threshold.
    scores = _scores([("a", 0.9), ("b", 0.4), ("c", 0.3), ("d", 0.2), ("e", 0.1)])
    kept = select_final(scores, threshold=0.8, min_keep=3, max_keep=10)
    assert len(kept) == 3
    assert [s.uid for s in kept] == ["a", "b", "c"]  # top 3 overall, backfilled


def test_select_final_cannot_manufacture_candidates_below_min_keep():
    # Only 2 distinct candidates exist at all; min_keep=5 cannot invent 3
    # more -- both are returned, nothing more.
    scores = _scores([("a", 0.9), ("b", 0.1)])
    kept = select_final(scores, threshold=0.0, min_keep=5, max_keep=10)
    assert {s.uid for s in kept} == {"a", "b"}
    assert len(kept) == 2


def test_select_final_enforces_max_keep_hard_cap():
    scores = _scores([(f"u{i}", 1.0 - i * 0.01) for i in range(20)])
    kept = select_final(scores, threshold=-1.0, min_keep=0, max_keep=5)
    assert len(kept) == 5
    assert [s.uid for s in kept] == [f"u{i}" for i in range(5)]  # highest-scoring 5


def test_select_final_max_keep_wins_when_it_conflicts_with_min_keep():
    # max_keep is a hard cap and always applies last, even if it cuts below
    # what min_keep would otherwise have kept -- the plan calls both "hard
    # resource caps", with max_keep applied after backfilling for min_keep.
    scores = _scores([("a", 0.9), ("b", 0.8), ("c", 0.7)])
    kept = select_final(scores, threshold=0.0, min_keep=3, max_keep=1)
    assert len(kept) == 1
    assert kept[0].uid == "a"


def test_select_final_default_threshold_is_a_documented_placeholder_that_disables_filtering():
    # DEFAULT_THRESHOLD (-inf) is explicitly a placeholder (25-plan.md §2.4
    # is out of scope) -- verify it behaves as documented: every candidate
    # passes threshold, only the caps decide what's kept.
    scores = _scores([("a", -1000.0), ("b", 5.0)])
    kept = select_final(scores, min_keep=0, max_keep=10)
    assert {s.uid for s in kept} == {"a", "b"}


def test_select_final_is_deterministic():
    scores = _scores([("a", 0.9), ("b", 0.8), ("c", 0.7), ("d", 0.6)])
    first = select_final(scores, threshold=0.0, min_keep=2, max_keep=3)
    second = select_final(scores, threshold=0.0, min_keep=2, max_keep=3)
    assert first == second


