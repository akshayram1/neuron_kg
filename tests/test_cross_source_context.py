from types import SimpleNamespace

from connectors.core.ledger import PendingChunk
from graph.ingestion.cross_source_context import build_cross_source_context_provider
from graph.retrieval.rerank import RerankScore
from graph.storage import vector_store


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class _Graph:
    def __init__(self, rows):
        self.rows = rows

    def query(self, query, params=None):
        assert "MENTIONED_IN" in query
        return _Result(self.rows)


class _Scorer:
    def __init__(self, scores):
        self.scores = scores

    def score(self, question, candidates):
        return [
            RerankScore(
                uid=item.uid, score=self.scores[item.uid],
                model="test-laya", model_version="1",
            )
            for item in candidates
        ]


class _FailingScorer:
    def score(self, question, candidates):
        raise RuntimeError("model unavailable")


def _embed(_texts):
    return SimpleNamespace(vectors=[[0.1, 0.2]])


def _rows():
    return [
        [
            "jira-1", ["WorkItem"], "AUTH-1", "Jira-only issue",
            ["jira"], ["jira:conn:issue:AUTH-1"], "2026-01-01",
        ],
        [
            "notion-1", ["Document"], "Auth design", "The auth design uses Redis.",
            ["notion"], ["notion:ws:page:design"], "2026-01-02",
        ],
    ]


def test_only_other_provider_candidates_reach_laya_and_llm(monkeypatch):
    monkeypatch.setattr(
        vector_store, "search",
        lambda *args, **kwargs: [("jira-1", 0.99), ("notion-1", 0.88)],
    )
    provider = build_cross_source_context_provider(
        _Graph(_rows()), reranker=_Scorer({"notion-1": 0.91}),
        embedder=_embed, vector_client=object(), threshold=0.5,
    )

    context = provider(PendingChunk(
        "jira:conn:issue:AUTH-2", "chunk", 0,
        "[SOURCE]\nAUTH-2 follows the Auth design.",
    ))

    assert context is not None
    assert context.candidate_uids == frozenset({"notion-1"})
    assert "uid=notion-1" in context.text
    assert "kind=Document" in context.text
    assert "jira-1" not in context.text


def test_laya_rejection_sends_no_candidate_to_llm(monkeypatch):
    monkeypatch.setattr(
        vector_store, "search", lambda *args, **kwargs: [("notion-1", 0.88)],
    )
    provider = build_cross_source_context_provider(
        _Graph(_rows()[1:]), reranker=_Scorer({"notion-1": 0.2}),
        embedder=_embed, vector_client=object(), threshold=0.5,
    )

    context = provider(PendingChunk(
        "jira:conn:issue:AUTH-2", "chunk", 0, "[SOURCE]\nunrelated change",
    ))

    assert context is None


def test_laya_failure_falls_back_to_bounded_vector_order(monkeypatch):
    monkeypatch.setattr(
        vector_store, "search", lambda *args, **kwargs: [("notion-1", 0.88)],
    )
    provider = build_cross_source_context_provider(
        _Graph(_rows()[1:]), reranker=_FailingScorer(),
        embedder=_embed, vector_client=object(), top_k=1,
    )

    context = provider(PendingChunk(
        "jira:conn:issue:AUTH-2", "chunk", 0, "[SOURCE]\nAuth design",
    ))

    assert context is not None
    assert context.candidate_uids == frozenset({"notion-1"})
    assert "laya_score=off" in context.text


def test_same_record_is_never_its_own_candidate(monkeypatch):
    monkeypatch.setattr(
        vector_store, "search", lambda *args, **kwargs: [("notion-1", 0.88)],
    )
    provider = build_cross_source_context_provider(
        _Graph(_rows()[1:]), reranker=_Scorer({}),
        embedder=_embed, vector_client=object(),
    )

    context = provider(PendingChunk(
        "notion:ws:page:design", "chunk", 0, "[SOURCE]\nAuth design",
    ))

    assert context is None
