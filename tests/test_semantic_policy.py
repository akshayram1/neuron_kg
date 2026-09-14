from graph.semantic_pass import evidence_in_chunk


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
