from graph.structured_query import (
    _ASSIGNED_TO, _CURRENT_API_USAGE, _CURRENT_REMOVAL_IMPACT,
    _LATEST_COMMIT, _SECOND, _UNASSIGNED,
    _commit_summary, _issue_keys, _repo_hint, _sha_prefixes,
)


def test_current_api_usage_intent_uses_structured_code_authority_lane():
    assert _CURRENT_API_USAGE.search(
        "Which projects currently consume Auth API v1 and Auth API v2?"
    )
    assert _CURRENT_API_USAGE.search("What API endpoint does MCP currently call?")
    assert not _CURRENT_API_USAGE.search("What is the Auth API migration plan?")


def test_current_removal_impact_intent_uses_open_findings_lane():
    assert _CURRENT_REMOVAL_IMPACT.search(
        "Which projects are currently affected by the removal of Auth API v1?"
    )
    assert not _CURRENT_REMOVAL_IMPACT.search("What was the original removal plan?")


def test_unassigned_matches_negation_questions():
    assert _UNASSIGNED.search("which tickets are not assigned to anyone")
    assert _UNASSIGNED.search("List unassigned tickets")
    assert _UNASSIGNED.search("which tickets have no assignee")


def test_assigned_to_does_not_look_like_unassigned():
    question = "List all tickets assigned to Aashish"
    assert _ASSIGNED_TO.search(question)
    assert not _UNASSIGNED.search(question)
    match = _ASSIGNED_TO.search(question)
    assert match and "Aashish" in match.group(1)


def test_not_assigned_to_anyone_is_not_assigned_to_a_person_named_anyone():
    question = "which tickets are not assigned to anyone"
    assert _UNASSIGNED.search(question)
    # Router checks unassigned first; this would otherwise capture 'anyone'.
    assert _ASSIGNED_TO.search(question)


def test_latest_commit_intents():
    assert _LATEST_COMMIT.search("what was the last commit in argus")
    assert _LATEST_COMMIT.search("second last commit in typesense")
    assert _SECOND.search("what was the second last commit")
    assert not _SECOND.search("what was the last commit in argus")


def test_repo_hint_from_in_clause():
    assert _repo_hint("last commit in argus") == "argus"
    assert _repo_hint("last commit in the argus repo") == "argus"


def test_sha_prefixes_from_natural_questions():
    assert _sha_prefixes("what did this ce9b313 commit did") == ["ce9b313"]
    assert _sha_prefixes("which files did ce9b313 modify") == ["ce9b313"]
    assert _sha_prefixes("what did be023b9 do") == ["be023b9"]
    assert _sha_prefixes("which tickets are not assigned") == []
    assert _sha_prefixes("DATAOS-3833") == []


def test_issue_keys_from_natural_questions():
    assert _issue_keys("for DATAOS-4346 ticket what has been done") == ["DATAOS-4346"]
    assert _issue_keys("tell me about dataos-3833 and DATAOS-4346") == ["DATAOS-3833", "DATAOS-4346"]
    assert _issue_keys("what was the last commit in argus") == []


def test_longer_sha_wins_over_its_own_prefix():
    assert _sha_prefixes("ce9b313 ce9b313f0b746883") == ["ce9b313f0b746883"]


def test_commit_summary_keeps_files_block():
    text = (
        "[SOURCE]\nKind: Commit\nName: ce9b313f0b74\n\n"
        "updated pipeline\n\nFiles:\n  modified  Dockerfile  +71/-9"
    )
    summary = _commit_summary("2026-08-17", "ce9b313f0b74", "rubik_/argus", text)
    assert "sha=ce9b313f0b74" in summary
    assert "Files:" in summary
    assert "Dockerfile" in summary
