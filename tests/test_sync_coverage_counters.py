"""Phase 0.6 follow-up: the two sync coverage counters flagged as partial in
QUERIES.md -- files rejected by the `.py`/`.md` extension filter, and
whether a sync's commit fetch was capped before it reached the real end of
history.

No real Bitbucket/GitHub API is called here. `BitbucketApiClient` and
`GitHubApiClient` both accept an injected `httpx.AsyncClient` (the same
convention `connectors/notion/api.py` already uses, exercised in
`tests/test_notion_child_pages.py`), so every response below comes from an
`httpx.MockTransport` handler instead of the network.
"""

from __future__ import annotations

import asyncio

import httpx

from connectors.bitbucket.api import BitbucketApiClient, BitbucketRepository
from connectors.core.ledger import ConnectorLedger
from connectors.github_app.api import GitHubApiClient, GitHubRepository

BB_BASE = "https://api.bitbucket.org/2.0"


def _bb_repo() -> BitbucketRepository:
    return BitbucketRepository(
        uuid="repo-uuid", workspace="ws", slug="repo", name="repo",
        full_name="ws/repo", main_branch="main", html_url="", private=True, description="",
    )


def _bb_client(handler) -> BitbucketApiClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BitbucketApiClient("token", client=http)


def _gh_repo() -> GitHubRepository:
    return GitHubRepository(
        repository_id=1, full_name="own/repo", name="repo", owner="own",
        default_branch="main", private=False, html_url="",
    )


def _gh_client(handler) -> GitHubApiClient:
    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubApiClient("token", client=http)


# ------------------------------------------------------ Bitbucket: files()

def test_bitbucket_files_counts_extension_filtered_distinctly():
    """A `.go` file is rejected by the extension filter, separate from
    `too_large`/`without_text`, which stay at zero here."""
    repo = _bb_repo()
    commit_hash = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/2.0/repositories/ws/repo/refs/branches/main":
            return httpx.Response(200, json={"target": {"hash": commit_hash}})
        if path == f"/2.0/repositories/ws/repo/src/{commit_hash}/":
            return httpx.Response(200, json={"values": [
                {"type": "commit_file", "path": "a.py", "size": 10,
                 "commit": {"hash": "h1"}},
                {"type": "commit_file", "path": "b.go", "size": 5,
                 "commit": {"hash": "h2"}},
                {"type": "commit_file", "path": "c.md", "size": 5,
                 "commit": {"hash": "h3"}},
            ], "next": None})
        if path == f"/2.0/repositories/ws/repo/src/{commit_hash}/a.py":
            return httpx.Response(200, content=b"print('a')")
        if path == f"/2.0/repositories/ws/repo/src/{commit_hash}/c.md":
            return httpx.Response(200, content=b"# c")
        raise AssertionError(f"unexpected request {request.method} {path}")

    async def run():
        async with _bb_client(handler) as client:
            return await client.files(repo, {".py", ".md"}, max_bytes=1_000_000)

    files, too_large, without_text, extension_filtered = asyncio.run(run())
    assert {f.path for f in files} == {"a.py", "c.md"}
    assert too_large == 0
    assert without_text == 0
    assert extension_filtered == 1


# ---------------------------------------------------- Bitbucket: commits()

