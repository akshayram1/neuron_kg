"""Two-pass Laya retrieval tests; pure unit tests, no model or live graph."""

from __future__ import annotations

from graph import chat
from graph.rerank import RerankCandidate, RerankScore
from graph.search import SearchHit


def _hit(uid: str, *, methods: list[str] | None = None) -> SearchHit:
    return SearchHit(uid, "WorkItem", uid, f"summary {uid}", 0.5, methods or ["vector"])


class ScoreByUid:
    def __init__(self, scores: dict[str, float]):
        self.scores = scores
        self.calls: list[list[str]] = []

    def score(self, _question: str, candidates: list[RerankCandidate]) -> list[RerankScore]:
        self.calls.append([candidate.uid for candidate in candidates])
        return [
            RerankScore(candidate.uid, self.scores[candidate.uid], "fake-laya", "v1")
            for candidate in candidates
        ]


def test_configured_reranker_reads_runtime_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("NEURON_RERANK", "laya")
    monkeypatch.setenv("LAYA_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("LAYA_DEVICE", "cpu")
    monkeypatch.setattr(chat, "_laya_reranker", None)
    monkeypatch.setattr(chat, "_laya_reranker_config", None)

    configured = chat._configured_reranker()

    assert configured is not None
    assert configured.model_dir == str(tmp_path)
    assert configured.device == "cpu"


def test_reranker_status_exposes_disabled_and_ready_states(monkeypatch, tmp_path):
    assert chat.reranker_status()["mode"] == "off"

    for name in ("model.safetensors", "questions.json", "rl_agent_config.json"):
        (tmp_path / name).write_text("{}")
    monkeypatch.setenv("NEURON_RERANK", "laya")
    monkeypatch.setenv("LAYA_MODEL_DIR", str(tmp_path))

    status = chat.reranker_status()

    assert status["mode"] == "laya"
    assert status["ready"] is True
    assert status["checkpointReady"] is True


def test_runtime_switch_overrides_environment_and_can_be_cleared(monkeypatch):
    monkeypatch.setenv("NEURON_RERANK", "laya")

    assert chat.set_reranker_enabled(False)["mode"] == "off"
    assert chat.reranker_status()["source"] == "runtime"
    assert chat.set_reranker_enabled(None)["mode"] == "laya"
    assert chat.reranker_status()["source"] == "environment"


def _simple_candidates(_graph, _question, hits, **_kwargs):
    return [
        RerankCandidate(
            hit.uid, hit.summary, label=hit.label, name=hit.name,
            methods=list(hit.methods),
        )
        for hit in hits
    ]


def test_rejected_candidate_can_bridge_to_a_relevant_second_hop(monkeypatch):
    monkeypatch.setattr(chat, "_rerank_candidates", _simple_candidates)
    monkeypatch.setattr(chat, "NEURON_RERANK_MIN_DIRECT", 1)
    monkeypatch.setattr(chat, "NEURON_RERANK_MAX_ROUNDS", 2)
    monkeypatch.setattr(
        chat, "expand_neighbors",
        lambda _graph, seeds, *_args, **_kwargs: [_hit("answer", methods=["graph:IMPLEMENTS"])]
        if "bridge" in seeds else [],
    )
    scorer = ScoreByUid({"bridge": 0.30, "noise": 0.05, "answer": 0.92})
    trace = chat.RetrievalTrace()

    selected = chat._laya_two_pass_search(
        scorer, object(), "why did this happen?", [_hit("bridge"), _hit("noise")],
        scope=object(), providers=None, at=None, at_end=None, as_of=None,
        exclude_edges=frozenset(), trace=trace,
    )

    assert scorer.calls == [["bridge", "noise"], ["answer"]]
    assert [hit.uid for hit in selected] == ["answer"]
    assert "laya:direct_evidence" in selected[0].methods
    assert trace.initial_candidate_uids == ["bridge", "noise"]
    assert trace.expanded_candidate_uids == ["answer"]
    assert trace.final_uids == ["answer"]
    assert trace.expansion_rounds == 1
    assert trace.reranker == "laya"


def test_bridge_candidates_never_reach_answer_when_second_hop_is_also_rejected(monkeypatch):
    monkeypatch.setattr(chat, "_rerank_candidates", _simple_candidates)
    monkeypatch.setattr(chat, "NEURON_RERANK_MIN_DIRECT", 1)
    monkeypatch.setattr(chat, "NEURON_RERANK_MAX_ROUNDS", 1)
    monkeypatch.setattr(
        chat, "expand_neighbors",
        lambda *_args, **_kwargs: [_hit("still_weak", methods=["graph:REFERENCES"])],
    )
    scorer = ScoreByUid({"bridge": 0.25, "still_weak": 0.22})

    selected = chat._laya_two_pass_search(
        scorer, object(), "unknown question", [_hit("bridge")],
        scope=object(), providers=None, at=None, at_end=None, as_of=None,
        exclude_edges=frozenset(),
    )

    assert selected == []


def test_temporal_lane_survives_low_semantic_score_without_expansion(monkeypatch):
    monkeypatch.setattr(chat, "_rerank_candidates", _simple_candidates)
    monkeypatch.setattr(chat, "NEURON_RERANK_MIN_DIRECT", 1)
    monkeypatch.setattr(
        chat, "expand_neighbors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not expand")),
    )
    scorer = ScoreByUid({"old_owner": 0.01})

    selected = chat._laya_two_pass_search(
        scorer, object(), "who owned it in March?", [_hit("old_owner", methods=["time_window"])],
        scope=object(), providers=None, at="2026-03-01", at_end="2026-04-01",
        as_of=None, exclude_edges=frozenset(),
    )

    assert [hit.uid for hit in selected] == ["old_owner"]
    assert "laya:temporal_context" in selected[0].methods


