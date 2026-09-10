from __future__ import annotations

from graph.chat import _used_record_keys


def test_trailing_whitespace_in_source_name_does_not_drop_a_citation():
    """Real case: a Jira summary stored with a trailing space made the
    model's (correctly trimmed) echo of that name fail an exact match,
    silently dropping a citation the model explicitly said it used."""
    record_keys_by_source = {
        "DATAOS-4192 — Create SRE deployment runbooks for Argus ": {"jira:conn:work_item:4192"},
    }
    used = _used_record_keys(
        ["DATAOS-4192 — Create SRE deployment runbooks for Argus"], record_keys_by_source,
    )
    assert used == {"jira:conn:work_item:4192"}


def test_leading_whitespace_on_the_model_side_is_also_tolerated():
    record_keys_by_source = {"DATAOS-1": {"jira:conn:work_item:1"}}
    used = _used_record_keys([" DATAOS-1 "], record_keys_by_source)
    assert used == {"jira:conn:work_item:1"}


def test_unused_source_contributes_no_citation():
    record_keys_by_source = {
        "DATAOS-1": {"jira:conn:work_item:1"},
        "DATAOS-2": {"jira:conn:work_item:2"},
    }
    used = _used_record_keys(["DATAOS-1"], record_keys_by_source)
    assert used == {"jira:conn:work_item:1"}


def test_unknown_source_name_is_ignored_not_an_error():
    record_keys_by_source = {"DATAOS-1": {"jira:conn:work_item:1"}}
    used = _used_record_keys(["DATAOS-does-not-exist"], record_keys_by_source)
    assert used == set()


def test_multiple_used_sources_union_their_record_keys():
    record_keys_by_source = {
        "DATAOS-1": {"jira:conn:work_item:1"},
        "DATAOS-2": {"jira:conn:work_item:2", "notion:ws:page:2"},
    }
    used = _used_record_keys(["DATAOS-1", "DATAOS-2"], record_keys_by_source)
    assert used == {"jira:conn:work_item:1", "jira:conn:work_item:2", "notion:ws:page:2"}