def test_bitbucket_commits_under_cap_is_not_capped():
    repo = _bb_repo()
    commit_hash = "a" * 40
    calls = {"commits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/2.0/repositories/ws/repo/refs/branches/main":
            return httpx.Response(200, json={"target": {"hash": commit_hash}})
        if path == f"/2.0/repositories/ws/repo/commits/{commit_hash}":
            calls["commits"] += 1
            return httpx.Response(200, json={"values": [
                {"hash": f"c{i}", "message": f"msg {i}", "date": "2026-01-01T00:00:00+00:00",
                 "author": {"raw": "A <a@example.com>"}, "links": {"html": {"href": ""}}}
                for i in range(5)
            ], "next": None})
        raise AssertionError(f"unexpected request {request.method} {path}")

    async def run():
        async with _bb_client(handler) as client:
            return await client.commits(repo, limit=100)

    commits, capped = asyncio.run(run())
    assert len(commits) == 5
    assert capped is False
    assert calls["commits"] == 1


def test_bitbucket_commits_over_cap_is_capped_with_a_single_request():
    """The provider says there's a `next` page beyond the 2 commits the cap
    asked for. That page is never actually fetched -- `commits_capped` comes
    from the response already in hand, not an extra request."""
    repo = _bb_repo()
    commit_hash = "a" * 40
    calls = {"commits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/2.0/repositories/ws/repo/refs/branches/main":
            return httpx.Response(200, json={"target": {"hash": commit_hash}})
        if path == f"/2.0/repositories/ws/repo/commits/{commit_hash}":
            calls["commits"] += 1
            if calls["commits"] > 1:
                raise AssertionError("must not page beyond the cap just to count")
            return httpx.Response(200, json={"values": [
                {"hash": "c0", "message": "msg 0", "date": "2026-01-01T00:00:00+00:00",
                 "author": {"raw": "A <a@example.com>"}, "links": {"html": {"href": ""}}},
                {"hash": "c1", "message": "msg 1", "date": "2026-01-01T00:00:00+00:00",
                 "author": {"raw": "A <a@example.com>"}, "links": {"html": {"href": ""}}},
            ], "next": f"{BB_BASE}/repositories/ws/repo/commits/{commit_hash}?page=2"})
        raise AssertionError(f"unexpected request {request.method} {path}")

    async def run():
        async with _bb_client(handler) as client:
            return await client.commits(repo, limit=2)

    commits, capped = asyncio.run(run())
    assert len(commits) == 2
    assert capped is True
    assert calls["commits"] == 1


# -------------------------------------------------------- GitHub: list_files()

def test_github_list_files_counts_extension_filtered_distinctly():
    repo = _gh_repo()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/own/repo/git/trees/main":
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"type": "blob", "path": "a.py", "size": 10, "sha": "s1"},
                {"type": "blob", "path": "b.go", "size": 5, "sha": "s2"},
                {"type": "tree", "path": "dir", "sha": "s3"},
                {"type": "blob", "path": "c.md", "size": 5, "sha": "s4"},
            ]})
        raise AssertionError(f"unexpected request {request.method} {path}")

    async def run():
        async with _gh_client(handler) as client:
            return await client.list_files(repo, {".py", ".md"}, max_bytes=1_000_000)

    files, too_large, extension_filtered = asyncio.run(run())
    assert {f.path for f in files} == {"a.py", "c.md"}
    assert too_large == 0
    # Only the blob with a rejected extension (b.go) counts -- the "dir"
    # tree entry is never a file at all and must not be double-counted.
    assert extension_filtered == 1


# ------------------------------------------------------ GitHub: list_commits()

def test_github_list_commits_under_cap_is_not_capped():
    repo = _gh_repo()
    calls = {"commits": 0}

    def _commit(sha: str) -> dict:
        return {"sha": sha, "html_url": "", "commit": {
            "message": f"msg {sha}", "author": {"name": "A", "email": "a@example.com", "date": "2026-01-01"},
        }}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/own/repo/commits":
            calls["commits"] += 1
            per_page = int(request.url.params.get("per_page"))
            assert per_page == 100  # min(100, limit) on the first page
            return httpx.Response(200, json=[_commit(f"c{i}") for i in range(5)])
        raise AssertionError(f"unexpected request {request.method} {path}")

    async def run():
        async with _gh_client(handler) as client:
            return await client.list_commits(repo, limit=100)

    commits, capped = asyncio.run(run())
    assert len(commits) == 5
    assert capped is False
    # A short batch (5 < the 100 requested) means the real end of history
    # was reached -- no probe request should follow.
    assert calls["commits"] == 1


