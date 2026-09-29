"""Laya adapters used while writing the graph.

Triage decides whether a chunk is worth semantic extraction. Same-entity
scoring covers the gray zone of identity resolution. Fact-update classification
decides whether new text duplicates, updates, contradicts, or extends a live fact.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from connectors.core.ledger import PendingChunk


@dataclass(frozen=True)
class TriageDecision:
    chunk_type: str
    durable_probability: float
    model: str

    @property
    def would_skip(self) -> bool:
        return (
            self.chunk_type in {"noise", "scheduling"}
            or (self.chunk_type == "discussion" and self.durable_probability < 0.3)
            or self.durable_probability < 0.15
        )


class LayaTriageClassifier:
    """Lazy checkpoint-validated adapter for the two trained triage heads."""

    CHUNK_TYPE_QUESTION = {
        "type": "choice",
        "instructions": "What kind of content is this chunk?",
        "criteria": {
            "decision": "a choice that was made or approved",
            "action_item": "a task someone must do",
            "status_update": "progress or state change of work",
            "fact_statement": "a stable fact about people, systems or projects",
            "request": "a question or ask directed at someone",
            "scheduling": "meeting time, invite, reschedule",
            "discussion": "opinions or back-and-forth without an outcome",
            "noise": "signatures, boilerplate, auto-notifications",
        },
    }
    DURABLE_QUESTION = {
        "type": "noul",
        "instructions": "Does this chunk state a fact worth storing in the knowledge graph?",
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
        self._questions: dict | None = None
        self._version = "unknown"
        self._lock = threading.Lock()

    def _load(self):
        if self._agent is not None:
            return self._agent
        if not self.model_dir:
            raise RuntimeError("LAYA_MODEL_DIR is required when NEURON_TRIAGE is enabled")
        model_dir = Path(self.model_dir).expanduser()
        question_path = model_dir / "questions.json"
        if not question_path.is_file():
            raise RuntimeError(f"Laya checkpoint is missing questions.json: {model_dir}")
        trained = json.loads(question_path.read_text())
        if trained.get("chunk_type") != self.CHUNK_TYPE_QUESTION:
            raise RuntimeError("Laya chunk_type schema does not match the trained contract")
        if trained.get("has_durable_fact") != self.DURABLE_QUESTION:
            raise RuntimeError("Laya has_durable_fact schema does not match the trained contract")
        if self._agent_factory is None:
            import laya
            self._agent = laya.Agent(str(model_dir), device=self.device)
        else:
            self._agent = self._agent_factory(str(model_dir), self.device)
        self._questions = {
            "chunk_type": trained["chunk_type"],
            "has_durable_fact": trained["has_durable_fact"],
        }
        self._version = hashlib.sha256(question_path.read_bytes()).hexdigest()[:12]
        return self._agent

    def classify_batch(self, chunks: list[PendingChunk]) -> list[TriageDecision]:
        if not chunks:
            return []
        states = [
            {"source": chunk.record_key.split(":", 1)[0], "text": chunk.text[:1600]}
            for chunk in chunks
        ]
        with self._lock:
            agent = self._load()
            predictions = agent.predict_batch(
                states,
                self._questions,
                batch_size=self.batch_size,
                sort_by_length=len(states) > self.batch_size,
            )
        if len(predictions) != len(chunks):
            raise RuntimeError(
                f"Laya returned {len(predictions)} triage results for {len(chunks)} chunks"
            )
        output: list[TriageDecision] = []
        for prediction in predictions:
            answers = prediction["answers"]
            output.append(TriageDecision(
                chunk_type=str(answers["chunk_type"]["choice"]),
                durable_probability=float(answers["has_durable_fact"]["noul"]),
                model=f"laya/triage:{self._version}",
            ))
        return output


class LayaSameEntityClassifier:
    QUESTION = {
        "type": "noul",
        "instructions": "Does the mention refer to the same real-world entity as the candidate node?",
    }

    def __init__(
        self, model_dir: str | None = None, device: str | None = None,
        batch_size: int = 8, *, agent_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        self.model_dir = model_dir or os.getenv("LAYA_MODEL_DIR")
        self.device = device or os.getenv("LAYA_DEVICE", "cpu")
        self.batch_size = batch_size
        self._factory = agent_factory
        self._agent = None
        self._lock = threading.Lock()

    def _load(self):
        if self._agent is not None:
            return self._agent
        if not self.model_dir:
            raise RuntimeError("LAYA_MODEL_DIR is required for same_entity scoring")
        path = Path(self.model_dir).expanduser()
        question_path = path / "questions.json"
        if not question_path.is_file():
            raise RuntimeError(f"Laya checkpoint is missing questions.json: {path}")
        questions = json.loads(question_path.read_text())
        if questions.get("same_entity") != self.QUESTION:
            raise RuntimeError("Laya same_entity schema does not match the trained contract")
        if self._factory is None:
            import laya
            self._agent = laya.Agent(str(path), device=self.device)
        else:
            self._agent = self._factory(str(path), self.device)
        return self._agent

    def score_batch(self, states: list[dict[str, str]]) -> list[float]:
        if not states:
            return []
        with self._lock:
            predictions = self._load().predict_batch(
                states, {"same_entity": self.QUESTION}, batch_size=self.batch_size,
                sort_by_length=len(states) > self.batch_size,
            )
        if len(predictions) != len(states):
            raise RuntimeError(
                f"Laya returned {len(predictions)} same_entity results for {len(states)} candidates"
            )
        return [
            float(item["answers"]["same_entity"]["noul"])
            for item in predictions
        ]


class LayaFactUpdateClassifier:
    QUESTION = {
        "type": "choice",
        "instructions": "How does the new evidence relate to the existing fact?",
        "criteria": {
            "duplicate": "says the same thing",
            "updates": "same subject, newer value replaces the old one",
            "contradicts": "conflicts without being clearly newer",
            "extends": "adds detail, old fact stays true",
            "unrelated": "about something else",
        },
    }

    def __init__(
        self, model_dir: str | None = None, device: str | None = None,
        *, agent_factory: Callable[[str, str], object] | None = None,
    ) -> None:
        self.model_dir = model_dir or os.getenv("LAYA_MODEL_DIR")
        self.device = device or os.getenv("LAYA_DEVICE", "cpu")
        self._factory = agent_factory
        self._agent = None
        self._lock = threading.Lock()

    def _load(self):
        if self._agent is not None:
            return self._agent
        if not self.model_dir:
            raise RuntimeError("LAYA_MODEL_DIR is required for fact_update scoring")
        path = Path(self.model_dir).expanduser()
        question_path = path / "questions.json"
        if not question_path.is_file():
            raise RuntimeError(f"Laya checkpoint is missing questions.json: {path}")
        questions = json.loads(question_path.read_text())
        if questions.get("fact_update") != self.QUESTION:
            raise RuntimeError("Laya fact_update schema does not match the trained contract")
        if self._factory is None:
            import laya
            self._agent = laya.Agent(str(path), device=self.device)
        else:
            self._agent = self._factory(str(path), self.device)
        return self._agent

    def classify(self, state: dict[str, str | None]) -> tuple[str, float]:
        with self._lock:
            prediction = self._load().predict(state, {"fact_update": self.QUESTION})
        answer = prediction["answers"]["fact_update"]
        return str(answer["choice"]), float(
            answer.get("confidence", answer.get("answer_confidence", 0.0))
        )
