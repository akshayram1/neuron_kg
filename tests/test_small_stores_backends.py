"""Job queue + OAuth/GitHub/Notion stores on both SQLite and Postgres."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from connectors.core.jobs import ConnectorJobStore
from connectors.core.oauth_store import OAuthConnectorStore
from connectors.github_app.store import GitHubStore
from connectors.notion.oauth import NotionStore
from storage.sql_backend import connect, is_postgres, table_columns

BOTH = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


def _clocked_jobs(path):
    store = ConnectorJobStore(path)
    state = {"now": datetime(2026, 1, 1, tzinfo=UTC)}
    store._now = lambda: state["now"]  # type: ignore[method-assign]
    return store, state


def _run_threads(count, target):
    errors: list[BaseException] = []

    def wrapped(index):
        try:
            target(index)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]


# ------------------------------------------------------------------ jobs


@BOTH
def test_jobs_enqueue_claim_fail_retry_complete(sql_backend, tmp_path):
    store, clock = _clocked_jobs(tmp_path / "jobs.sqlite3")
    store.enqueue("run-1", "jira", {"request": {"project_key": "EAPD"}})
    queued = store.get("run-1")
    assert queued is not None and queued.status == "queued" and queued.attempts == 0
    assert queued.payload == {"request": {"project_key": "EAPD"}}

    first = store.claim("worker")
    assert first is not None and first.status == "running" and first.attempts == 1
    assert first.lease_owner == "worker" and first.max_attempts == 3
    assert store.claim("other") is None  # nothing else claimable
    assert store.heartbeat("run-1", "worker") is True
    assert store.heartbeat("run-1", "intruder") is False

    assert store.fail(first, "worker", "temporary") == "retry"
    assert store.claim("worker") is None  # backoff not elapsed

    clock["now"] += timedelta(seconds=2)
    second = store.claim("worker")
    assert second is not None and second.attempts == 2
    store.complete(second.job_id, "someone-else")  # wrong owner: ignored
    assert store.get("run-1").status == "running"  # type: ignore[union-attr]
    store.complete(second.job_id, "worker")
    done = store.get("run-1")
    assert done is not None and done.status == "completed" and done.lease_owner is None
    assert done.last_error == "temporary"
    assert store.get("missing") is None


@BOTH
def test_jobs_dead_letter_and_duplicate_enqueue(sql_backend, tmp_path):
    from storage.sql_backend import IntegrityError

    store, _clock = _clocked_jobs(tmp_path / "jobs.sqlite3")
    store.enqueue("run-3", "github", {}, max_attempts=1)
    with pytest.raises(IntegrityError):
        store.enqueue("run-3", "github", {})
    job = store.claim("worker")
    assert job is not None
    assert store.fail(job, "worker", "permanent") == "dead"
    dead = store.get("run-3")
    assert dead is not None and dead.status == "dead" and dead.last_error == "permanent"
    assert store.claim("worker") is None


@BOTH
def test_jobs_expired_lease_is_recovered(sql_backend, tmp_path):
    store, clock = _clocked_jobs(tmp_path / "jobs.sqlite3")
    store.enqueue("run-2", "notion", {"request": {"workspace_id": "ws"}})
    stale = store.claim("dead-worker", lease_seconds=1)
    assert stale is not None

    clock["now"] += timedelta(seconds=2)
    recovered = store.claim("new-worker")
    assert recovered is not None
    assert recovered.job_id == "run-2" and recovered.attempts == 2
    assert recovered.lease_owner == "new-worker"
    assert recovered.last_error == "Worker lease expired; recovered"
    # The dead worker lost its lease: its heartbeat/complete/fail are no-ops.
    assert store.heartbeat("run-2", "dead-worker") is False
    store.complete("run-2", "dead-worker")
    assert store.get("run-2").status == "running"  # type: ignore[union-attr]


@BOTH
def test_jobs_expired_lease_on_final_attempt_is_dead_lettered(sql_backend, tmp_path):
    store, clock = _clocked_jobs(tmp_path / "jobs.sqlite3")
    store.enqueue("run-4", "jira", {}, max_attempts=1)
    assert store.claim("dead-worker", lease_seconds=1) is not None
    clock["now"] += timedelta(seconds=2)
    assert store.claim("new-worker") is None
    job = store.get("run-4")
    assert job is not None and job.status == "dead" and job.lease_owner is None


@BOTH
def test_jobs_store_reopen_is_idempotent(sql_backend, tmp_path):
    path = tmp_path / "jobs.sqlite3"
    ConnectorJobStore(path).enqueue("run-5", "jira", {"a": 1})
    reopened = ConnectorJobStore(path)
    assert reopened.get("run-5").payload == {"a": 1}  # type: ignore[union-attr]


@BOTH
def test_jobs_concurrent_claims_lease_each_job_once(sql_backend, tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = ConnectorJobStore(path)
    job_count, worker_count = 30, 8
    for index in range(job_count):
        store.enqueue(f"job-{index:02d}", "jira", {"n": index})

    claimed: list[tuple[str, str]] = []
    lock = threading.Lock()
    start = threading.Barrier(worker_count)

    def worker(index):
        own = ConnectorJobStore(path)
        name = f"w{index}"
        start.wait()
        while (job := own.claim(name)) is not None:
            with lock:
                claimed.append((job.job_id, name))
            own.complete(job.job_id, name)

    _run_threads(worker_count, worker)
    ids = [job_id for job_id, _ in claimed]
    assert sorted(ids) == [f"job-{index:02d}" for index in range(job_count)]
    assert len(set(ids)) == job_count
    for index in range(job_count):
        job = store.get(f"job-{index:02d}")
        assert job is not None and job.status == "completed" and job.attempts == 1


@BOTH
def test_jobs_concurrent_lease_recovery_requeues_once(sql_backend, tmp_path):
    path = tmp_path / "jobs.sqlite3"
    seed, clock = _clocked_jobs(path)
    for index in range(10):
        seed.enqueue(f"job-{index}", "jira", {})
    while seed.claim("crashed", lease_seconds=1) is not None:
        pass
    clock["now"] += timedelta(seconds=5)

    claimed: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(5)

    def worker(index):
        own = ConnectorJobStore(path)
        own._now = lambda: clock["now"]  # type: ignore[method-assign]
        start.wait()
        while (job := own.claim(f"w{index}")) is not None:
            assert job.attempts == 2
            with lock:
                claimed.append(job.job_id)
            own.complete(job.job_id, f"w{index}")

    _run_threads(5, worker)
    assert sorted(claimed) == sorted(f"job-{index}" for index in range(10))


# ----------------------------------------------------------- oauth store


def _oauth(path, provider="jira", key=None):
    key = key or Fernet.generate_key().decode()
    return OAuthConnectorStore(path, provider, key), key


@BOTH
def test_oauth_store_round_trip(sql_backend, tmp_path):
    path = tmp_path / "oauth_connectors.sqlite3"
    store, key = _oauth(path)
    other_provider, _ = _oauth(path, "bitbucket", key)

    state = store.create_state("browser-1")
    assert store.consume_state(state) == store.session_hash("browser-1")
    assert store.consume_state(state) is None  # single use
    expired = store.create_state("browser-1", ttl_seconds=-5)
    assert store.consume_state(expired) is None
    assert other_provider.consume_state(store.create_state("browser-1")) is None

    token = {"access_token": "at-1", "refresh_token": "rt-1", "scope": ["read"]}
    saved = store.save_connection(
        connection_id="cloud-1", account_id="acc", account_name="Acme", token=token
    )
    assert saved["token"] == token and saved["provider"] == "jira"
    created_at = saved["created_at"]
    updated = store.update_token("cloud-1", {"access_token": "at-2"})
    assert updated["token"] == {**token, "access_token": "at-2"}
    assert updated["created_at"] == created_at

    # Ciphertext is stored as raw bytes and decrypts with the same key.
    with connect(path) as db:
        raw = db.execute(
            "SELECT encrypted_token FROM oauth_connections WHERE provider=? AND connection_id=?",
            ("jira", "cloud-1"),
        ).fetchone()[0]
    assert isinstance(raw, bytes)
    assert Fernet(key.encode()).decrypt(raw) == b'{"access_token":"at-2","refresh_token":"rt-1","scope":["read"]}'

    session = store.session_hash("browser-1")
    assert not store.session_has_connection(session, "cloud-1")
    store.link_session(session, "cloud-1")
    store.link_session(session, "cloud-1")  # upsert
    assert store.session_has_connection(session, "cloud-1")
    assert [row["connection_id"] for row in store.list_connections(session)] == ["cloud-1"]
    assert other_provider.list_connections(session) == []

    store.save_source("cloud-1", "EAPD", "Eapd", "jira_eapd", {"jql": "a"})
    store.save_source("cloud-1", "EAPD", "Eapd v2", "jira_eapd", {"jql": "b"})
    sources = store.list_sources(session)
    assert len(sources) == 1 and sources[0]["source_name"] == "Eapd v2"
    assert sources[0]["config"] == {"jql": "b"}

    store.create_run("run-1", "cloud-1", "EAPD", graph_name="default")
    with pytest.raises(RuntimeError, match="already active"):
        store.create_run("run-2", "cloud-1", "EAPD", graph_name="default")
    store.create_run("run-3", "cloud-1", "EAPD", graph_name="second")  # other graph ok
    assert store.fail_orphaned_runs("cloud-1", "EAPD", {"run-1"}) == 1
    assert store.get_run("run-3")["status"] == "failed"  # type: ignore[index]
    store.set_run("run-1", "completed", {"episodes": 3})
    run = store.get_run("run-1")
    assert run is not None and run["result"] == {"episodes": 3} and run["finished_at"]
    assert store.list_sources(session)[0]["last_sync_at"] == run["finished_at"]
    store.create_run("run-4", "cloud-1", "EAPD")  # slot free again
    assert {r["run_id"] for r in store.list_runs(session)} == {"run-1", "run-3", "run-4"}
    assert {r["run_id"] for r in store.list_runs(session, graph_name="second")} == {"run-3"}
    assert store.get_run("nope") is None

    assert store.delete_connection("cloud-1") == ["jira_eapd"]
    with pytest.raises(KeyError):
        store.get_connection("cloud-1")
    assert store.list_runs(session) == [] and store.list_sources(session) == []


@BOTH
def test_oauth_store_reopen_and_legacy_migration(sql_backend, tmp_path):
    path = tmp_path / "oauth_connectors.sqlite3"
    with connect(path) as db:  # pre-multi-graph layout
        db.execute(
            """CREATE TABLE oauth_sync_runs (
                   provider TEXT NOT NULL, run_id TEXT NOT NULL, connection_id TEXT NOT NULL,
                   source_id TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                   finished_at TEXT, result_json TEXT, error TEXT,
                   PRIMARY KEY(provider, run_id))"""
        )
        db.execute(
            """CREATE UNIQUE INDEX oauth_one_active_sync_per_source
               ON oauth_sync_runs(provider, connection_id, source_id)
               WHERE status IN ('queued', 'running')"""
        )
        db.execute(
            """INSERT INTO oauth_sync_runs(provider, run_id, connection_id, source_id, status, started_at)
               VALUES ('jira', 'old', 'c', 's', 'running', '2025-01-01')"""
        )
    store, key = _oauth(path)
    with connect(path) as db:
        assert "graph_name" in table_columns(db, "oauth_sync_runs")
    assert store.get_run("old")["graph_name"] == "default"  # type: ignore[index]
    store.create_run("new", "c", "s", graph_name="second")  # widened index
    with pytest.raises(RuntimeError):
        store.create_run("dup", "c", "s", graph_name="default")

    for _ in range(2):  # re-open is idempotent and keeps data + constraint
        store, _ = _oauth(path, key=key)
    assert store.get_run("new")["graph_name"] == "second"  # type: ignore[index]
    with pytest.raises(RuntimeError):
        store.create_run("dup2", "c", "s", graph_name="second")


# ---------------------------------------------------------- github store


@BOTH
def test_github_store_round_trip(sql_backend, tmp_path):
    path = tmp_path / "github_connector.sqlite3"
    store = GitHubStore(path)
    state = store.create_oauth_state("browser")
    assert store.consume_oauth_state(state) == store.session_hash("browser")
    assert store.consume_oauth_state(state) is None

    installation = {"id": 42, "account": {"id": 7, "login": "acme", "type": "Organization"}}
    assert store.save_installation(installation) == 42
    first = store.list_installations(store.session_hash("browser"))
    assert first == []
    session = store.session_hash("browser")
    store.link_session_installation(session, 42)
    store.link_session_installation(session, 42)
    assert store.session_has_installation(session, 42)
    rows = store.list_installations(session)
    assert [(r["installation_id"], r["account_login"]) for r in rows] == [(42, "acme")]
    store.save_installation({**installation, "account": {"id": 7, "login": "acme-renamed"}})
    assert store.list_installations(session)[0]["account_login"] == "acme-renamed"

    store.save_source(42, 1001, "acme/api", "main", [".py"], True)
    store.save_source(42, 1001, "acme/api", "trunk", [".py", ".md"], False)
    sources = store.list_sources(session)
    assert len(sources) == 1
    assert sources[0]["default_branch"] == "trunk"
    assert sources[0]["file_types"] == [".py", ".md"]
    assert sources[0]["include_commit_messages"] is False
    assert sources[0]["group_id"] == "github_42_1001"

    store._now = lambda: "2026-01-01T00:00:00+00:00"  # type: ignore[method-assign]
    store.record_episode(42, 1001, "file", "a.py", "uid-1", "graph-1")
    store.record_episode(42, 1001, "file", "a.py", "uid-1", "graph-dup")  # ignored
    store.record_episode(42, 1001, "file", "a.py", "uid-2", "graph-2")  # same timestamp
    assert store.episode_exists(42, 1001, "uid-1")
    assert not store.episode_exists(42, 1001, "uid-3")
    assert store.latest_source_episode(42, 1001, "file", "a.py") == "graph-2"
    assert store.latest_source_episode(42, 1001, "file", "b.py") is None
    del store._now

    store.create_sync_run("r1", 42, 1001)
    with pytest.raises(RuntimeError, match="already queued"):
        store.create_sync_run("r2", 42, 1001)
    store.create_sync_run("r3", 42, 1001, graph_name="second")
    assert store.fail_orphaned_sync_runs(42, 1001, {"r1"}) == 1
    store.set_sync_run("r1", "completed", {"files": 2})
    assert store.get_sync_run("r1")["result"] == {"files": 2}  # type: ignore[index]
    store.finish_source_sync(42, 1001, None)
    assert store.list_sources(session)[0]["last_sync_at"]
    assert {r["run_id"] for r in store.list_sync_runs(session)} == {"r1", "r3"}
    assert [r["run_id"] for r in store.list_sync_runs(session, graph_name="second")] == ["r3"]

    assert store.delete_installation(42) == ["github_42_1001"]
    assert store.list_installations(session) == []
    assert not store.episode_exists(42, 1001, "uid-1")

    reopened = GitHubStore(path)
    GitHubStore(path)
    assert reopened.get_sync_run("r1") is None


@BOTH
def test_github_session_link_requires_installation(sql_backend, tmp_path):
    from storage.sql_backend import IntegrityError

    store = GitHubStore(tmp_path / "github_connector.sqlite3")
    with pytest.raises(IntegrityError):  # FOREIGN KEY enforced on both engines
        store.link_session_installation("s", 999)


# ---------------------------------------------------------- notion store


@BOTH
def test_notion_store_round_trip(sql_backend, tmp_path):
    path = tmp_path / "notion_connector.sqlite3"
    key = Fernet.generate_key().decode()
    store = NotionStore(path, key)

    state = store.create_oauth_state("browser")
    assert store.consume_oauth_state(state) == store.session_hash("browser")
    assert store.consume_oauth_state(state) is None

    payload = {"access_token": "secret", "workspace_id": "ws-1", "bot_id": "bot", "workspace_name": "Acme"}
    connection = store.save_connection(payload)
    assert connection.token == payload and connection.workspace_name == "Acme"
    rotated = store.update_tokens("ws-1", {"access_token": "secret-2", "refresh_token": "r"})
    assert rotated.access_token == "secret-2" and rotated.refresh_token == "r"
    assert rotated.created_at == connection.created_at
    with connect(path) as db:
        raw = db.execute("SELECT encrypted_token FROM notion_connections").fetchone()[0]
    assert isinstance(raw, bytes)
    assert Fernet(key.encode()).decrypt(raw) == (
        b'{"access_token":"secret-2","bot_id":"bot","refresh_token":"r",'
        b'"workspace_id":"ws-1","workspace_name":"Acme"}'
    )

    session = store.session_hash("browser")
    assert store.list_connections(session) == []
    store.link_session_connection(session, "ws-1")
    assert store.session_has_connection(session, "ws-1")
    assert [c["group_id"] for c in store.list_connections(session)] == ["notion_ws-1"]
    assert len(store.list_connections()) == 1

    store.record_episode("ws-1", "notion_ws-1", "uid", "page", "g1")
    store.record_episode("ws-1", "notion_ws-1", "uid", "page", "g2")  # ignored
    assert store.episode_exists("ws-1", "notion_ws-1", "uid")

    pages = [
        {"page_id": "p1", "title": "One", "url": "u1", "last_edited_time": "t"},
        {"page_id": "p2", "title": "Two", "url": "u2", "last_edited_time": "t"},
    ]
    assert store.reconcile_pages("ws-1", pages) == 0
    assert store.reconcile_pages("ws-1", pages[:1]) == 1
    assert store.reconcile_pages("ws-1", pages) == 0  # reactivated

    store.create_sync_run("r1", "ws-1")
    with pytest.raises(RuntimeError, match="already queued"):
        store.create_sync_run("r2", "ws-1")
    store.create_sync_run("r3", "ws-1", graph_name="second")
    assert store.fail_orphaned_sync_runs("ws-1", {"r1"}) == 1
    store.set_sync_run("r1", "completed", {"pages": 2})
    assert store.get_sync_run("r1")["result"] == {"pages": 2}  # type: ignore[index]
    store.finish_connection_sync("ws-1", "boom")
    assert store.list_connections()[0]["last_sync_error"] == "boom"
    store.save_connection(payload)  # reconnect clears last_sync_error
    assert store.list_connections()[0]["last_sync_error"] is None
    assert {r["run_id"] for r in store.list_sync_runs(session)} == {"r1", "r3"}

    assert store.delete_connection("ws-1") == ["notion_ws-1"]
    with pytest.raises(KeyError):
        store.get_connection("ws-1")
    assert not store.episode_exists("ws-1", "notion_ws-1", "uid")

    NotionStore(path, key)
    NotionStore(path, key)


# ------------------------------------------------------ postgres concurrency


@pytest.mark.parametrize("sql_backend", ["postgres"], indirect=True)
def test_postgres_state_is_consumed_exactly_once(sql_backend, tmp_path):
    store, _ = _oauth(tmp_path / "oauth_connectors.sqlite3")
    state = store.create_state("browser")
    results: list[str | None] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def consume(_index):
        start.wait()
        value = store.consume_state(state)
        with lock:
            results.append(value)

    _run_threads(8, consume)
    assert [value for value in results if value] == [store.session_hash("browser")]


@pytest.mark.parametrize("sql_backend", ["postgres"], indirect=True)
def test_postgres_concurrent_store_initialisation(sql_backend, tmp_path):
    key = Fernet.generate_key().decode()
    # storage.sql_backend's own CREATE SCHEMA IF NOT EXISTS races on the very
    # first concurrent connect (shim issue, reported separately); open each
    # schema once so this test exercises the stores' table/index DDL race.
    for name in ("oauth_connectors", "github_connector", "notion_connector"):
        with connect(tmp_path / f"{name}.sqlite3") as db:
            db.execute("SELECT 1")
    start = threading.Barrier(6)

    def build(index):
        start.wait()
        if index % 3 == 0:
            OAuthConnectorStore(tmp_path / "oauth_connectors.sqlite3", "jira", key)
        elif index % 3 == 1:
            GitHubStore(tmp_path / "github_connector.sqlite3")
        else:
            NotionStore(tmp_path / "notion_connector.sqlite3", key)

    _run_threads(6, build)
    with connect(tmp_path / "github_connector.sqlite3") as db:
        assert is_postgres(db)
        assert "rowid" in table_columns(db, "github_episodes")
