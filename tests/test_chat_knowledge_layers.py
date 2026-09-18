from graph import chat
from graph.search import SearchHit


def _hit(uid: str, label: str, name: str) -> SearchHit:
    return SearchHit(uid, label, name, name, 0.5, ["vector"])


def test_wisdom_question_reserves_wisdom_and_lineage_slots(monkeypatch):
    general = [_hit("work-1", "WorkItem", "MCP-150")]
    wisdom = [_hit("wisdom-1", "Wisdom", "Verify API migrations")]
    finding = _hit("finding-1", "Finding", "Migration claim conflicts with code")
    calls: list[tuple[str, ...] | None] = []
    shared_embedding = [0.1, 0.2]

    monkeypatch.setattr(chat, "resolve_structured", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat, "embed_query", lambda *args, **kwargs: shared_embedding)
    monkeypatch.setattr(chat, "find_named_persons", lambda *args, **kwargs: [])

    def fake_search(*args, labels=None, query_embedding=None, **kwargs):
        calls.append(tuple(labels) if labels else None)
        assert query_embedding is shared_embedding
        if labels == ["Wisdom"]:
            return wisdom
        if labels == ["Finding"]:
            return [finding]
        return general

    monkeypatch.setattr(chat, "hybrid_search", fake_search)
    monkeypatch.setattr(chat, "_actionable_wisdom_hits", lambda graph, hits: hits)
    monkeypatch.setattr(
        chat, "_linked_finding_hits",
        lambda graph, hits, scope, providers: [finding],
    )

    structured, hits = chat.retrieve(
        object(), object(),
        "What organizational lesson and wisdom follows from this finding?",
        limit=6, providers=None, scope=object(),
    )

    assert structured is None
    assert calls == [None, ("Wisdom",), ("Finding",)]
    assert [(hit.label, hit.uid) for hit in hits] == [
        ("Wisdom", "wisdom-1"),
        ("Finding", "finding-1"),
        ("WorkItem", "work-1"),
    ]


def test_wisdom_intent_also_requests_findings():
    assert chat._requested_knowledge_layers("What lesson should we follow?") == (True, True)
    assert chat._requested_knowledge_layers("Show the blast radius") == (False, True)
    assert chat._requested_knowledge_layers("Who owns MCP?") == (False, False)
