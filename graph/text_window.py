"""Token-aware windowing over long text, scored by overlap with a query.

`best_window` slides overlapping windows of `tokens` tokens over `text` and
returns the one that shares the most distinct query terms with `question`.
It exists so a long `SourceRecord.summary` (or, in Phase 2, a reranker
candidate node) can be cut down to a fixed token budget without slicing mid
token and without just taking the head of the text, which is often not the
part that answers the question -- per `25-plan.md` §1.2/§2.1, this module is
shared by both the context-packing path (`graph/chat.py`) and the reranker
candidate path (`graph/rerank.py`).

Token boundaries are counted with `encoder`, the same tiktoken convention
already used in this repo (see `graph.vector_store._encoding =
tiktoken.get_encoding("cl100k_base")`): any object exposing `.encode(str) ->
list[int]` and `.decode(list[int]) -> str`, tiktoken's actual interface.
Windowing operates on those token ids directly, so a window can never split
a token -- there is no string-slicing step that could land inside one.

Deliberately dependency-light: no imports beyond the standard library, so
Phase 2's reranker can reuse it without pulling in anything new.
"""

from __future__ import annotations

import re

# Matches graph.search._WORD_RE's word shape; the length-3 floor is the
# plan's own rule (25-plan.md §1.2: "score by count of question terms (>= 3
# chars)") -- short words (a, in, of, is...) are near-universal and would
# score every window the same, so filtering by length is a cheap stand-in
# for "content word" without carrying a stopword list.
_TERM_RE = re.compile(r"[A-Za-z0-9_]{3,}")

# 50% overlap, matching the char-based windowing this module's token-based
# version replaces (25-plan.md §1.2: "slide a window ... with 50% overlap").
_OVERLAP_RATIO = 0.5


def _query_terms(question: str) -> set[str]:
    """Lowercased, deduplicated terms (>= 3 chars) from `question`."""
    return {term.lower() for term in _TERM_RE.findall(question)}


def _token_windows(
    token_count: int, size: int, *, overlap: float = _OVERLAP_RATIO,
) -> list[tuple[int, int]]:
    """Overlapping `[start, end)` index ranges over `token_count` token ids.

    General-purpose over an index range rather than the token id list itself,
    so it has nothing repo- or encoder-specific in it and is trivial to unit
    test without a real tokenizer.

    The step between window starts is `size * (1 - overlap)`, floored at 1
    token so a degenerate `size`/`overlap` combination can't loop forever.
    The final window is clipped to `token_count` instead of dropped or
    padded past the end, so windows always cover the whole range with no
    gaps, including a short tail smaller than `size`.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if token_count <= 0:
        return [(0, 0)]
    step = max(1, int(size * (1 - overlap)))
    windows = []
    start = 0
    while True:
        end = min(start + size, token_count)
        windows.append((start, end))
        if end >= token_count:
            break
        start += step
    return windows


def _overlap_score(window_text: str, terms: set[str]) -> int:
    """Count of distinct `terms` that appear (as a substring) in
    `window_text`, case-insensitively.

    Substring containment rather than re-tokenizing the window: windows are
    only ever compared against each other for the same `text`/`question`
    pair, so an absolute or precisely-tokenized relevance score isn't
    needed -- just a stable ranking.
    """
    if not terms:
        return 0
    lowered = window_text.lower()
    return sum(1 for term in terms if term in lowered)


def best_window(question: str, text: str, tokens: int, encoder) -> str:
    """Return the `tokens`-token slice of `text` most relevant to `question`.

    If `text` already fits within `tokens` tokens (per `encoder`), it is
    returned unchanged -- byte-for-byte, no windowing or scoring happens.
    Otherwise, overlapping `tokens`-sized windows are generated over the
    token ids, each is decoded back to text and scored by how many distinct
    query terms (>= 3 chars) it contains, and the highest-scoring window's
    text is returned. Ties keep the earliest (leftmost) window, which for a
    single relevant passage embedded in filler is usually also the first one
    to fully contain it.
    """
    if not text:
        return text
    token_ids = encoder.encode(text)
    if len(token_ids) <= tokens:
        return text

    terms = _query_terms(question)
    best_text = None
    best_score = -1
    for start, end in _token_windows(len(token_ids), tokens):
        window_text = encoder.decode(token_ids[start:end])
        score = _overlap_score(window_text, terms)
        if score > best_score:
            best_score = score
            best_text = window_text
    return best_text
