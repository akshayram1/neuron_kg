from connectors.core.ledger import ConnectorLedger, SemanticStatus


def test_changed_record_jumps_ahead_of_new_fifo_backlog(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.commit("jira:c:work_item:old", "v1", semantic_status=SemanticStatus.PENDING)
    ledger.save_chunks("jira:c:work_item:old", [("old-1", 0, "old")])
    ledger.commit("jira:c:work_item:new", "v1", semantic_status=SemanticStatus.PENDING)
    ledger.save_chunks("jira:c:work_item:new", [("new-1", 0, "new")])

    ledger.commit("jira:c:work_item:old", "v2", semantic_status=SemanticStatus.PENDING)
    ledger.save_chunks("jira:c:work_item:old", [("old-2", 0, "changed")])

    assert ledger.pending_semantic_records(2)[0] == "jira:c:work_item:old"
    assert ledger.pending_chunks(2)[0].chunk_id == "old-2"
    entry = ledger.get("jira:c:work_item:old")
    assert entry is not None and entry.update_count == 1 and entry.semantic_priority >= 200
