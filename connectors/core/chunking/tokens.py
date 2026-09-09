"""Token accounting with an exact tiktoken path and safe local fallback."""

from __future__ import annotations

import re
import logging
from functools import lru_cache
from typing import Protocol

_TOKEN_PATTERN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
logger = logging.getLogger(__name__)


class TokenCounter(Protocol):
    exact: bool

    def count(self, text: str) -> int: ...

    def split(self, text: str, max_tokens: int) -> list[str]: ...


class RegexTokenCounter:
    """Offline fallback. It over-splits rather than risk overflowing a prompt."""

    exact = False

    def count(self, text: str) -> int:
        return len(_TOKEN_PATTERN.findall(text))

    def split(self, text: str, max_tokens: int) -> list[str]:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        matches = list(_TOKEN_PATTERN.finditer(text))
        if len(matches) <= max_tokens:
            return [text] if text else []
        output: list[str] = []
        for start in range(0, len(matches), max_tokens):
            group = matches[start : start + max_tokens]
            begin = group[0].start()
            end = (
                matches[start + max_tokens].start()
                if start + max_tokens < len(matches)
                else len(text)
            )
            piece = text[begin:end].strip()
            if piece:
                output.append(piece)
        return output


class TiktokenCounter:
    exact = True

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken

        self.encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text, disallowed_special=()))

    def split(self, text: str, max_tokens: int) -> list[str]:
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        tokens = self.encoding.encode(text, disallowed_special=())
        return [
            self.encoding.decode(tokens[index : index + max_tokens]).strip()
            for index in range(0, len(tokens), max_tokens)
            if tokens[index : index + max_tokens]
        ]


@lru_cache(maxsize=1)
def default_token_counter() -> TokenCounter:
    try:
        return TiktokenCounter()
    except Exception as exc:
        # Some tiktoken releases fetch their BPE table on first use. Source
        # ingestion must remain available during an offline deployment/startup.
        logger.warning("cl100k_base unavailable; using approximate local token count: %s", exc)
        return RegexTokenCounter()


def enforce_hard_limit(texts: list[str], hard_max_tokens: int, counter: TokenCounter) -> list[str]:
    output: list[str] = []
    for text in texts:
        if counter.count(text) <= hard_max_tokens:
            if text.strip():
                output.append(text.strip())
        else:
            output.extend(counter.split(text, hard_max_tokens))
    return output
