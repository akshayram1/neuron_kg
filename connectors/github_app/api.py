"""Retrying read-only GitHub REST client."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .auth import API_VERSION

TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
SUPPORTED_FILE_TYPES = {".py", ".md"}


class GitHubApiError(RuntimeError):
    """A safe-to-display GitHub API failure."""


class GitHubFileTextError(GitHubApiError):
    """A blob exists but cannot be represented as ingestible UTF-8 text."""


@dataclass(frozen=True)
class GitHubRepository:
    repository_id: int
    full_name: str
    name: str
    owner: str
    default_branch: str
    private: bool
    html_url: str


@dataclass(frozen=True)
class GitHubFile:
    path: str
    sha: str
    size: int


@dataclass(frozen=True)
class GitHubCommit:
    sha: str
    message: str
    author_name: str
    author_email: str
    authored_at: str
    html_url: str


class GitHubApiClient:
    def __init__(self, token: str):
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=45,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "graphiti-context-connector",
            },
        )

    async def __aenter__(self) -> "GitHubApiClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response: httpx.Response | None = None
        for attempt in range(4):
            try:
                response = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise GitHubApiError(f"GitHub request failed: {exc}") from exc
                await asyncio.sleep(0.35 * (2 ** attempt))
                continue
            if response.status_code not in TRANSIENT_STATUSES:
                break
            if attempt < 3:
                retry_after = response.headers.get("Retry-After")
                await asyncio.sleep(float(retry_after) if retry_after else 0.5 * (2 ** attempt))
        assert response is not None
        if response.is_error:
            try:
                message = response.json().get("message")
            except (ValueError, AttributeError):
                message = response.reason_phrase
            raise GitHubApiError(f"GitHub API error ({response.status_code}): {message}")
        return response.json()

    @staticmethod
    def _repository(item: dict[str, Any]) -> GitHubRepository:
        owner = item.get("owner") or {}
        return GitHubRepository(
            repository_id=int(item["id"]),
            full_name=str(item["full_name"]),
            name=str(item["name"]),
            owner=str(owner.get("login") or str(item["full_name"]).split("/", 1)[0]),
            default_branch=str(item.get("default_branch") or "main"),
            private=bool(item.get("private")),
            html_url=str(item.get("html_url") or ""),
        )

    async def list_repositories(self) -> list[GitHubRepository]:
        output: list[GitHubRepository] = []
        page = 1
        while True:
            data = await self._get("/installation/repositories", {"per_page": 100, "page": page})
            batch = data.get("repositories", [])
            output.extend(self._repository(item) for item in batch)
            if len(batch) < 100:
                return sorted(output, key=lambda repo: repo.full_name.lower())
            page += 1

    async def list_files(
        self, repository: GitHubRepository, file_types: set[str], max_bytes: int
    ) -> tuple[list[GitHubFile], int]:
        owner, name = repository.full_name.split("/", 1)
        tree = await self._get(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/trees/"
            f"{quote(repository.default_branch, safe='')}",
            {"recursive": "1"},
        )
        if tree.get("truncated"):
            raise GitHubApiError(
                "This repository tree is too large for recursive discovery; select a smaller repository"
            )
        matched: list[GitHubFile] = []
        too_large = 0
        for item in tree.get("tree", []):
            path = str(item.get("path") or "")
            extension = next((ext for ext in file_types if path.lower().endswith(ext)), None)
            if item.get("type") != "blob" or not extension:
                continue
            size = int(item.get("size") or 0)
            if size > max_bytes:
                too_large += 1
                continue
            matched.append(GitHubFile(path=path, sha=str(item["sha"]), size=size))
        return sorted(matched, key=lambda item: item.path.lower()), too_large

    async def read_blob(self, repository: GitHubRepository, sha: str) -> str:
        owner, name = repository.full_name.split("/", 1)
        data = await self._get(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/blobs/{quote(sha, safe='')}"
        )
        if data.get("encoding") != "base64":
            raise GitHubFileTextError("GitHub returned an unsupported blob encoding")
        try:
            raw = base64.b64decode(str(data.get("content") or ""), validate=False)
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubFileTextError("Repository file is not valid UTF-8 text") from exc

    async def list_commits(
        self, repository: GitHubRepository, limit: int
    ) -> list[GitHubCommit]:
        owner, name = repository.full_name.split("/", 1)
        output: list[GitHubCommit] = []
        page = 1
        while len(output) < limit:
            page_size = min(100, limit - len(output))
            batch = await self._get(
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/commits",
                {"sha": repository.default_branch, "per_page": page_size, "page": page},
            )
            if not batch:
                break
            for item in batch:
                commit = item.get("commit") or {}
                author = commit.get("author") or commit.get("committer") or {}
                message = str(commit.get("message") or "").strip()
                if not message:
                    continue
                output.append(GitHubCommit(
                    sha=str(item.get("sha") or ""),
                    message=message,
                    author_name=str(author.get("name") or "Unknown"),
                    author_email=str(author.get("email") or ""),
                    authored_at=str(author.get("date") or ""),
                    html_url=str(item.get("html_url") or ""),
                ))
            if len(batch) < page_size:
                break
            page += 1
        return output[:limit]