def test_github_list_commits_over_cap_is_capped_via_one_probe_request():
    """Once the 2-commit cap is filled, exactly one extra `per_page=1`
    request (page 2) checks whether more exist -- not a full extra page."""
    repo = _gh_repo()
    calls = {"page1": 0, "probe": 0}

    def _commit(sha: str) -> dict:
        return {"sha": sha, "html_url": "", "commit": {
            "message": f"msg {sha}", "author": {"name": "A", "email": "a@example.com", "date": "2026-01-01"},
        }}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/repos/own/repo/commits":
            raise AssertionError(f"unexpected request {request.method} {path}")
        page = request.url.params.get("page")
        per_page = int(request.url.params.get("per_page"))
        if page == "1":
            assert per_page == 2  # min(100, limit)
            calls["page1"] += 1
            return httpx.Response(200, json=[_commit("c0"), _commit("c1")])
        if page == "2":
            assert per_page == 1  # the cheap probe, not a real page
            calls["probe"] += 1
            return httpx.Response(200, json=[_commit("c2")])
        raise AssertionError(f"unexpected page {page}")

    async def run():
        async with _gh_client(handler) as client:
            return await client.list_commits(repo, limit=2)

    commits, capped = asyncio.run(run())
    assert len(commits) == 2
    assert capped is True
    assert calls == {"page1": 1, "probe": 1}


def test_github_list_commits_exact_total_is_not_capped():
    """Repo has exactly `limit` commits: the probe comes back empty, so
    `commits_capped` must be False even though the fetch loop filled the cap
    exactly on the last page."""
    repo = _gh_repo()

    def _commit(sha: str) -> dict:
        return {"sha": sha, "html_url": "", "commit": {
            "message": f"msg {sha}", "author": {"name": "A", "email": "a@example.com", "date": "2026-01-01"},
        }}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/repos/own/repo/commits":
            raise AssertionError(f"unexpected request {request.method} {path}")
        page = request.url.params.get("page")
        if page == "1":
            return httpx.Response(200, json=[_commit("c0"), _commit("c1")])
        if page == "2":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected page {page}")

    async def run():
        async with _gh_client(handler) as client:
            return await client.list_commits(repo, limit=2)

    commits, capped = asyncio.run(run())
    assert len(commits) == 2
    assert capped is False


# --------------------------------------------------------- ledger plumbing

def test_sync_coverage_round_trips_new_counters(tmp_path):
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage(
        "run-1", "bitbucket", connection_id="conn-a",
        provider_reported_total=None, fetched_count=10, ledger_count=9,
        skipped_by_rule_count=1, extension_filtered_count=4, commits_capped=True,
    )
    row = ledger.latest_sync_coverage("bitbucket")[0]
    assert row.extension_filtered_count == 4
    assert row.commits_capped is True
    # Unchanged meaning: skipped_by_rule_count is NOT the extension count.
    assert row.skipped_by_rule_count == 1


def test_sync_coverage_new_counters_default_when_unspecified(tmp_path):
    """Existing callers (jira_routes.py, notion_routes.py) don't pass the
    new keywords at all -- they must not be required."""
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    ledger.record_sync_coverage("run-1", "jira", fetched_count=5, ledger_count=5)
    row = ledger.latest_sync_coverage("jira")[0]
    assert row.extension_filtered_count == 0
    assert row.commits_capped is False


def test_sync_coverage_migrates_a_pre_phase_0_6_table(tmp_path):
    """A ledger file written before `extension_filtered_count`/
    `commits_capped` existed must still open and accept writes/reads using
    the same migration-safe `ALTER TABLE ... ADD COLUMN` pattern the rest of
    this ledger already uses for `source_records`/`source_chunks`/
    `relation_axioms`."""
    import sqlite3
    from datetime import UTC, datetime

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE sync_coverage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            connection_id TEXT,
            provider_reported_total INTEGER,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            ledger_count INTEGER NOT NULL DEFAULT 0,
            skipped_by_rule_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO sync_coverage(run_id, provider, fetched_count, ledger_count, "
        "skipped_by_rule_count, created_at) VALUES ('run-0', 'jira', 3, 3, 0, ?)",
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()

    ledger = ConnectorLedger(path)
    old_row = ledger.sync_coverage_for_run("run-0")
    assert old_row is not None
    assert old_row.extension_filtered_count == 0
    assert old_row.commits_capped is False

    ledger.record_sync_coverage(
        "run-1", "jira", fetched_count=4, ledger_count=4,
        extension_filtered_count=2, commits_capped=True,
    )
    new_row = ledger.sync_coverage_for_run("run-1")
    assert new_row.extension_filtered_count == 2
    assert new_row.commits_capped is True
