from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkPolicy:
    target_tokens: int = 2_000
    hard_max_tokens: int = 3_500
    overlap_tokens: int = 0

    def __post_init__(self) -> None:
        if self.target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if self.hard_max_tokens < self.target_tokens:
            raise ValueError("hard_max_tokens must be >= target_tokens")
        if self.overlap_tokens != 0:
            raise ValueError("Graph ingestion currently requires zero-overlap chunks")


@dataclass(frozen=True)
class SourceChunk:
    original_entity_id: str
    chunk_id: str
    chunk_index: int
    text: str
    token_count: int
    content_hash: str
    route: str
    heading_path: tuple[str, ...] = ()
    page_number: int | None = None
