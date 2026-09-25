from connectors.core.ledger import PendingChunk
from graph.semantic_pass import _call_llm, _candidate_identity_matches, evidence_in_chunk


def test_verbatim_span_matches_after_whitespace_normalize():
    chunk = "We decided to use Redis\nbecause Postgres locking caused timeouts."
    assert evidence_in_chunk("use Redis\nbecause Postgres", chunk)


def test_case_insensitive_verbatim_match():
    assert evidence_in_chunk("USE REDIS", "we decided to use Redis")


def test_paraphrase_is_rejected():
    chunk = "We decided to use Redis because Postgres locking caused timeouts."
    assert not evidence_in_chunk("they switched stores for better performance", chunk)


def test_empty_evidence_is_rejected():
    assert not evidence_in_chunk("", "we decided to use Redis")
    assert not evidence_in_chunk("   ", "we decided to use Redis")
    assert not evidence_in_chunk(None, "we decided to use Redis")


def test_related_evidence_is_comparison_context_not_a_fact_source():
    captured = {}

    class Responses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return object()

    class Client:
        responses = Responses()

    chunk = PendingChunk("jira:c:issue:AUTH-1", "chunk-1", 0, "[SOURCE]\nnew claim")
    _call_llm(Client(), "test-model", chunk, "[notion | old design]\nold fact")

    user_text = captured["input"][1]["content"]
    assert user_text.startswith(chunk.text)
    assert "RELATED EXISTING EVIDENCE" in user_text
    assert "Do not extract a fact unless its verbatim evidence occurs in the NEW SOURCE" in user_text
    assert "old fact" in user_text


def test_endpoint_candidate_must_match_version_and_method():
    v1 = ["POST /v1/auth", "/v1/auth", "POST", None, None, None, None]
    assert not _candidate_identity_matches("Endpoint", "POST /v2/auth", v1)
    assert _candidate_identity_matches("Endpoint", "POST /v1/auth", v1)
    assert not _candidate_identity_matches("Endpoint", "GET /v1/auth", v1)
