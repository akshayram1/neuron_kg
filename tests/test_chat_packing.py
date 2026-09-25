"""Unit tests for `graph/chat.py`'s plan.md §1.2 context packing
(`_pack_context` and its helpers). No live graph/OpenAI call: `_entity_evidence`,
`_knowledge_metadata` and `_mentioned_record_keys` are monkeypatched to plain
Python data, the same pattern `tests/test_chat_knowledge_layers.py` already
uses for `graph/chat.py`. `best_window`/token counting run for real against
the actual `cl100k_base` encoder so budget-enforcement claims are verified
against the real tokenizer, not a mock that could hide a miscount.
"""

from __future__ import annotations

import tiktoken

from graph import chat
from graph.search import SearchHit

_ENC = tiktoken.get_encoding("cl100k_base")


def _hit(uid: str, label: str, name: str, summary: str) -> SearchHit:
    return SearchHit(uid, label, name, summary, 0.5, ["vector"])


def _no_facts(*args, **kwargs):
    return [], [], set()


def _no_metadata(*args, **kwargs):
    return ""


def _pack(monkeypatch, hits, *, evidence_budget, entity_evidence=_no_facts, **kwargs):
    monkeypatch.setattr(chat, "_entity_evidence", entity_evidence)
    monkeypatch.setattr(chat, "_knowledge_metadata", _no_metadata)
    monkeypatch.setattr(chat, "_mentioned_record_keys", lambda *a, **k: set())
    return chat._pack_context(
        object(), hits, "what happened", structured=None, scope=object(), providers=None,
        at=None, at_end=None, as_of=None, evidence_budget=evidence_budget, **kwargs,
    )


def _long_text(word: str, count: int) -> str:
    return " ".join(f"{word}{i}" for i in range(count))


# --- hard token limit ------------------------------------------------------

def test_packing_never_exceeds_the_evidence_budget(monkeypatch):
    hits = [
        _hit(f"n{i}", "WorkItem", f"NODE-{i}", _long_text("filler", 400))
        for i in range(8)
    ]
    budget = 200
    packed = _pack(monkeypatch, hits, evidence_budget=budget)

    assembled = "\n\n".join(packed.blocks)
    join_overhead = max(0, len(packed.blocks) - 1) * len(_ENC.encode("\n\n"))
    assert len(_ENC.encode(assembled)) <= budget + join_overhead
    # A tight budget against 8 large blocks must drop or squeeze something —
    # otherwise this test would be vacuous.
    assert packed.dropped_uids or any(info.truncated for info in packed.packed_blocks)


def test_zero_budget_drops_everything_rather_than_lying(monkeypatch):
    hits = [_hit("n1", "WorkItem", "NODE-1", _long_text("filler", 100))]
    packed = _pack(monkeypatch, hits, evidence_budget=0)
    assert packed.blocks == []
    assert packed.dropped_uids == ["n1"]
    assert packed.packed_blocks == []


# --- determinism -------------------------------------------------------------

def test_packing_order_is_deterministic(monkeypatch):
    hits = [
        _hit(f"n{i}", "WorkItem", f"NODE-{i}", _long_text("word", 50 + i * 10))
        for i in range(6)
    ]
    first = _pack(monkeypatch, hits, evidence_budget=500)
    second = _pack(monkeypatch, hits, evidence_budget=500)

    assert first.blocks == second.blocks
    assert [info.uid for info in first.packed_blocks] == [info.uid for info in second.packed_blocks]
    assert first.packed_blocks == second.packed_blocks
    assert first.dropped_uids == second.dropped_uids
    # And matches simple left-to-right input order (no re-sorting by size/score).
    kept_uids = [info.uid for info in first.packed_blocks]
    assert kept_uids == [hit.uid for hit in hits if hit.uid in set(kept_uids)]


# --- support-span accounting is honest --------------------------------------

def test_short_block_is_not_reported_as_truncated(monkeypatch):
    short_text = "Akshay merged the fix on Friday."
    hits = [_hit("n1", "WorkItem", "NODE-1", short_text)]
    packed = _pack(monkeypatch, hits, evidence_budget=5000)

    assert len(packed.packed_blocks) == 1
    info = packed.packed_blocks[0]
    assert info.truncated is False
    assert info.gold_support_preserved is True
    assert info.facts_dropped == 0
    full_tokens = len(_ENC.encode(short_text))
    assert info.window_tokens == full_tokens
    assert info.packed_tokens == full_tokens


def test_windowed_block_is_honestly_reported_as_truncated(monkeypatch):
    monkeypatch.setattr(chat, "NEURON_BLOCK_TOKENS", 20)
    long_text = _long_text("filler", 200)  # far more than 20 tokens
    hits = [_hit("n1", "WorkItem", "NODE-1", long_text)]
    # Generous overall budget so only the per-block NEURON_BLOCK_TOKENS
    # window (not the evidence budget) forces the cut being tested here.
    packed = _pack(monkeypatch, hits, evidence_budget=5000)

    assert len(packed.packed_blocks) == 1
    info = packed.packed_blocks[0]
    full_tokens = len(_ENC.encode(long_text))
    assert info.window_tokens < full_tokens
    assert info.truncated is True
    assert info.gold_support_preserved is False


