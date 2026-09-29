from __future__ import annotations

from dataclasses import dataclass

from connectors.core.ledger import ConnectorLedger, DropReason, PendingChunk, SemanticStatus
from graph.ingestion.semantic_pass import _gate_laya_triage, run_semantic_pass
from graph.ingestion.laya import LayaTriageClassifier, TriageDecision


def test_initial_skip_rule_and_shadow_mode():
    chunk = PendingChunk("jira:c:work_item:X-1", "c1", 0, "calendar invite")
    decision = TriageDecision("scheduling", 0.9, "laya:test")
    assert _gate_laya_triage(chunk, decision, mode="shadow") is None
    drop = _gate_laya_triage(chunk, decision, mode="enforce")
    assert drop is not None
    assert drop.reason == DropReason.LAYA_TRIAGE_SKIP
    assert "durable_p=0.900000" in (drop.detail or "")


def test_classifier_reads_both_trained_answers(tmp_path):
    questions = tmp_path / "questions.json"
    questions.write_text(__import__("json").dumps({
        "chunk_type": LayaTriageClassifier.CHUNK_TYPE_QUESTION,
        "has_durable_fact": LayaTriageClassifier.DURABLE_QUESTION,
    }))

    class Agent:
        def predict_batch(self, states, questions, **kwargs):
            assert list(questions) == ["chunk_type", "has_durable_fact"]
            return [{"answers": {
                "chunk_type": {"choice": "discussion", "confidence": 0.8},
                "has_durable_fact": {"noul": 0.2},
            }} for _ in states]

    scorer = LayaTriageClassifier(
        str(tmp_path), agent_factory=lambda _path, _device: Agent(),
    )
    [decision] = scorer.classify_batch([
        PendingChunk("notion:c:page:p1", "c1", 0, "Maybe we should discuss it")
    ])
    assert decision.chunk_type == "discussion"
    assert decision.durable_probability == 0.2
    assert decision.would_skip is True


@dataclass
class _FixedClassifier:
    decision: TriageDecision

    def classify_batch(self, chunks):
        return [self.decision for _ in chunks]


def test_enforce_commits_skip_and_records_measured_cost(tmp_path, monkeypatch):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    key = "jira:c:work_item:X-1"
    ledger.commit(key, "hash", primary_node_uid="uid-1", semantic_status=SemanticStatus.PENDING)
    ledger.save_chunks(key, [("c1", 0, "automated notification")])
    monkeypatch.setenv("NEURON_TRIAGE", "enforce")

    class EmptyGraph:
        def query(self, *_args, **_kwargs):
            return type("Result", (), {"result_set": []})()

    result = run_semantic_pass(
        graph=EmptyGraph(), ledger=ledger, client=object(), model="unused",
        triage_classifier=_FixedClassifier(TriageDecision("noise", 0.01, "laya:test")),
    )

    assert result.llm_calls == 0
    assert ledger.pending_chunks(10) == []
    assert ledger.drop_counts() == {str(DropReason.LAYA_TRIAGE_SKIP): 1}
    assert ledger.triage_report() == {"scored": 1, "would_skip": 1, "lost_facts": 0}
