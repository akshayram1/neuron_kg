"""Laya adapter for temporal text-fact conflict classification."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable


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
