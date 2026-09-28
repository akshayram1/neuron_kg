"""Local JSON fixtures standing in for live Jira/Bitbucket/Notion API calls.

`synthetic/mcp-access/` holds real API response shapes captured once (Jira
issue search results, Bitbucket pull-request/commit list pages, Notion search
results plus their rendered markdown) with no live credentials or network
access needed to replay them. Each `load_*` function here returns exactly the
dataclasses `connectors.<provider>.api` would have returned, so the result
goes straight into the same `graph.<provider>_pipeline.write_*` writers a real
OAuth sync uses — this is a different *source* for the same pipeline, not a
parallel ingestion path (unlike the removed `connectors/story/` demo, which
wrote its own bespoke fixture format through its own one-off ingestor).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from connectors.bitbucket.api import (
    BitbucketCommit,
    BitbucketPullRequest,
    BitbucketRepository,
    _split_author,
)
from connectors.jira.api import JiraApiClient, JiraIssue, JiraProject, JiraSite
from connectors.notion.api import NotionPage
from util.paths import ROOT

SYNTHETIC_JIRA_CONNECTION = "synthetic-jira"
SYNTHETIC_BITBUCKET_CONNECTION = "synthetic-bitbucket"
SYNTHETIC_NOTION_CONNECTION = "synthetic-notion"


def _data_dir() -> Path:
    raw = os.getenv("SYNTHETIC_DATA_DIR")
    return Path(raw) if raw else ROOT / "synthetic" / "mcp-access"


def _load_json(*parts: str) -> Any:
    path = _data_dir().joinpath(*parts)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def availability() -> dict[str, bool]:
    base = _data_dir()
    return {
        "jira": (base / "jira_real" / "issues.json").is_file(),
        "bitbucket": (base / "bitbucket_real" / "pull_requests").is_dir(),
        "notion": (base / "notion_real" / "search_mcp.json").is_file(),
    }


# ------------------------------------------------------------------- Jira

def load_jira() -> tuple[JiraSite, JiraProject, list[JiraIssue]]:
    site = JiraSite(cloud_id="synthetic", name="Synthetic Jira", url="")
    try:
        raw_issues = _load_json("jira_real", "issues.json")
    except FileNotFoundError:
        raw_issues = []
    if not raw_issues:
        return site, JiraProject("synthetic", "SYNTHETIC", "Synthetic project", ""), []

    project_fields = (raw_issues[0].get("fields") or {}).get("project") or {}
    key = str(project_fields.get("key") or "SYNTHETIC")
    project = JiraProject(
        project_id=key, key=key,
        name=str(project_fields.get("name") or key), description="",
    )

    issues: list[JiraIssue] = []
    for raw in raw_issues:
        issue_key = str(raw.get("key") or "")
        if not issue_key:
            continue
        # `_parse_issue` needs a stable `id`; the fixture only kept `key`
        # (real Jira's internal numeric id was never captured), and the key
        # is already unique and stable, so it stands in for one here.
        issues.append(JiraApiClient._parse_issue({**raw, "id": raw.get("id") or issue_key}))
    return site, project, issues


# -------------------------------------------------------------- Bitbucket

def _bitbucket_repository(raw_repo: dict[str, Any]) -> BitbucketRepository:
    full_name = str(raw_repo.get("full_name") or "synthetic/repo")
    workspace, _, slug = full_name.partition("/")
    slug = slug or full_name
    return BitbucketRepository(
        uuid=str(raw_repo.get("uuid") or f"{{synthetic-{slug}}}"),
        workspace=workspace or "synthetic",
        slug=slug,
        name=str(raw_repo.get("name") or slug),
        full_name=full_name,
        main_branch="main",
        html_url=f"https://bitbucket.org/{full_name}",
        private=True,
        description="",
    )


def _parse_pr(value: dict[str, Any]) -> BitbucketPullRequest:
    """Mirrors the per-item parsing in `BitbucketApiClient.pull_requests`
    (same field paths), just against a fixture list instead of a live page."""
    author = value.get("author") or {}
    source = (value.get("source") or {}).get("branch") or {}
    destination = (value.get("destination") or {}).get("branch") or {}
    return BitbucketPullRequest(
        id=int(value.get("id") or 0),
        title=str(value.get("title") or ""),
        description=str(value.get("description") or ""),
        state=str(value.get("state") or ""),
        author_name=str(author.get("display_name") or "Unknown"),
        author_uuid=str(author.get("uuid") or ""),
        source_branch=str(source.get("name") or ""),
        destination_branch=str(destination.get("name") or ""),
        created_on=str(value.get("created_on") or ""),
        updated_on=str(value.get("updated_on") or ""),
        html_url=str(((value.get("links") or {}).get("html") or {}).get("href") or ""),
    )


def _parse_commit(value: dict[str, Any]) -> BitbucketCommit | None:
    """Mirrors the per-item parsing in `BitbucketApiClient.commits`."""
    message = str(value.get("message") or "").strip()
    if not message:
        return None
    name, email = _split_author(value.get("author") or {})
    return BitbucketCommit(
        commit_hash=str(value.get("hash") or ""), message=message,
        author_name=name, author_email=email,
        date=str(value.get("date") or ""),
        html_url=str(((value.get("links") or {}).get("html") or {}).get("href") or ""),
    )


def load_bitbucket() -> tuple[BitbucketRepository, list[BitbucketPullRequest], dict[int, list[BitbucketCommit]]]:
    pr_dir = _data_dir() / "bitbucket_real" / "pull_requests"
    commit_dir = _data_dir() / "bitbucket_real" / "pr_commits"

    prs: list[BitbucketPullRequest] = []
    repo_raw: dict[str, Any] | None = None
    for path in sorted(pr_dir.glob("*.json")) if pr_dir.is_dir() else []:
        for value in _load_json(*path.relative_to(_data_dir()).parts).get("values", []):
            if repo_raw is None:
                repo_raw = (
                    (value.get("destination") or {}).get("repository")
                    or (value.get("source") or {}).get("repository")
                )
            prs.append(_parse_pr(value))

    commits_by_pr: dict[int, list[BitbucketCommit]] = {}
    for path in sorted(commit_dir.glob("*.json")) if commit_dir.is_dir() else []:
        pr_id = int(path.stem) if path.stem.isdigit() else 0
        commits: list[BitbucketCommit] = []
        for value in _load_json(*path.relative_to(_data_dir()).parts).get("values", []):
            if repo_raw is None:
                repo_raw = value.get("repository")
            commit = _parse_commit(value)
            if commit is not None:
                commits.append(commit)
        commits_by_pr[pr_id] = commits

    return _bitbucket_repository(repo_raw or {}), prs, commits_by_pr


# ----------------------------------------------------------------- Notion

def _notion_page_title(raw_page: dict[str, Any]) -> str:
    for prop in (raw_page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return "".join(
                str(item.get("plain_text") or "")
                for item in prop.get("title") or []
                if isinstance(item, dict)
            )
    return ""


def load_notion() -> tuple[str, str, list[NotionPage]]:
    workspace_id, workspace_name = "synthetic-notion", "Synthetic Notion workspace"
    try:
        search = _load_json("notion_real", "search_mcp.json")
    except FileNotFoundError:
        return workspace_id, workspace_name, []

    pages_dir = _data_dir() / "notion_real" / "pages"
    pages: list[NotionPage] = []
    for raw_page in search.get("results", []):
        page_id = str(raw_page.get("id") or "")
        if not page_id:
            continue
        content_path = pages_dir / f"{page_id}.json"
        content = ""
        if content_path.is_file():
            content = json.loads(content_path.read_text(encoding="utf-8")).get("markdown") or ""
        parent = raw_page.get("parent") or {}
        pages.append(NotionPage(
            page_id=page_id,
            title=_notion_page_title(raw_page) or "Untitled",
            url=str(raw_page.get("url") or ""),
            content=content,
            last_edited_time=str(raw_page.get("last_edited_time") or ""),
            parent_page_id=(
                str(parent.get("page_id")) if parent.get("type") == "page_id" else None
            ),
        ))
    return workspace_id, workspace_name, pages
