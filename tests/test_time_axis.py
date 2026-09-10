from graph.time_axis import held_at, holds_at, infer_query_clocks, parse_iso


def test_unknown_start_is_not_open_on_the_world_axis():
    march = parse_iso("2024-03-01T00:00:00Z")
    june = parse_iso("2024-06-01T00:00:00Z")
    assert holds_at(None, None, None) is True
    assert holds_at(None, None, march) is False
    assert holds_at(None, None, june, attested_from="2024-06-01T00:00:00Z") is True
    assert holds_at(None, None, march, attested_from="2024-06-01T00:00:00Z") is False


def test_ended_unknown_closes_at_the_attesting_moment():
    february = parse_iso("2026-02-01T00:00:00Z")
    assert holds_at("2025-01-01T00:00:00Z", None, february) is True
    assert holds_at(
        "2025-01-01T00:00:00Z", None, february,
        attested_from="2026-01-15T00:00:00Z", ended_unknown=True,
    ) is False


def test_record_axis_omitted_as_of_means_now():
    assert held_at("2026-01-01T00:00:00Z", None, None) is True
    assert held_at("2026-01-01T00:00:00Z", "2026-03-01T00:00:00Z", None) is False


def test_question_clocks_split_world_and_record():
    at, as_of = infer_query_clocks("who was assigned in March 2026")
    assert at is not None and at.startswith("2026-03-01")
    assert as_of is None
    at, as_of = infer_query_clocks("what did we know about Argus in January 2026")
    assert at is None
    assert as_of is not None and as_of.startswith("2026-01-01")
