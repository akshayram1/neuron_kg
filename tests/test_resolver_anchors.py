from connectors.core.models import SourceRecord
from graph.resolver import anchor_properties


def test_exact_anchors_are_normalized_and_persistable():
    record = SourceRecord(
        provider="notion", connection_id="ws", entity_type="page", external_id="p1",
        name="Implementation", content=(
            "EAPD-12 uses Repository: Acme/Login.git and "
            "https://github.com/Acme/Login/commit/ABCDEF1234567 "
            "see https://bitbucket.org/rubik_/argus/pull-requests/12 "
            "and PR #12"
        ),
    )
    anchors = anchor_properties(record)
    assert anchors["anchor_jira_keys"] == ["EAPD-12"]
    assert anchors["anchor_repository_names"] == ["acme/login"]
    assert anchors["anchor_commit_shas"] == ["abcdef1234567"]
    assert "rubik_/argus#12" in anchors["anchor_pull_request_refs"]
    assert "#12" in anchors["anchor_pull_request_refs"]


def test_pull_request_refs_from_github_and_bare_number():
    from graph.bridge.anchors import pull_request_refs
    refs = pull_request_refs("github.com/Acme/Login/pull/9 and PR #9")
    assert "acme/login#9" in {ref.lower() for ref in refs}
    assert "#9" in refs
