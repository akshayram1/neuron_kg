"""Retrying read-only Bitbucket Cloud REST client."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("neuron.bitbucket")
BASE_URL = "https://api.bitbucket.org/2.0"
# 555 is Bitbucket Cloud's undocumented overload response (seen live on
# diffstat during a 100-commit sync). Treat it like 503 so we back off
# instead of dropping that commit's file list.
TRANSIENT = {429, 500, 502, 503, 504, 555}
LANGUAGES = {".py": "python", ".md": "markdown"}
SUPPORTED_FILE_TYPES = set(LANGUAGES)
_EMAIL_IN_RAW = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")
_COMMIT_HASH_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{12}")


class BitbucketApiError(RuntimeError):
    pass


class BitbucketUnauthorized(BitbucketApiError):
    pass


class BitbucketRateLimited(BitbucketApiError):
    """The rolling provider quota is exhausted and the job must be delayed."""

    def __init__(self, retry_after_seconds: float, detail: str = ""):
        self.retry_after_seconds = max(1.0, retry_after_seconds)
        suffix = f"; {detail}" if detail else ""
        super().__init__(
            f"Bitbucket rate limit reached; retry after "
            f"{round(self.retry_after_seconds)} seconds{suffix}"
        )


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
class BitbucketFileChange:
    """One path from `GET …/diffstat/{commit}` (vs first parent)."""
    path: str
    old_path: str
    status: str  # added | modified | removed | renamed
    lines_added: int
    lines_removed: int


@dataclass(frozen=True)
class BitbucketCommit:
    commit_hash: str
    message: str
    author_name: str
    author_email: str
    date: str
    html_url: str
    files: tuple[BitbucketFileChange, ...] = ()


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


def src_listing_url(src_root: str, path: str) -> str:
    """Directory listing URL. Root must be `{src_root}/`, not `{src_root}`.

    Bitbucket 404s `GET /src/{commit}` (verified on the parallel-walk
    regression) and lists the tree at `GET /src/{commit}/`.
    """
    return f"{src_root}/{quote(path, safe='/')}"


def parse_diffstat(values: list[dict]) -> tuple[BitbucketFileChange, ...]:
    """Map Bitbucket diffstat JSON rows to file changes.

    Path prefers `new.path` (added / modified / renamed target), then
    `old.path` (removed). Official shape verified against
    https://developer.atlassian.com/cloud/bitbucket/rest/api-group-commits/
    """
    changes: list[BitbucketFileChange] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        new = value.get("new") or {}
        old = value.get("old") or {}
        path = str(new.get("path") or old.get("path") or "").strip()
        if not path:
            continue
        try:
            added = int(value.get("lines_added") or 0)
            removed = int(value.get("lines_removed") or 0)
        except (TypeError, ValueError):
            added, removed = 0, 0
        changes.append(BitbucketFileChange(
            path=path,
            old_path=str(old.get("path") or "").strip(),
            status=str(value.get("status") or "modified"),
            lines_added=added,
            lines_removed=removed,
        ))
    return tuple(changes)


def _split_author(raw_author: dict) -> tuple[str, str]:
    user = raw_author.get("user") or {}
    display_name = str(user.get("display_name") or "").strip()
    raw = str(raw_author.get("raw") or "").strip()
    email_match = _EMAIL_IN_RAW.search(raw)
    email = email_match.group(1).lower() if email_match else ""
    name = display_name or (raw.split("<", 1)[0].strip() if raw else "") or "Unknown"
    return name, email


class BitbucketApiClient:
    def __init__(
        self, access_token: str, client: httpx.AsyncClient | None = None, *,
        max_concurrency: int | None = None, min_interval: float | None = None,
        max_retries: int = 8,
    ):
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=40)
        self._headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        self._ref_cache: dict[tuple[str, str], str] = {}
        # Bitbucket explicitly recommends avoiding concurrent requests after a
        # 429. Every endpoint in this client shares this gate, including the
        # tree walk and commit diffstats that run as separate asyncio tasks.
        configured_concurrency = (
            max_concurrency if max_concurrency is not None
            else int(os.getenv("BITBUCKET_API_CONCURRENCY", "1"))
        )
        configured_interval = (
            min_interval if min_interval is not None
            else float(os.getenv("BITBUCKET_API_MIN_INTERVAL_SECONDS", "0.25"))
        )
        self._request_sem = asyncio.Semaphore(max(1, configured_concurrency))
        self._pace_lock = asyncio.Lock()
        self._min_interval = max(0.0, configured_interval)
        self._adaptive_interval = self._min_interval
        self._max_request_interval = max(
            self._min_interval,
            float(os.getenv("BITBUCKET_MAX_REQUEST_INTERVAL_SECONDS", "5")),
        )
        self._success_streak = 0
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._max_retries = max(1, max_retries)
        self._max_inline_retry = max(
            0.0, float(os.getenv("BITBUCKET_MAX_INLINE_RETRY_SECONDS", "60")),
        )

    async def __aenter__(self) -> "BitbucketApiClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns:
            await self._client.aclose()

    async def _wait_for_request_turn(self) -> None:
        """Apply process-local pacing and any shared provider cooldown."""
        loop = asyncio.get_running_loop()
        async with self._pace_lock:
            while True:
                now = loop.time()
                delay = max(self._next_request_at, self._blocked_until) - now
                if delay <= 0:
                    self._next_request_at = now + self._adaptive_interval
                    return
                await asyncio.sleep(delay)

    async def _set_cooldown(self, seconds: float) -> None:
        async with self._pace_lock:
            self._blocked_until = max(
                self._blocked_until,
                asyncio.get_running_loop().time() + max(0.0, seconds),
            )

    async def _apply_rate_limit(self, seconds: float) -> float:
        """Slow every later request after a 429, not only its retry."""
        async with self._pace_lock:
            now = asyncio.get_running_loop().time()
            self._blocked_until = max(self._blocked_until, now + max(0.0, seconds))
            self._adaptive_interval = min(
                self._max_request_interval,
                max(self._adaptive_interval * 1.5, seconds * 1.1, self._min_interval),
            )
            self._success_streak = 0
            return self._adaptive_interval

    async def _record_success(self) -> None:
        """Recover throughput only after a sustained run without throttling."""
        async with self._pace_lock:
            self._success_streak += 1
            if self._success_streak >= 20:
                self._adaptive_interval = max(
                    self._min_interval, self._adaptive_interval * 0.9,
                )
                self._success_streak = 0

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Prefer provider guidance, otherwise use exponential jitter."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError):
                    pass
        reset = response.headers.get("X-RateLimit-Reset")
        if reset:
            try:
                parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
            except ValueError:
                pass
        base = min(60.0, float(2 ** attempt))
        return base * random.uniform(1.0, 1.25)

    async def _response(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        response: httpx.Response | None = None
        for attempt in range(self._max_retries):
            await self._wait_for_request_turn()
            try:
                async with self._request_sem:
                    response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self._max_retries - 1:
                    raise BitbucketApiError("Bitbucket network request failed") from exc
                await self._set_cooldown(min(30.0, float(2 ** attempt)))
                continue
            if response.status_code == 401:
                raise BitbucketUnauthorized("Bitbucket access token expired or was revoked")
            if response.status_code in TRANSIENT:
                delay = self._retry_delay(response, attempt)
                if response.status_code == 429:
                    interval = await self._apply_rate_limit(delay)
                    logger.warning(
                        "Bitbucket rate limited; retry_after=%.1fs request_gap=%.1fs "
                        "attempt=%d/%d url=%s",
                        delay, interval, attempt + 1, self._max_retries, url,
                    )
                    # A long rolling-window wait belongs in the durable queue,
                    # where it survives backend restarts without tying up a
                    # worker coroutine.
                    if delay > self._max_inline_retry or attempt == self._max_retries - 1:
                        raise BitbucketRateLimited(
                            delay,
                            response.headers.get("RateLimit-Reason", ""),
                        )
                elif attempt == self._max_retries - 1:
                    raise BitbucketApiError(f"Bitbucket API failed ({response.status_code})")
                if response.status_code != 429:
                    await self._set_cooldown(delay)
                continue
            if response.status_code >= 400:
                raise BitbucketApiError(f"Bitbucket API failed ({response.status_code})")
            await self._record_success()
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
    ) -> tuple[list[BitbucketFile], int, int, int]:
        """Walk the repository tree, downloading matched files inline.

        Bitbucket's src API has no single recursive-tree endpoint like
        GitHub's git/trees — each directory needs its own listing call — so
        content is fetched during the same walk rather than in a second pass.
        Returns (files, too_large_count, without_text_count,
        extension_filtered_count) -- the last being files whose extension is
        not in `extensions` (plan.md Phase 0.6): previously this filter
        (below) silently `continue`d with no counter at all, so a repo full
        of e.g. `.go`/`.json` files reported a clean sync with no sign that
        almost everything was dropped before it ever reached the tree walk's
        other counters.
        """
        output: list[BitbucketFile] = []
        too_large = 0
        without_text = 0
        extension_filtered = 0
        visited: set[str] = set()
        # Read the tree at a resolved commit, never at a branch name -- see
        # resolve_ref: a branch containing '/' cannot be addressed here.
        encoded_branch = quote(await self.resolve_ref(repository, repository.main_branch), safe="")
        lock = asyncio.Lock()
        src_root = (
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/"
            f"{quote(repository.slug)}/src/{encoded_branch}"
        )

        async def download(item_path: str, size: int, commit_hash: str) -> None:
            nonlocal without_text
            raw_url = f"{src_root}/{quote(item_path, safe='/')}"
            response = await self._response("GET", raw_url, headers={"Accept": "text/plain"})
            try:
                content = response.content.decode("utf-8")
            except UnicodeDecodeError:
                async with lock:
                    without_text += 1
                return
            language = LANGUAGES.get(PurePosixPath(item_path).suffix.lower())
            async with lock:
                output.append(BitbucketFile(item_path, commit_hash, size, content, language))

        async def walk(path: str) -> None:
            nonlocal too_large, extension_filtered
            async with lock:
                if path in visited:
                    return
                visited.add(path)
            url = src_listing_url(src_root, path)
            values = await self.paginated(url, {"pagelen": 100})
            child_dirs: list[str] = []
            pending: list[tuple[str, int, str]] = []
            for value in values:
                item_path = str(value.get("path") or "")
                if value.get("type") == "commit_directory":
                    child_dirs.append(item_path)
                    continue
                if value.get("type") != "commit_file":
                    continue
                if PurePosixPath(item_path).suffix.lower() not in extensions:
                    async with lock:
                        extension_filtered += 1
                    continue
                size = int(value.get("size") or 0)
                if size > max_bytes:
                    async with lock:
                        too_large += 1
                    continue
                commit_hash = str(((value.get("commit") or {}).get("hash")) or "")
                pending.append((item_path, size, commit_hash))
            # Bitbucket's rolling quotas are consumer-wide for OAuth. Keep the
            # tree walk sequential so a single large repository cannot fan out
            # hundreds of raw-file requests at once.
            for child in child_dirs:
                await walk(child)
            for item in pending:
                await download(*item)

        await walk("")
        return output, too_large, without_text, extension_filtered

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

    async def commits(
        self, repository: BitbucketRepository, limit: int,
    ) -> tuple[list[BitbucketCommit], bool]:
        """List up to `limit` commits, newest first, plus whether more exist
        beyond the cap (plan.md Phase 0.6).

        This used to call `self.paginated(...)`, which walks every page
        Bitbucket has -- following `next` until the API stops returning one
        -- regardless of `limit`, only slicing down to `limit` afterwards.
        For a repository with thousands of commits that meant thousands of
        unneeded requests just to throw almost all of the result away, and
        it still never told the caller how many commits existed beyond the
        cap. This instead stops paging as soon as `limit` raw items have
        been gathered. Bitbucket's own page JSON already says whether a
        `next` page exists -- that's a free signal from a request already
        made, not an extra one -- so `commits_capped` costs nothing beyond
        what fetching `limit` commits already costs. An *exact* count of
        commits beyond the cap would need paging all the way to the real
        end, i.e. the same expensive, wasted walk this now avoids, so only
        the boolean is reported.
        """
        head = await self.resolve_ref(repository, repository.main_branch)
        url = (
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
            f"/commits/{quote(head, safe='')}"
        )
        pagelen = min(100, max(1, limit))
        raw: list[dict] = []
        next_url: str | None = url
        while next_url and len(raw) < limit:
            data = await self.json(next_url, params={"pagelen": pagelen} if next_url == url else None)
            raw.extend(item for item in data.get("values", []) if isinstance(item, dict))
            next_url = data.get("next")
        commits_capped = len(raw) > limit or bool(next_url)
        output = []
        for value in raw[:limit]:
            message = str(value.get("message") or "").strip()
            if not message:
                continue
            name, email = _split_author(value.get("author") or {})
            output.append(BitbucketCommit(
                str(value.get("hash") or ""), message, name, email,
                str(value.get("date") or ""),
                str(((value.get("links") or {}).get("html") or {}).get("href") or ""),
            ))
        return output, commits_capped

    async def diffstat(
        self, repository: BitbucketRepository, commit_hash: str,
    ) -> tuple[BitbucketFileChange, ...]:
        """Files this commit changed vs its first parent. JSON, not the patch."""
        values = await self.paginated(
            f"{BASE_URL}/repositories/{quote(repository.workspace)}/{quote(repository.slug)}"
            f"/diffstat/{quote(commit_hash, safe='')}",
            {"pagelen": 100},
        )
        return parse_diffstat(values)

    async def attach_diffstats(
        self, repository: BitbucketRepository, commits: list[BitbucketCommit],
    ) -> list[BitbucketCommit]:
        """Fill commit file lists sequentially; one ordinary failure stays empty."""
        if not commits:
            return commits
        output: list[BitbucketCommit] = []
        for commit in commits:
            try:
                files = await self.diffstat(repository, commit.commit_hash)
            except BitbucketRateLimited:
                raise
            except BitbucketApiError as exc:
                logger.warning(
                    "diffstat failed commit=%s repo=%s: %s",
                    commit.commit_hash[:12], repository.full_name, exc,
                )
                output.append(commit)
            else:
                output.append(replace(commit, files=files))
        return output
