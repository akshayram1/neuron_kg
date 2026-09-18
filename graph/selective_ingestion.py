"""Choose only deterministically unresolved evidence for semantic extraction."""

from __future__ import annotations

import re
from collections.abc import Iterable

from connectors.core.chunking.models import SourceChunk
from connectors.core.ledger import ChunkWrite, SemanticStatus


_SENTENCE = re.compile(r"(?<=[.!?])(?:[ \t]+|\n+)")
_HEADER_LINE = re.compile(
    r"^(?:\[SOURCE\]|\[CONTENT\]|Kind|Name|Repository|Author|Branch|Workspace|"
    r"Provider|Entity type|Title|Path|URL|External ID|Created at|Updated at):?",
    re.IGNORECASE,
)
_ANCHOR_RELATION_WORDS = re.compile(
    r"\b(?:jira|ticket|issue|implements?|implemented|fix(?:es|ed)?|tracks?|"
    r"documents?|documented|reference[sd]?|pr|pull request|commit)\b",
    re.IGNORECASE,
)
_SEMANTIC_CUES = re.compile(
    r"\b(?:because|decid\w*|architectur\w*|migrat\w*|deprecat\w*|supersed\w*|"
    r"must|should|breaking|caveat|risk|instead|replace[sd]?|calls?\s+/(?:v\d+/)?)\b",
    re.IGNORECASE,
)


def _units(text: str) -> list[str]:
    """Small evidence units; conservative splitting keeps quotes verifiable."""
    output: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if paragraph.startswith("[SOURCE]"):
            lines = paragraph.splitlines()
            body = [line for line in lines if not _HEADER_LINE.match(line.strip())]
            if body:
                output.extend(body)
            continue
        output.extend(part.strip() for part in _SENTENCE.split(paragraph) if part.strip())
    return output


def _source_header(text: str) -> str:
    if not text.startswith("[SOURCE]"):
        return ""
    preamble, separator, _body = text.partition("\n\n")
    return preamble.strip() if separator else ""


def _covered_by_exact_anchor(unit: str, anchors: frozenset[str]) -> bool:
    folded = unit.casefold()
    matched = [anchor for anchor in anchors if anchor and anchor in folded]
    if not matched:
        return False
    # An explicit link/reference sentence has already become an exact graph
    # edge. Longer rationale around the identifier remains unresolved.
    remainder = folded
    for anchor in matched:
        remainder = remainder.replace(anchor, " ")
    words = re.findall(r"[a-z0-9]+", remainder)
    return (
        bool(_ANCHOR_RELATION_WORDS.search(unit))
        and not _SEMANTIC_CUES.search(remainder)
        and len(words) <= 24
    )


def selective_chunk_writes(
    chunks: Iterable[SourceChunk], *, resolved_anchors: frozenset[str] = frozenset(),
) -> list[ChunkWrite]:
    """Retain complete evidence while queueing only unresolved statements.

    Code chunks are already comment/docstring-only before this stage. For all
    providers, exact cross-source reference statements written by Pass 1 are
    removed; surrounding semantic claims still go to the LLM.
    """
    writes: list[ChunkWrite] = []
    for chunk in chunks:
        units = _units(chunk.text)
        unresolved = [unit for unit in units if not _covered_by_exact_anchor(unit, resolved_anchors)]
        resolved_count = len(units) - len(unresolved)
        header = _source_header(chunk.text)
        llm_text = "\n\n".join(part for part in (header, *unresolved) if part).strip()
        if not unresolved:
            status = SemanticStatus.DONE
            resolution_status = "deterministic"
            reason = f"pass1_exact_anchor:{resolved_count}" if resolved_count else "no_semantic_evidence"
            llm_text = None
        else:
            status = SemanticStatus.PENDING
            resolution_status = "partial" if resolved_count else "unresolved"
            reason = f"pass1_exact_anchor:{resolved_count}" if resolved_count else "no_deterministic_match"
        writes.append(ChunkWrite(
            chunk_id=chunk.chunk_id,
            chunk_index=chunk.chunk_index,
            text=chunk.text,
            status=status,
            llm_text=llm_text,
            resolution_status=resolution_status,
            resolution_reason=reason,
        ))
    return writes


def has_pending(writes: Iterable[ChunkWrite]) -> bool:
    return any(str(item.status) == str(SemanticStatus.PENDING) for item in writes)
