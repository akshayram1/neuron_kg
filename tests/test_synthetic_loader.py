"""connectors/synthetic/loader.py against the real export under
nilus_data/ (skipped if that directory isn't present) plus a
from-scratch fixture set built in a temp dir, to pin down the parsing rules
that don't come from the live connector clients (missing Jira issue `id`,
Bitbucket repository inferred from a PR/commit payload, Notion page markdown
read from a sibling file)."""

from __future__ import annotations

import json

import pytest

from connectors.bitbucket.api import BitbucketCommit, BitbucketFile, BitbucketPullRequest
from connectors.jira.api import JiraIssue
from connectors.notion.api import NotionPage
from connectors.synthetic import loader
from util.paths import ROOT

REAL_FIXTURES = ROOT / "nilus_data"


@pytest.mark.skipif(not REAL_FIXTURES.is_dir(), reason="nilus_data export not present")
def test_real_fixtures_load_without_error():
    assert loader.availability() == {"jira": True, "bitbucket": True, "notion": True}

    site, project, issues = loader.load_jira()
    assert site.cloud_id and project.key
    assert issues and all(isinstance(issue, JiraIssue) for issue in issues)
    assert len({issue.issue_id for issue in issues}) == len(issues)  # synthesized ids stay unique

    repository, prs, commits_by_pr = loader.load_bitbucket()
    assert repository.full_name and prs
    assert all(isinstance(pr, BitbucketPullRequest) for pr in prs)
    assert any(commits for commits in commits_by_pr.values())
    all_commits = {commit.commit_hash: commit for commits in commits_by_pr.values() for commit in commits}
    assert any(commit.files for commit in all_commits.values())
    files = loader.load_bitbucket_files(repository)
    assert files and all(isinstance(file, BitbucketFile) for file in files)

    workspace_id, workspace_name, pages = loader.load_notion()
    assert workspace_id and workspace_name and pages
    assert all(isinstance(page, NotionPage) for page in pages)
    assert any(page.content for page in pages)  # rendered markdown was actually attached


@pytest.fixture
def empty_synthetic_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_DATA_DIR", str(tmp_path))
    return tmp_path


def test_missing_fixtures_return_empty_not_raise(empty_synthetic_dir):
    assert loader.availability() == {"jira": False, "bitbucket": False, "notion": False}
    site, project, issues = loader.load_jira()
    assert issues == [] and project.key == "LOCAL" and site.cloud_id == "nilus-data"
    repository, prs, commits = loader.load_bitbucket()
    assert prs == [] and commits == {} and repository.full_name == "local/repo"
    workspace_id, workspace_name, pages = loader.load_notion()
    assert pages == [] and workspace_id and workspace_name


def test_jira_id_is_synthesized_from_key(empty_synthetic_dir):
    (empty_synthetic_dir / "jira_real").mkdir()
    (empty_synthetic_dir / "jira_real" / "issues.json").write_text(json.dumps([
        {"key": "SYN-1", "fields": {
            "summary": "Title", "status": {"name": "Done"}, "issuetype": {"name": "Task"},
            "project": {"key": "SYN", "name": "Synthetic project"},
            "created": "2026-01-01T00:00:00.000+0000", "updated": "2026-01-02T00:00:00.000+0000",
        }},
    ]))
    site, project, issues = loader.load_jira()
    assert project.key == "SYN" and project.name == "Synthetic project"
    assert len(issues) == 1
    assert issues[0].key == "SYN-1"
    assert issues[0].issue_id == "SYN-1"  # no real numeric id in the fixture -- the key stands in


def test_bitbucket_repository_inferred_from_pr_destination(empty_synthetic_dir):
    pr_dir = empty_synthetic_dir / "bitbucket_real" / "pull_requests"
    commit_dir = empty_synthetic_dir / "bitbucket_real" / "pr_commits"
    pr_dir.mkdir(parents=True)
    commit_dir.mkdir(parents=True)
    (pr_dir / "repo.json").write_text(json.dumps({"values": [
        {
            "id": 7, "title": "Fix thing", "state": "MERGED",
            "author": {"display_name": "Dev", "uuid": "{u1}"},
            "source": {"branch": {"name": "fix/x"}},
            "destination": {
                "branch": {"name": "main"},
                "repository": {"full_name": "team/repo", "uuid": "{r1}", "name": "repo"},
            },
            "created_on": "2026-01-01T00:00:00Z", "updated_on": "2026-01-02T00:00:00Z",
        },
    ]}))
    (commit_dir / "7.json").write_text(json.dumps({"values": [
        {"hash": "abc123", "message": "Fix thing\n", "author": {"user": {"display_name": "Dev"}},
         "date": "2026-01-01T00:00:00Z"},
        {"hash": "def456", "message": "", "author": {"user": {"display_name": "Dev"}}},  # dropped: empty message
    ]}))

    repository, prs, commits_by_pr = loader.load_bitbucket()
    assert repository.full_name == "team/repo"
    assert repository.workspace == "team" and repository.slug == "repo"
    assert len(prs) == 1 and prs[0].id == 7 and prs[0].destination_branch == "main"
    assert [commit.commit_hash for commit in commits_by_pr[7]] == ["abc123"]
    assert isinstance(commits_by_pr[7][0], BitbucketCommit)


def test_notion_page_reads_sibling_markdown_and_parent(empty_synthetic_dir):
    notion_dir = empty_synthetic_dir / "notion_real"
    pages_dir = notion_dir / "pages"
    pages_dir.mkdir(parents=True)
    (notion_dir / "search_mcp.json").write_text(json.dumps({"results": [
        {
            "id": "page-1", "url": "https://notion.so/page-1",
            "last_edited_time": "2026-01-01T00:00:00.000Z",
            "parent": {"type": "page_id", "page_id": "page-0"},
            "properties": {"title": {"type": "title", "title": [{"plain_text": "My Page"}]}},
        },
    ]}))
    (pages_dir / "page-1.json").write_text(json.dumps({"id": "page-1", "markdown": "# Hello"}))

    workspace_id, workspace_name, pages = loader.load_notion()
    assert len(pages) == 1
    page = pages[0]
    assert page.page_id == "page-1" and page.title == "My Page"
    assert page.content == "# Hello"
    assert page.parent_page_id == "page-0"


def test_notion_page_without_sibling_file_gets_empty_content(empty_synthetic_dir):
    notion_dir = empty_synthetic_dir / "notion_real"
    (notion_dir / "pages").mkdir(parents=True)
    (notion_dir / "search_mcp.json").write_text(json.dumps({"results": [
        {"id": "page-2", "url": "", "last_edited_time": "", "properties": {}},
    ]}))
    _, _, pages = loader.load_notion()
    assert len(pages) == 1
    assert pages[0].content == "" and pages[0].title == "Untitled" and pages[0].parent_page_id is None
