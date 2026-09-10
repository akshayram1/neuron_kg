from datetime import UTC, datetime, timedelta

from connectors.core.jobs import ConnectorJobStore


def test_job_retry_and_completion(tmp_path):
    store = ConnectorJobStore(tmp_path / "jobs.sqlite3")
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    store._now = lambda: clock  # type: ignore[method-assign]
    store.enqueue("run-1", "jira", {"request": {"project_key": "EAPD"}})

    first = store.claim("worker")
    assert first is not None and first.status == "running" and first.attempts == 1
    assert store.fail(first, "worker", "temporary") == "retry"

    clock += timedelta(seconds=2)
    second = store.claim("worker")
    assert second is not None and second.attempts == 2
    store.complete(second.job_id, "worker")
    assert store.get("run-1").status == "completed"  # type: ignore[union-attr]


def test_expired_lease_is_recovered(tmp_path):
    store = ConnectorJobStore(tmp_path / "jobs.sqlite3")
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    store._now = lambda: clock  # type: ignore[method-assign]
    store.enqueue("run-2", "notion", {"request": {"workspace_id": "ws"}})
    assert store.claim("dead-worker", lease_seconds=1) is not None

    clock += timedelta(seconds=2)
    recovered = store.claim("new-worker")
    assert recovered is not None
    assert recovered.job_id == "run-2" and recovered.attempts == 2


def test_job_moves_to_dead_letter_after_budget(tmp_path):
    store = ConnectorJobStore(tmp_path / "jobs.sqlite3")
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    store._now = lambda: clock  # type: ignore[method-assign]
    store.enqueue("run-3", "github", {}, max_attempts=1)
    job = store.claim("worker")
    assert job is not None
    assert store.fail(job, "worker", "permanent") == "dead"
    assert store.get("run-3").last_error == "permanent"  # type: ignore[union-attr]
