"""AST-aware code boundaries with a lossless fallback for other languages."""

from __future__ import annotations

import ast

from connectors.core.chunking.models import ChunkPolicy
from connectors.core.chunking.semantic import pack_units
from connectors.core.chunking.tokens import TokenCounter, enforce_hard_limit


def _python_units(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [text]
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and hasattr(node, "lineno")
    ]
    if not nodes:
        return [text]
    lines = text.splitlines(keepends=True)
    starts = [max(0, node.lineno - 1) for node in nodes]
    units: list[str] = []
    if starts[0] > 0:
        prefix = "".join(lines[: starts[0]]).strip()
        if prefix:
            units.append(prefix)
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        unit = "".join(lines[start:end]).strip()
        if unit:
            units.append(unit)
    return units


def chunk_code(
    text: str,
    language: str | None,
    policy: ChunkPolicy,
    counter: TokenCounter,
) -> tuple[list[str], str]:
    normalized_language = (language or "").lower().replace("-", "_")
    if normalized_language in {"python", "py"}:
        units = _python_units(text)
        route = "code_ast_python"
    else:
        units = [text]
        route = "code_token_fallback"
    packed = pack_units(units, policy.target_tokens, counter)
    return enforce_hard_limit(packed, policy.hard_max_tokens, counter), route
