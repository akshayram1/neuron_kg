"""Phase 0.4 sync coverage: `ConnectorLedger.record_sync_coverage` and its
read paths (`latest_sync_coverage`, `sync_coverage_for_run`, `count_present`).

No real Jira/Bitbucket/GitHub/Notion API is called here -- this exercises
exactly what each connector route does at the end of a sync: build the
record keys for what was just fetched, ask the ledger how many of them are
actually present, and log one coverage row -- against a real (temp-file)
SQLite ledger, the same one every route uses.
"""

from connectors.core.ledger import ConnectorLedger, SemanticStatus


def test_record_and_read_back_latest_coverage(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage(
        "run-1", "jira", connection_id="conn-a",
        provider_reported_total=None, fetched_count=10, ledger_count=10,
        skipped_by_rule_count=0,
    )

    rows = ledger.latest_sync_coverage("jira")
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == "run-1"
    assert row.provider == "jira"
    assert row.connection_id == "conn-a"
    assert row.provider_reported_total is None
    assert row.fetched_count == 10
    assert row.ledger_count == 10
    assert row.skipped_by_rule_count == 0
    assert row.created_at


def test_provider_reported_total_is_stored_when_known(tmp_path):
    """Not every provider leaves this null -- when an API does hand back a
    total, it must round-trip as a real int, not get coerced to 0 or dropped."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage(
        "run-1", "bitbucket", connection_id="conn-a",
        provider_reported_total=350, fetched_count=342, ledger_count=342,
        skipped_by_rule_count=8,
    )
    row = ledger.latest_sync_coverage("bitbucket")[0]
    assert row.provider_reported_total == 350
    assert row.fetched_count == 342
    assert row.skipped_by_rule_count == 8


def test_latest_sync_coverage_is_most_recent_run_per_provider(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage("run-1", "jira", fetched_count=5, ledger_count=5)
    ledger.record_sync_coverage("run-2", "jira", fetched_count=7, ledger_count=7)
    ledger.record_sync_coverage("run-1", "bitbucket", fetched_count=3, ledger_count=3)

    all_latest = {row.provider: row for row in ledger.latest_sync_coverage()}
    assert set(all_latest) == {"jira", "bitbucket"}
    assert all_latest["jira"].run_id == "run-2"
    assert all_latest["jira"].fetched_count == 7
    assert all_latest["bitbucket"].run_id == "run-1"

    # Scoped to one provider, still only the newest row.
    jira_only = ledger.latest_sync_coverage("jira")
    assert len(jira_only) == 1 and jira_only[0].run_id == "run-2"


def test_sync_coverage_for_run_looks_up_by_run_id(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage("run-1", "jira", fetched_count=5, ledger_count=5)
    ledger.record_sync_coverage("run-2", "jira", fetched_count=7, ledger_count=7)

    assert ledger.sync_coverage_for_run("run-1").fetched_count == 5
    assert ledger.sync_coverage_for_run("run-2").fetched_count == 7
    assert ledger.sync_coverage_for_run("does-not-exist") is None


def test_count_present_reflects_a_silent_partial_write(tmp_path):
    """This is the exact scenario sync coverage exists to catch: the sync
    loop believes it wrote N records (kept+written == N), but a re-read of
    the ledger shows fewer actually committed -- `fetched_count` and
    `ledger_count` diverge instead of both trusting the same in-memory tally."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    fetched_keys = [f"jira:conn-a:work_item:cloud:{i}" for i in range(5)]
    # Only 3 of the 5 "fetched" records actually made it into the ledger.
    for key in fetched_keys[:3]:
        ledger.commit(key, "hash", semantic_status=SemanticStatus.NOT_APPLICABLE)

    ledger_count = ledger.count_present(fetched_keys)
    assert ledger_count == 3

    ledger.record_sync_coverage(
        "run-1", "jira", connection_id="conn-a",
        provider_reported_total=None, fetched_count=len(fetched_keys),
        ledger_count=ledger_count, skipped_by_rule_count=0,
    )
    row = ledger.latest_sync_coverage("jira")[0]
    assert row.fetched_count == 5
    assert row.ledger_count == 3


def test_count_present_empty_list_is_zero_without_querying(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    assert ledger.count_present([]) == 0


def test_count_present_chunks_past_sqlite_variable_limit(tmp_path):
    """SQLite's default bound-parameter limit is 999 -- this exercises the
    batching in `count_present` with a key list well past that, mixing
    present and absent keys across the chunk boundary."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    all_keys = [f"github:1:source_file:repo:{i}" for i in range(1200)]
    present_keys = all_keys[::2]  # every other key actually committed
    for key in present_keys:
        ledger.commit(key, "hash", semantic_status=SemanticStatus.NOT_APPLICABLE)

    assert ledger.count_present(all_keys) == len(present_keys)
    assert ledger.count_present(present_keys) == len(present_keys)
    assert ledger.count_present([k for k in all_keys if k not in present_keys]) == 0
