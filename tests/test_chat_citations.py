from __future__ import annotations

from graph.chat import _assignment_facts, _used_record_keys


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


def test_model_title_echo_matches_short_issue_key_block_name():
    record_keys_by_source = {"DATAOS-3833": {"jira:conn:work_item:3833"}}
    used = _used_record_keys(
        ["DATAOS-3833 — Converse across Data Products"],
        record_keys_by_source,
    )
    assert used == {"jira:conn:work_item:3833"}


def test_short_prefix_does_not_steal_a_longer_issue_key():
    record_keys_by_source = {"DATAOS-3833": {"jira:conn:work_item:3833"}}
    used = _used_record_keys(["DATAOS-38"], record_keys_by_source)
    assert used == set()


def test_assignment_facts_drop_parent_of_so_children_are_not_cited_or_highlighted():
    facts = [
        {"relation": "PARENT_OF", "fromUid": "epic", "toUid": "child-assigned",
         "recordKeys": ["jira:conn:work_item:3839"]},
        {"relation": "ASSIGNED_TO", "fromUid": "ticket", "toUid": "person",
         "recordKeys": ["jira:conn:work_item:4182"]},
        {"relation": "REPORTED_BY", "fromUid": "ticket", "toUid": "reporter",
         "recordKeys": ["jira:conn:work_item:4182"]},
    ]
    slim, edges = _assignment_facts(facts)
    assert [fact["relation"] for fact in slim] == ["ASSIGNED_TO"]
    assert edges == ["ticket:ASSIGNED_TO:person"]
