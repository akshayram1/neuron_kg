from connectors.core.models import SourceRecord
from graph.resolver import anchor_properties


def test_exact_anchors_are_normalized_and_persistable():
    record = SourceRecord(
        provider="notion", connection_id="ws", entity_type="page", external_id="p1",
        name="Implementation", content=(
            "EAPD-12 uses Repository: Acme/Login.git and "
            "https://github.com/Acme/Login/commit/ABCDEF1234567"
        ),
    )
    anchors = anchor_properties(record)
    assert anchors["anchor_jira_keys"] == ["EAPD-12"]
    assert anchors["anchor_repository_names"] == ["acme/login"]
    assert anchors["anchor_commit_shas"] == ["abcdef1234567"]
