"""Tree-sitter-aware code chunking with a lossless token fallback.

The parser engine/config/model layer is adapted from PipesHub under Apache-2.0.
This module is Neuron's intentionally small adapter: it flattens the parser's
hierarchy into non-overlapping leaf spans and retains the existing chunking API.
"""

from __future__ import annotations

from collections import defaultdict

from connectors.core.chunking.code_parser import parse_code
from connectors.core.chunking.code_parser.lang_config import (
    LanguageConfig,
    config_for_extension,
    config_for_language,
)
from connectors.core.chunking.code_parser.models import ParsedFile, ParsedSymbol
from connectors.core.chunking.models import ChunkPolicy
from connectors.core.chunking.semantic import pack_units
from connectors.core.chunking.tokens import TokenCounter, enforce_hard_limit


_LANGUAGE_ALIASES = {
    "py": "python",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "ts": "typescript", "mts": "typescript", "cts": "typescript",
    "c++": "cpp", "cc": "cpp", "cxx": "cpp", "h": "cpp", "hpp": "cpp", "hxx": "cpp",
    "c#": "csharp", "c_sharp": "csharp", "cs": "csharp",
    "kt": "kotlin", "kts": "kotlin",
    "gradle": "groovy",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
}


def _canonical_language(language: str | None) -> str | None:
    value = (language or "").strip().lower().replace("-", "_").lstrip(".")
    value = _LANGUAGE_ALIASES.get(value, value)
    cfg = config_for_language(value) or config_for_extension(value)
    return cfg.name if cfg else None


def _split_source_preamble(text: str) -> tuple[str | None, str]:
    """Keep Neuron's synthetic source header out of the grammar parser."""
    if not text.startswith("[SOURCE]\n"):
        return None, text
    preamble, separator, body = text.partition("\n\n")
    return (preamble if separator else None), (body if separator else text)


def _leaf_symbols(parsed: ParsedFile) -> list[ParsedSymbol]:
    """Return a source-ordered, non-overlapping tiling of the parsed file.

    Container symbols retain their whole text while their children tile that
    same range. Recursively replacing each container with its direct children
    preserves method-level boundaries without embedding the same bytes twice.
    """
    children: defaultdict[int | None, list[int]] = defaultdict(list)
    for index, symbol in enumerate(parsed.symbols):
        children[symbol.parent].append(index)

    leaves: list[ParsedSymbol] = []

    def visit(index: int) -> None:
        symbol = parsed.symbols[index]
        nested = children.get(index, [])
        if symbol.is_container and nested:
            for child_index in nested:
                visit(child_index)
            return
        if symbol.text.strip():
            leaves.append(symbol)

    for root_index in children.get(None, []):
        visit(root_index)
    return leaves


def _leaf_units(parsed: ParsedFile) -> list[str]:
    """Full-text tiling (used by the existing structural tests)."""
    return [symbol.text.strip() for symbol in _leaf_symbols(parsed)]


def _comment_lines(text: str, docstring_style: str, doc_line_prefixes: tuple[str, ...]) -> str:
    """Keep only comment/docstring lines from a symbol's text, in source order.

    Executable code is almost never a Decision/Term (the semantic-pass LLM
    already rejects the overwhelming majority of code-body facts it's asked
    to consider from raw source — see CHECKLIST), so sending it wastes the
    whole chunk's token budget. The rationale that *is* worth extracting lives
    in prose humans wrote: docstrings and comments, wherever they fall inside
    the symbol, not only immediately before its signature (verified against a
    real repo: a load-bearing constraint comment sat mid-function, not at the
    top of the docstring).
    """
    if docstring_style == "none":
        return ""
    lines = text.splitlines()
    kept: list[str] = []
    in_block = False
    in_python_doc = False
    doc_quote = ""
    for line in lines:
        stripped = line.strip()
        if docstring_style == "python":
            if in_python_doc:
                kept.append(line)
                if doc_quote in stripped:
                    in_python_doc = False
                continue
            matched_quote = next((q for q in ('"""', "'''") if stripped.startswith(q)), None)
            if matched_quote:
                kept.append(line)
                doc_quote = matched_quote
                if stripped.count(matched_quote) < 2:
                    in_python_doc = True
                continue
            if stripped.startswith("#"):
                kept.append(line)
            continue
        if docstring_style == "block_comment":
            if in_block:
                kept.append(line)
                if "*/" in stripped:
                    in_block = False
                continue
            if stripped.startswith("/*"):
                kept.append(line)
                if "*/" not in stripped:
                    in_block = True
                continue
            if stripped.startswith("//") or stripped.startswith("#"):
                kept.append(line)
            continue
        if docstring_style == "line_comment":
            if any(stripped.startswith(prefix) for prefix in doc_line_prefixes):
                kept.append(line)
            continue
    return "\n".join(kept)


def _comment_units(parsed: ParsedFile, config: LanguageConfig | None) -> list[str]:
    """Leaf units reduced to their comment/docstring content, with a one-line
    name anchor for named definitions so a packed chunk mixing several
    symbols still tells the LLM (and a human reviewing evidence) which
    definition each note belongs to. A symbol with no comment contributes
    nothing -- that's the entire point, not a gap to fill."""
    style = config.docstring_style if config else "none"
    prefixes = config.doc_line_prefixes if config else ()
    units: list[str] = []
    for symbol in _leaf_symbols(parsed):
        comment = _comment_lines(symbol.text, style, prefixes).strip()
        if not comment:
            continue
        if symbol.name:
            qualified = ".".join((*symbol.parent_chain, symbol.name))
            units.append(f"# {symbol.kind} {qualified}\n{comment}")
        else:
            units.append(comment)
    return units


def chunk_code(
    text: str,
    language: str | None,
    policy: ChunkPolicy,
    counter: TokenCounter,
) -> tuple[list[str], str]:
    """Chunk code for the semantic pass -- comments/docstrings only.

    Empty units are a valid, common outcome here: a symbol (or a whole file)
    with no comment has nothing for the LLM to do, and skipping it is
    correct, not a fallback condition. The raw-text fallback below fires only
    when tree-sitter itself couldn't parse the file (unsupported language or
    a real parse failure) -- there, there's no structure to extract comments
    from, so the whole text is the only option left.
    """
    canonical = _canonical_language(language)
    if canonical is None:
        units = [text]
        route = "code_token_fallback"
    else:
        preamble, source = _split_source_preamble(text)
        parsed = parse_code(source.encode("utf-8"), canonical)
        if parsed.skipped_reason:
            units = [text]
            route = "code_token_fallback"
        else:
            config = config_for_language(canonical)
            units = _comment_units(parsed, config)
            if preamble and units:
                units.insert(0, preamble)
            route = f"code_tree_sitter_{canonical}_comments"

    packed = pack_units(units, policy.target_tokens, counter)
    return enforce_hard_limit(packed, policy.hard_max_tokens, counter), route