def test_laya_failure_falls_back_to_original_rrf_pool(monkeypatch):
    general = [_hit("g1"), _hit("g2")]
    wisdom = [_hit("wisdom")]
    finding = [_hit("finding")]

    monkeypatch.setattr(chat, "resolve_structured", lambda *a, **k: None)
    monkeypatch.setattr(chat, "embed_query", lambda *a, **k: [0.0])

    def fake_search(*_args, labels=None, **_kwargs):
        if labels == ["Wisdom"]:
            return wisdom
        if labels == ["Finding"]:
            return finding
        return general

    monkeypatch.setattr(chat, "hybrid_search", fake_search)
    monkeypatch.setattr(chat, "_actionable_wisdom_hits", lambda _graph, hits: hits)
    monkeypatch.setattr(chat, "_linked_finding_hits", lambda *a, **k: [])
    monkeypatch.setattr(chat, "_two_entity_lane", lambda *a, **k: [])
    monkeypatch.setattr(chat, "find_named_persons", lambda *a, **k: [])
    monkeypatch.setattr(chat, "expand_neighbors", lambda *a, **k: [])
    monkeypatch.setattr(chat, "_rerank_candidates", _simple_candidates)

    class FailingScorer:
        def score(self, *_args, **_kwargs):
            raise RuntimeError("model unavailable")

    trace = chat.RetrievalTrace()
    _structured, hits = chat.retrieve(
        object(), object(), "ordinary question", limit=2, providers=None,
        scope=object(), reranker=FailingScorer(), trace=trace,
    )

    assert [hit.uid for hit in hits] == ["g1", "g2"]
    assert all(not method.startswith("laya:") for hit in hits for method in hit.methods)
    assert trace.fallback is True
    assert trace.reranker == "rrf"
    assert trace.fallback_reason == "RuntimeError: model unavailable"


def test_final_laya_selection_is_not_cut_back_to_old_search_limit(monkeypatch):
    selected = [
        SearchHit(f"u{i}", "Document", f"doc {i}", "text", 0.8, ["laya:direct_evidence"])
        for i in range(8)
    ]
    monkeypatch.setattr(chat, "retrieve", lambda *a, **k: (None, selected))
    captured = {}

    def fake_pack(_graph, hits, *_args, **_kwargs):
        captured["uids"] = [hit.uid for hit in hits]
        return chat._PackedContext([], [], [], {}, [], [])

    monkeypatch.setattr(chat, "_pack_context", fake_pack)

    class Responses:
        def parse(self, **_kwargs):
            parsed = type("Parsed", (), {"answer": "ok", "used_sources": []})()
            return type("Response", (), {"usage": None, "output_parsed": parsed})()

    class Client:
        responses = Responses()

    monkeypatch.setattr(chat, "_resolve_records", lambda *a, **k: {})
    monkeypatch.setattr(chat, "_resolve_knowledge_citations", lambda *a, **k: [])
    result = chat.run_chat_turn(
        object(), Client(), "question", search_limit=3, scope=object(),
    )

    assert result.answer == "ok"
    assert captured["uids"] == [f"u{i}" for i in range(8)]
    assert result.retrieval_trace is not None
