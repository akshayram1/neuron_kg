"""Standalone PDF text extraction + chunking — no cognee dependency.

Mirrors cognee's default `PyPdfLoader` behavior (pypdf, page-by-page text
extraction with page markers) plus a simple char-based chunker so extracted
text is ready to feed into `graph_extraction.extract_knowledge_graph`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def extract_pdf_text(path: str | os.PathLike) -> str:
    """Extract plain text from a PDF file, one page at a time.

    Uses `pypdf` (the same library cognee's default PDF loader uses). Each
    page is prefixed with "Page N:" so page provenance survives in the text.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"Page {index}:\n{text}")
    return "\n\n".join(pages)


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str


def chunk_text(text: str, max_chars: int = 3000, overlap: int = 200) -> list[TextChunk]:
    """Split text into overlapping character-window chunks.

    A simple, dependency-free stand-in for cognee's token-aware chunker.
    `max_chars` and `overlap` are characters, not tokens — tune down if you
    are feeding a small-context model. Chunk boundaries fall on whitespace
    where possible so words aren't split mid-token.
    """
    if max_chars <= overlap:
        raise ValueError("max_chars must be greater than overlap")

    chunks: list[TextChunk] = []
    start = 0
    length = len(text)
    index = 0

    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            # Back up to the last whitespace so we don't cut a word in half.
            split_at = text.rfind(" ", start, end)
            if split_at > start:
                end = split_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(TextChunk(index=index, text=chunk))
            index += 1

        if end >= length:
            break
        start = max(end - overlap, start + 1)

    return chunks


def load_pdf_chunks(
    path: str | os.PathLike, max_chars: int = 3000, overlap: int = 200
) -> list[TextChunk]:
    """Extract a PDF's text and split it into chunks in one call."""
    return chunk_text(extract_pdf_text(path), max_chars=max_chars, overlap=overlap)
