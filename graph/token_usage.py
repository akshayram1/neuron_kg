"""Small, provider-SDK-tolerant model token accounting helper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _value(usage: Any, *names: str) -> int:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if value is not None:
            return int(value)
    return 0


@dataclass
class TokenUsage:
    """Accumulates usage from Responses, Chat Completions and Embeddings.

    The OpenAI SDK uses ``input_tokens``/``output_tokens`` for Responses and
    ``prompt_tokens``/``completion_tokens`` for older endpoints. Supporting
    both keeps the UI counter accurate without coupling it to one API shape.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: Any) -> None:
        if usage is None:
            return
        input_tokens = _value(usage, "input_tokens", "prompt_tokens")
        output_tokens = _value(usage, "output_tokens", "completion_tokens")
        total_tokens = _value(usage, "total_tokens") or input_tokens + output_tokens
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total_tokens

    def as_dict(self, prefix: str) -> dict[str, int]:
        return {
            f"{prefix}_input_tokens": self.input_tokens,
            f"{prefix}_output_tokens": self.output_tokens,
            f"{prefix}_total_tokens": self.total_tokens,
        }
