"""Laya `same_entity` scoring for the gray-zone resolution rung."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable


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
