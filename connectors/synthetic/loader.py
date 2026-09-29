"""Load the real Jira, Bitbucket and Notion export stored in ``nilus_data``.

The export holds API response shapes captured once (Jira issue search results,
Bitbucket pull requests/commits/diffs plus a source checkout, and Notion search
results with rendered markdown) with no live credentials or network access
needed to replay them. Each ``load_*`` function returns exactly the
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
    LANGUAGES, SUPPORTED_FILE_TYPES, BitbucketCommit, BitbucketFile, BitbucketFileChange,
    BitbucketPullRequest,
    BitbucketRepository,
    _split_author,
    parse_diffstat,
)
from connectors.jira.api import JiraApiClient, JiraIssue, JiraProject, JiraSite
from connectors.notion.api import NotionPage
from util.paths import ROOT

LOCAL_DATA_JIRA_CONNECTION = "nilus-data-jira"
LOCAL_DATA_BITBUCKET_CONNECTION = "nilus-data-bitbucket"
LOCAL_DATA_NOTION_CONNECTION = "nilus-data-notion"

# Compatibility aliases for existing imports while the UI/API terminology is
# migrated from the old fixture panel to the real local export.
SYNTHETIC_JIRA_CONNECTION = LOCAL_DATA_JIRA_CONNECTION
SYNTHETIC_BITBUCKET_CONNECTION = LOCAL_DATA_BITBUCKET_CONNECTION
SYNTHETIC_NOTION_CONNECTION = LOCAL_DATA_NOTION_CONNECTION


def _data_dir() -> Path:
    raw = os.getenv("NILUS_DATA_DIR") or os.getenv("SYNTHETIC_DATA_DIR")
    return Path(raw) if raw else ROOT / "nilus_data"


def data_dir() -> Path:
    return _data_dir()


def _load_json(*parts: str) -> Any:
    path = _data_dir().joinpath(*parts)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def availability() -> dict[str, bool]:
    base = _data_dir()
    return {
        "jira": (base / "jira_real" / "issues.json").is_file(),
        "bitbucket": (
            (base / "bitbucket_real" / "pull_requests.json").is_file()
            or (base / "bitbucket_real" / "pull_requests").is_dir()
        ),
        "notion": (base / "notion_real" / "search_mcp.json").is_file(),
    }


def data_summary() -> dict[str, dict[str, int]]:
    """Counts shown before a user starts a potentially long ingest."""
    base = _data_dir()
    jira = _load_optional(base / "jira_real" / "issues.json", [])
    notion = _load_optional(base / "notion_real" / "search_mcp.json", {})
    prs = _load_optional(base / "bitbucket_real" / "pull_requests.json", {})
    commits = _load_optional(base / "bitbucket_real" / "commits.json", {})
    commit_hashes = {
        str(value.get("hash") or "")
        for value in commits.get("values", [])
        if isinstance(value, dict) and value.get("hash")
    } if isinstance(commits, dict) else set()
    for path in (base / "bitbucket_real" / "pr_commits").glob("*.json"):
        for value in _load_optional(path, {}).get("values", []):
            if isinstance(value, dict) and value.get("hash"):
                commit_hashes.add(str(value["hash"]))
    raw_repository = _load_optional(base / "bitbucket_real" / "repository.json", {})
    source_files = len(load_bitbucket_files(_bitbucket_repository(raw_repository)))
    return {
        "jira": {"issues": len(jira) if isinstance(jira, list) else 0},
        "bitbucket": {
            "pull_requests": len(prs.get("values", [])) if isinstance(prs, dict) else 0,
            "commits": len(commit_hashes),
            "source_files": source_files,
        },
        "notion": {
            "pages": len(notion.get("results", [])) if isinstance(notion, dict) else 0,
        },
    }


def _load_optional(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ------------------------------------------------------------------- Jira

def load_jira() -> tuple[JiraSite, JiraProject, list[JiraIssue]]:
    site = JiraSite(cloud_id="nilus-data", name="Nilus Jira export", url="")
    try:
        raw_issues = _load_json("jira_real", "issues.json")
    except FileNotFoundError:
        raw_issues = []
    if not raw_issues:
        return site, JiraProject("nilus-data", "LOCAL", "Local Jira export", ""), []

    project_fields = (raw_issues[0].get("fields") or {}).get("project") or {}
    key = str(project_fields.get("key") or "LOCAL")
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
    full_name = str(raw_repo.get("full_name") or "local/repo")
    workspace, _, slug = full_name.partition("/")
    slug = slug or full_name
    return BitbucketRepository(
        uuid=str(raw_repo.get("uuid") or f"{{local-{slug}}}"),
        workspace=workspace or "local",
        slug=slug,
        name=str(raw_repo.get("name") or slug),
        full_name=full_name,
        main_branch=str((raw_repo.get("mainbranch") or {}).get("name") or "main"),
        html_url=str(((raw_repo.get("links") or {}).get("html") or {}).get("href") or f"https://bitbucket.org/{full_name}"),
        private=bool(raw_repo.get("is_private", True)),
        description=str(raw_repo.get("description") or ""),
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


def _parse_commit(
    value: dict[str, Any], files: tuple[BitbucketFileChange, ...] = (),
) -> BitbucketCommit | None:
    """Mirrors the per-item parsing in `BitbucketApiClient.commits`."""
    message = str(value.get("message") or "").strip()
    if not message:
        return None
    if value.get("author_name") or value.get("author_email"):
        name = str(value.get("author_name") or "Unknown")
        email = str(value.get("author_email") or "").lower()
    else:
        name, email = _split_author(value.get("author") or {})
    return BitbucketCommit(
        commit_hash=str(value.get("hash") or ""), message=message,
        author_name=name, author_email=email,
        date=str(value.get("date") or ""),
        html_url=str(
            value.get("html_url")
            or ((value.get("links") or {}).get("html") or {}).get("href")
            or ""
        ),
        files=files,
    )


def _local_diffstat(commit_hash: str) -> tuple[BitbucketFileChange, ...]:
    path = _data_dir() / "bitbucket_real" / "commit_diffstats" / f"{commit_hash}.json"
    raw = _load_optional(path, {})
    if not isinstance(raw, dict):
        return ()
    if isinstance(raw.get("values"), list):
        return parse_diffstat(raw["values"])

    stats: dict[str, tuple[int, int]] = {}
    for line in raw.get("numstat", []):
        parts = str(line).split("\t", 2)
        if len(parts) != 3:
            continue
        added = int(parts[0]) if parts[0].isdigit() else 0
        removed = int(parts[1]) if parts[1].isdigit() else 0
        stats[parts[2]] = (added, removed)

    changes: list[BitbucketFileChange] = []
    for line in raw.get("name_status", []):
        parts = str(line).split("\t")
        if len(parts) < 2:
            continue
        code = parts[0]
        old_path = parts[1] if code.startswith("R") and len(parts) > 2 else ""
        path_value = parts[2] if old_path else parts[1]
        added, removed = stats.get(path_value, (0, 0))
        status = {
            "A": "added", "D": "removed", "M": "modified", "R": "renamed",
        }.get(code[:1], "modified")
        changes.append(BitbucketFileChange(
            path=path_value, old_path=old_path, status=status,
            lines_added=added, lines_removed=removed,
        ))
    return tuple(changes)


def load_bitbucket() -> tuple[BitbucketRepository, list[BitbucketPullRequest], dict[int, list[BitbucketCommit]]]:
    pr_dir = _data_dir() / "bitbucket_real" / "pull_requests"
    commit_dir = _data_dir() / "bitbucket_real" / "pr_commits"

    prs: list[BitbucketPullRequest] = []
    repo_raw = _load_optional(_data_dir() / "bitbucket_real" / "repository.json", None)
    pr_files = sorted(pr_dir.glob("*.json")) if pr_dir.is_dir() else []
    single_pr_file = _data_dir() / "bitbucket_real" / "pull_requests.json"
    if single_pr_file.is_file():
        pr_files = [single_pr_file]
    for path in pr_files:
        for value in _load_optional(path, {}).get("values", []):
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
        for value in _load_optional(path, {}).get("values", []):
            if repo_raw is None:
                repo_raw = value.get("repository")
            commit_hash = str(value.get("hash") or "")
            commit = _parse_commit(value, _local_diffstat(commit_hash))
            if commit is not None:
                commits.append(commit)
        commits_by_pr[pr_id] = commits

    # Latest branch commits are exported independently from PR membership.
    # Keep them in bucket 0; the route flattens/deduplicates all buckets.
    branch_commits: list[BitbucketCommit] = []
    for value in _load_optional(
        _data_dir() / "bitbucket_real" / "commits.json", {},
    ).get("values", []):
        commit_hash = str(value.get("hash") or "")
        commit = _parse_commit(value, _local_diffstat(commit_hash))
        if commit is not None:
            branch_commits.append(commit)
    if branch_commits:
        commits_by_pr[0] = branch_commits

    return _bitbucket_repository(repo_raw or {}), prs, commits_by_pr


def _bitbucket_source_root() -> Path | None:
    root = _data_dir() / "bitbucket_real"
    manifest = _load_optional(root / "manifest.json", {})
    archive_name = str((manifest.get("source") or {}).get("archive") or "")
    if archive_name:
        candidate = root / Path(archive_name).stem
        if candidate.is_dir():
            return candidate
    candidates = sorted(
        path for path in root.glob("nilus-*")
        if path.is_dir() and path.name != "repository.git"
    )
    return candidates[0] if candidates else None


def load_bitbucket_files(repository: BitbucketRepository) -> list[BitbucketFile]:
    source_root = _bitbucket_source_root()
    if source_root is None:
        return []
    manifest = _load_optional(_data_dir() / "bitbucket_real" / "manifest.json", {})
    commit_hash = str((manifest.get("git_mirror") or {}).get("head") or repository.main_branch)
    max_bytes = int(os.getenv("BITBUCKET_MAX_FILE_BYTES", "1000000"))
    files: list[BitbucketFile] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_FILE_TYPES:
            continue
        size = path.stat().st_size
        if size > max_bytes:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files.append(BitbucketFile(
            path.relative_to(source_root).as_posix(), commit_hash, size, content,
            LANGUAGES.get(path.suffix.lower()),
        ))
    return files


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
    workspace_id, workspace_name = LOCAL_DATA_NOTION_CONNECTION, "Nilus Notion export"
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
