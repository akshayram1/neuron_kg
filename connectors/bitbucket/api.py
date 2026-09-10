"""Retrying read-only Bitbucket Cloud REST client."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

BASE_URL = "https://api.bitbucket.org/2.0"
TRANSIENT = {429, 500, 502, 503, 504}
LANGUAGES = {".py": "python", ".md": "markdown"}
SUPPORTED_FILE_TYPES = set(LANGUAGES)
_EMAIL_IN_RAW = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_COMMIT_HASH_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{12}")


class BitbucketApiError(RuntimeError):
    pass


class BitbucketUnauthorized(BitbucketApiError):
    pass


@dataclass(frozen=True)
class BitbucketWorkspace:
    uuid: str
    slug: str
    name: str


@dataclass(frozen=True)
class BitbucketRepository:
    uuid: str
    workspace: str
    slug: str
    name: str
    full_name: str
    main_branch: str
    html_url: str
    private: bool
    description: str


@dataclass(frozen=True)
class BitbucketFile:
    path: str
    commit_hash: str
    size: int
    content: str
    language: str | None


@dataclass(frozen=True)
class BitbucketCommit:
    commit_hash: str
    message: str
    author_name: str
    author_email: str
    date: str
    html_url: str


@dataclass(frozen=True)
class BitbucketPullRequest:
    id: int
    title: str
    description: str
    state: str  # OPEN | MERGED | DECLINED | SUPERSEDED
    author_name: str
    author_uuid: str
    source_branch: str
    destination_branch: str
    created_on: str
    updated_on: str
    html_url: str


def _split_author(raw_author: dict) -> tuple[str, str]:
    user = raw_author.get("user") or {}
    display_name = str(user.get("display_name") or "").strip()
    raw = str(raw_author.get("raw") or "").strip()
    email_match = _EMAIL_IN_RAW.search(raw)
    email = email_match.group(1).lower() if email_match else ""
    name = display_name or (raw.split("<", 1)[0].strip() if raw else "") or "Unknown"
    return name, email


class BitbucketApiClient:
    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None):
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=40)
        self._headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        self._ref_cache: dict[tuple[str, str], str] = {}

    async def __aenter__(self) -> "BitbucketApiClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns:
            await self._client.aclose()

    async def _response(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        response: httpx.Response | None = None
        for attempt in range(5):
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise BitbucketApiError("Bitbucket network request failed") from exc
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 401:
                raise BitbucketUnauthorized("Bitbucket access token expired or was revoked")
            if response.status_code in TRANSIENT and attempt < 4:
                try:
                    delay = float(response.headers.get("Retry-After") or 2 ** attempt)
                except ValueError:
                    delay = float(2 ** attempt)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise BitbucketApiError(f"Bitbucket API failed ({response.status_code})")
            return response
        assert response is not None
        return response

    async def json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await self._response("GET", url, **kwargs)
        value = response.json()
        if not isinstance(value, dict):
            raise BitbucketApiError("Bitbucket returned invalid JSON")
        return value

    async def paginated(self, url: str, params: dict | None = None) -> list[dict]:
        output: list[dict] = []
        next_url: str | None = url
        while next_url:
            data = await self.json(next_url, params=params if next_url == url else None)
            output.extend(item for item in data.get("values", []) if isinstance(item, dict))
            next_url = data.get("next")
        return output

    async def user(self) -> dict[str, Any]:
        """The authenticated account (`read:account` scope)."""
        return await self.json(f"{BASE_URL}/user")

    async def workspace(self, slug: str) -> BitbucketWorkspace:
        """Fetch one workspace by slug.

        There is deliberately no `workspaces()` listing method: Bitbucket has
        removed workspace *enumeration* from this API. Verified against the
        live API with a token holding every read scope (account, team,
        repository, project, pullrequest) — `/2.0/workspaces`,
        `/2.0/user/permissions/workspaces`, `/2.0/teams` and
        `/2.0/user/permissions/repositories` all return 404 "There is no API
        hosted at this URL", and `/2.0/repositories?role=member` returns 410
        (deprecated, CHANGE-2770). Only per-workspace paths still work, so the
        caller must supply the slug.
        """
        value = await self.json(f"{BASE_URL}/workspaces/{quote(slug)}")
        return BitbucketWorkspace(
            str(value.get("uuid") or value.get("slug") or slug),
            str(value.get("slug") or slug),
            str(value.get("name") or value.get("slug") or slug),
        )

    async def resolve_ref(self, repository: BitbucketRepository, ref: str) -> str:
        """Resolve a branch name to its head commit hash.

        Required, not an optimisation: Bitbucket's `src` endpoint cannot take a
        branch name containing '/' (verified live on `feat/aug26-...` —
        percent-encoded `%2F` and a raw slash both 404, because the API cannot
        tell the branch name from the file path). A commit hash is unambiguous
        and works for every branch, so all tree/commit reads go through this.
        """
        if _COMMIT_HASH_RE.fullmatch(ref):
            return ref
        cached = self._ref_cache.get((repository.uuid, ref))
        if cached:
            return cached
        value = await self.json(
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
            f"/refs/branches/{quote(ref, safe='/')}"
        )
        commit_hash = str((value.get("target") or {}).get("hash") or "")
        if not commit_hash:
            raise BitbucketApiError(f"Branch {ref!r} has no resolvable head commit")
        self._ref_cache[(repository.uuid, ref)] = commit_hash
        return commit_hash

    async def branches(self, repository: BitbucketRepository) -> list[str]:
        """Branch names, the repository's own default branch listed first."""
        values = await self.paginated(
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
            f"/refs/branches",
            {"pagelen": 100, "sort": "-target.date"},
        )
        names = [str(value["name"]) for value in values if value.get("name")]
        default = repository.main_branch
        return ([default] if default in names else []) + [n for n in names if n != default]

    async def repositories(self, workspace: str) -> list[BitbucketRepository]:
        values = await self.paginated(f"{BASE_URL}/repositories/{quote(workspace)}", {"pagelen": 100})
        return sorted(
            (self._repository(value, workspace) for value in values if value.get("slug")),
            key=lambda repo: repo.full_name.lower(),
        )

    def _repository(self, value: dict, workspace: str) -> BitbucketRepository:
        return BitbucketRepository(
            str(value.get("uuid") or value["slug"]), workspace, str(value["slug"]),
            str(value.get("name") or value["slug"]), str(value.get("full_name") or f"{workspace}/{value['slug']}"),
            str((value.get("mainbranch") or {}).get("name") or "main"),
            str(((value.get("links") or {}).get("html") or {}).get("href") or ""),
            bool(value.get("is_private", True)), str(value.get("description") or ""),
        )

    async def files(
        self, repository: BitbucketRepository, extensions: set[str], max_bytes: int,
    ) -> tuple[list[BitbucketFile], int, int]:
        """Walk the repository tree, downloading matched files inline.

        Bitbucket's src API has no single recursive-tree endpoint like
        GitHub's git/trees — each directory needs its own listing call — so
        content is fetched during the same walk rather than in a second pass.
        Returns (files, too_large_count, without_text_count).
        """
        output: list[BitbucketFile] = []
        too_large = 0
        without_text = 0
        visited: set[str] = set()
        # Read the tree at a resolved commit, never at a branch name -- see
        # resolve_ref: a branch containing '/' cannot be addressed here.
        encoded_branch = quote(await self.resolve_ref(repository, repository.main_branch), safe="")

        async def walk(path: str) -> None:
            nonlocal too_large, without_text
            if path in visited:
                return
            visited.add(path)
            encoded_path = quote(path, safe="/")
            url = (f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
                   f"/src/{encoded_branch}/{encoded_path}")
            values = await self.paginated(url, {"pagelen": 100})
            for value in values:
                item_path = str(value.get("path") or "")
                if value.get("type") == "commit_directory":
                    await walk(item_path)
                elif value.get("type") == "commit_file":
                    extension = PurePosixPath(item_path).suffix.lower()
                    if extension not in extensions:
                        continue
                    size = int(value.get("size") or 0)
                    if size > max_bytes:
                        too_large += 1
                        continue
                    raw_url = (f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
                               f"/src/{encoded_branch}/{quote(item_path, safe='/')}")
                    response = await self._response("GET", raw_url, headers={"Accept": "text/plain"})
                    try:
                        content = response.content.decode("utf-8")
                    except UnicodeDecodeError:
                        without_text += 1
                        continue
                    commit_hash = str(((value.get("commit") or {}).get("hash")) or "")
                    output.append(BitbucketFile(item_path, commit_hash, size, content, LANGUAGES.get(extension)))
        await walk("")
        return output, too_large, without_text

    async def pull_requests(
        self, repository: BitbucketRepository,
        states: tuple[str, ...] = ("OPEN", "MERGED", "DECLINED"),
        limit: int = 100,
    ) -> list[BitbucketPullRequest]:
        """List pull requests. `state` is repeated as a query param per state
        (httpx encodes a list value that way) -- Bitbucket's endpoint defaults
        to OPEN only otherwise. NOT yet verified against the live API (this
        connection was disconnected before a real PR fetch could be tested);
        verify field names on first real use before trusting them blindly.
        """
        values = await self.paginated(
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}/pullrequests",
            {"pagelen": min(50, max(1, limit)), "state": list(states)},
        )
        output = []
        for value in values[:limit]:
            author = value.get("author") or {}
            source = (value.get("source") or {}).get("branch") or {}
            destination = (value.get("destination") or {}).get("branch") or {}
            output.append(BitbucketPullRequest(
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
            ))
        return output

    async def commits(self, repository: BitbucketRepository, limit: int) -> list[BitbucketCommit]:
        head = await self.resolve_ref(repository, repository.main_branch)
        values = await self.paginated(
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
            f"/commits/{quote(head, safe='')}",
            {"pagelen": min(100, max(1, limit))},
        )
        output = []
        for value in values[:limit]:
            message = str(value.get("message") or "").strip()
            if not message:
                continue
            name, email = _split_author(value.get("author") or {})
            output.append(BitbucketCommit(
                str(value.get("hash") or ""), message, name, email,
                str(value.get("date") or ""),
                str(((value.get("links") or {}).get("html") or {}).get("href") or ""),
            ))
        return output
