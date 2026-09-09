"""Meaning-preserving text chunking without an external model call.

Headings and paragraphs are kept as the first boundaries. Sentences are used
only when a paragraph itself exceeds the target. A future Chonkie boundary
detector can replace ``semantic_units`` without changing connector adapters.
"""

from __future__ import annotations

import re

from connectors.core.chunking.models import ChunkPolicy
from connectors.core.chunking.tokens import TokenCounter, enforce_hard_limit

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])(?:[ \t]+|\n+)")
_HEADING = re.compile(r"^(?:#{1,6}\s+.+|[A-Z][A-Z0-9 /&:_-]{3,}|\d+(?:\.\d+)*[.)]?\s+.+)$")


def semantic_units(text: str, target_tokens: int, counter: TokenCounter) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    units: list[str] = []
    for paragraph in paragraphs:
        if _HEADING.fullmatch(paragraph) or counter.count(paragraph) <= target_tokens:
            units.append(paragraph)
            continue
        sentences = [part.strip() for part in _SENTENCE_BOUNDARY.split(paragraph) if part.strip()]
        units.extend(sentences or [paragraph])
    return units


def pack_units(units: list[str], target_tokens: int, counter: TokenCounter) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        unit_tokens = counter.count(unit)
        if current and current_tokens + unit_tokens > target_tokens:
            chunks.append("\n\n".join(current))
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += unit_tokens
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_semantic(text: str, policy: ChunkPolicy, counter: TokenCounter) -> list[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    packed = pack_units(semantic_units(cleaned, policy.target_tokens, counter), policy.target_tokens, counter)
    return enforce_hard_limit(packed, policy.hard_max_tokens, counter)