def test_budget_squeeze_beyond_the_window_is_also_reported_as_truncated(monkeypatch):
    monkeypatch.setattr(chat, "NEURON_BLOCK_TOKENS", 200)
    text = _long_text("filler", 60)  # ~121 tokens, fits within NEURON_BLOCK_TOKENS=200 unsqueezed
    hits = [
        _hit("n1", "WorkItem", "NODE-1", text),
        _hit("n2", "WorkItem", "NODE-2", text),
    ]
    # Budget big enough for one block comfortably, not both at full size.
    solo_tokens = len(_ENC.encode(f"[WorkItem] NODE-1\n{text}\n  (no recorded facts)"))
    packed = _pack(monkeypatch, hits, evidence_budget=int(solo_tokens * 1.3))

    kept = {info.uid: info for info in packed.packed_blocks}
    assert "n1" in kept
    assert kept["n1"].truncated is False
    # n2 either got squeezed smaller than its own window, or dropped outright
    # — either way it must never be reported as fully preserved.
    if "n2" in kept:
        assert kept["n2"].packed_tokens < kept["n2"].window_tokens
        assert kept["n2"].truncated is True
        assert kept["n2"].gold_support_preserved is False
    else:
        assert "n2" in packed.dropped_uids


# --- facts capping: asserted before derived ---------------------------------

def test_facts_are_capped_asserted_before_derived(monkeypatch):
    monkeypatch.setattr(chat, "NEURON_FACTS_PER_BLOCK", 3)
    facts = [
        {"source": "a", "target": "b", "relation": "REL", "derived": True,
         "fromUid": "a", "toUid": "b", "recordKeys": ["derived-1"]}
        for _ in range(5)
    ] + [
        {"source": "a", "target": "b", "relation": "ASSIGNED_TO", "derived": False,
         "fromUid": "a", "toUid": "b", "recordKeys": ["asserted-1"]}
        for _ in range(2)
    ]

    def fake_evidence(*args, **kwargs):
        return list(facts), [], set()

    hits = [_hit("n1", "WorkItem", "NODE-1", "short text")]
    packed = _pack(monkeypatch, hits, evidence_budget=5000, entity_evidence=fake_evidence)

    info = packed.packed_blocks[0]
    assert info.facts_included == 3
    assert info.facts_dropped == 4  # 7 total - 3 kept
    # Both asserted facts (2) must survive the cap before any derived one (1 of 5).
    kept_relations = [fact["relation"] for fact in packed.all_facts]
    assert kept_relations.count("ASSIGNED_TO") == 2
    assert kept_relations.count("REL") == 1


# --- expansion tier annotation (plan.md §1.4) -------------------------------

class _FakeGraph:
    """Only implements what `_expansion_tier` needs: one `.query(...).result_set`
    call returning canned `extraction_method` rows."""

    def __init__(self, extraction_methods):
        self._rows = [(method,) for method in extraction_methods]

    def query(self, *args, **kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(result_set=self._rows)


def test_expansion_sourced_block_is_annotated_with_its_tier(monkeypatch):
    monkeypatch.setattr(chat, "_entity_evidence", _no_facts)
    monkeypatch.setattr(chat, "_mentioned_record_keys", lambda *a, **k: set())
    # No _knowledge_metadata monkeypatch here — it does its own `graph.query`,
    # which _FakeGraph also answers (with the same canned rows shape it
    # returns for _expansion_tier); its result columns don't match what
    # `_knowledge_metadata` expects, so patch it directly rather than risk an
    # unpack mismatch unrelated to what this test checks.
    monkeypatch.setattr(chat, "_knowledge_metadata", _no_metadata)

    hit = SearchHit("n1", "Commit", "abc123", "short text", 0.0, ["graph:IMPLEMENTS"])
    graph = _FakeGraph(["deterministic"])  # -> tier "primary"

    packed = chat._pack_context(
        graph, [hit], "what changed", structured=None, scope=object(), providers=None,
        at=None, at_end=None, as_of=None, evidence_budget=5000,
    )

    assert len(packed.blocks) == 1
    assert "GRAPH EXPANSION TIER: primary" in packed.blocks[0]


def test_non_expansion_block_gets_no_tier_annotation(monkeypatch):
    monkeypatch.setattr(chat, "_entity_evidence", _no_facts)
    monkeypatch.setattr(chat, "_mentioned_record_keys", lambda *a, **k: set())
    monkeypatch.setattr(chat, "_knowledge_metadata", _no_metadata)

    hit = SearchHit("n1", "WorkItem", "NODE-1", "short text", 0.5, ["vector"])
    packed = chat._pack_context(
        object(), [hit], "what changed", structured=None, scope=object(), providers=None,
        at=None, at_end=None, as_of=None, evidence_budget=5000,
    )

    assert "GRAPH EXPANSION TIER" not in packed.blocks[0]
