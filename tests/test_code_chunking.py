from __future__ import annotations

import importlib

from tree_sitter import Language

from connectors.core.chunking.code import chunk_code
from connectors.core.chunking.code_parser import parse_code
from connectors.core.chunking.code_parser.lang_config import LANGUAGES
from connectors.core.chunking.models import ChunkPolicy
from connectors.core.chunking.tokens import RegexTokenCounter


def test_every_pinned_grammar_constructs_with_tree_sitter_abi():
    for name, config in sorted(LANGUAGES.items()):
        module = importlib.import_module(config.ts_module)
        factory = getattr(module, config.ts_language_fn, None)
        assert factory is not None, f"{name}: missing {config.ts_language_fn}()"
        Language(factory())


def test_all_18_languages_parse_a_named_definition():
    samples = {
        "python": b"def run():\n    return 1\n",
        "javascript": b"function run() { return 1; }\n",
        "typescript": b"function run(): number { return 1; }\n",
        "tsx": b"function App() { return <div/>; }\n",
        "c": b"int run(void) { return 1; }\n",
        "cpp": b"int run() { return 1; }\n",
        "csharp": b"class App { int Run() { return 1; } }\n",
        "java": b"class App { int run() { return 1; } }\n",
        "kotlin": b"fun run(): Int { return 1 }\n",
        "scala": b"def run(): Int = 1\n",
        "groovy": b"class App { def run() { return 1 } }\n",
        "go": b"package main\nfunc run() int { return 1 }\n",
        "rust": b"fn run() -> i32 { 1 }\n",
        "ruby": b"def run\n  1\nend\n",
        "php": b"<?php function run() { return 1; }\n",
        "swift": b"func run() -> Int { return 1 }\n",
        "dart": b"int run() => 1;\n",
        "lua": b"function run() return 1 end\n",
    }
    assert set(samples) == set(LANGUAGES)
    for language, source in samples.items():
        parsed = parse_code(source, language)
        assert any(symbol.name for symbol in parsed.symbols), language


def test_code_chunks_keep_only_comments_and_docstrings():
    """The semantic pass only wants human-authored rationale, not executable
    code -- the LLM rejects nearly every fact it's asked to consider from raw
    code bodies (verified on real extraction runs), so shipping the bodies
    just spends the whole chunk's token budget on content that gets discarded.
    """
    source = """[SOURCE]
Kind: SourceFile
Repository: acme/login

import os

class Limiter:
    def allow(self):
        \"\"\"Grant access. Fails closed if the backing store is unreachable.\"\"\"
        return True

    def deny(self):
        return False

def build():
    # Wired here, not in __init__, so tests can swap the backing store.
    return Limiter()
"""
    counter = RegexTokenCounter()
    chunks, route = chunk_code(
        source, "py", ChunkPolicy(target_tokens=200, hard_max_tokens=300), counter,
    )
    joined = "\n".join(chunks)
    assert route == "code_tree_sitter_python_comments"
    # The rationale survives...
    assert "Fails closed if the backing store is unreachable" in joined
    assert "Wired here, not in __init__" in joined
    # ...but the executable bodies it was attached to do not.
    assert "return True" not in joined
    assert "return Limiter()" not in joined
    # `deny` has no comment or docstring at all -- nothing to extract, so it
    # contributes no chunk, not an empty one.
    assert "deny" not in joined


def test_tree_sitter_adapter_still_tiles_leaf_units_without_overlap():
    """`_leaf_units` (the pre-comment-filter tiling) is exercised directly:
    the comment-only filter in `chunk_code` would hide a tiling regression
    that duplicated or dropped a whole symbol's *text*, since a docstring-only
    symbol contributes nothing either way once filtered."""
    from connectors.core.chunking.code import _leaf_units
    from connectors.core.chunking.code_parser import parse_code

    source = """import os

class Limiter:
    def allow(self):
        return True

    def deny(self):
        return False

def build():
    return Limiter()
"""
    parsed = parse_code(source.encode("utf-8"), "python")
    units = _leaf_units(parsed)
    joined = "\n".join(units)
    assert joined.count("def allow") == 1
    assert joined.count("def deny") == 1
    assert joined.count("def build") == 1


def test_comment_with_no_docstring_or_comment_anywhere_yields_no_chunks():
    """A file that parses cleanly but has nothing worth extracting should
    produce zero LLM-facing chunks -- not a fallback to the raw file text.
    That fallback is reserved for when tree-sitter itself couldn't parse."""
    source = "def add(a, b):\n    return a + b\n"
    chunks, route = chunk_code(
        source, "py", ChunkPolicy(target_tokens=200, hard_max_tokens=300), RegexTokenCounter(),
    )
    assert route == "code_tree_sitter_python_comments"
    assert chunks == []


def test_unknown_language_keeps_token_fallback_contract():
    chunks, route = chunk_code(
        "alpha beta gamma", "brainfuck",
        ChunkPolicy(target_tokens=2, hard_max_tokens=2), RegexTokenCounter(),
    )
    assert route == "code_token_fallback"
    assert chunks == ["alpha beta", "gamma"]
