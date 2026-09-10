from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

ATLASSIAN_API = "https://api.atlassian.com"
TRANSIENT = {429, 500, 502, 503, 504}


class JiraApiError(RuntimeError):
    pass


class JiraUnauthorized(JiraApiError):
    pass


@dataclass(frozen=True)
class JiraSite:
    cloud_id: str
    name: str
    url: str


@dataclass(frozen=True)
class JiraProject:
    project_id: str
    key: str
    name: str
    description: str


@dataclass(frozen=True)
class JiraPerson:
    account_id: str
    display_name: str
    email: str | None = None


@dataclass(frozen=True)
class JiraChange:
    """One field-level changelog item. `from_id`/`to_id` are account ids for
    assignee; display names live in the string fields."""

    at: str
    field: str
    from_id: str | None
    from_string: str | None
    to_id: str | None
    to_string: str | None
    author: str | None


@dataclass(frozen=True)
class JiraIssue:
    """`content` stays a fully flattened text blob (used for hashing and for
    the semantic-pass profile — plan.md §3 Pass B). Every field below it is
    the SAME data, exposed unflattened, because it is structured API data,
    not free text, and belongs in the deterministic pass (plan.md §3 Pass A)
    with zero LLM involvement. Do not re-derive these by parsing `content`."""

    issue_id: str
    key: str
    summary: str
    content: str
    description: str
    status: str
    issue_type: str
    assignee: JiraPerson | None
    reporter: JiraPerson | None
    labels: tuple[str, ...]
    blocks: tuple[str, ...]  # issue_id (stable id, NOT key -- keys can change) of each blocked issue
    created_at: str
    updated_at: str
    parent_id: str | None = None
    parent_key: str | None = None
    changes: tuple[JiraChange, ...] = field(default_factory=tuple)


def adf_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(adf_text(item) for item in value)
    if not isinstance(value, dict):
        return ""
    node_type = value.get("type")
    if node_type == "text":
        return str(value.get("text") or "")
    if node_type == "hardBreak":
        return "\n"
    children = value.get("content") or []
    rendered = "".join(adf_text(item) for item in children)
    if node_type in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
        return rendered.strip() + "\n"
    return rendered


class JiraApiClient:
    def __init__(self, access_token: str, client: httpx.AsyncClient | None = None):
        self._owns = client is None
        self._client = client or httpx.AsyncClient(timeout=40)
        self._headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        if self._owns:
            await self._client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> Any:
        request_headers = {**self._headers, **kwargs.pop("headers", {})}
        for attempt in range(5):
            try:
                response = await self._client.request(method, url, headers=request_headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 4:
                    raise JiraApiError("Jira network request failed") from exc
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code == 401:
                detail = (response.text or "").strip()[:240]
                raise JiraUnauthorized(detail or "Jira access token expired or was revoked")
            if response.status_code in TRANSIENT and attempt < 4:
                try:
                    delay = float(response.headers.get("Retry-After") or 2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                detail = (response.text or "").strip()[:240]
                raise JiraApiError(f"Jira API failed ({response.status_code}){': ' + detail if detail else ''}")
            return response.json()
        raise JiraApiError("Jira retry budget exhausted")

    async def sites(self) -> list[JiraSite]:
        value = await self.request("GET", f"{ATLASSIAN_API}/oauth/token/accessible-resources")
        return [
            JiraSite(str(item["id"]), str(item.get("name") or "Jira site"), str(item.get("url") or ""))
            for item in value if isinstance(item, dict) and item.get("id")
        ]

    def site_base(self, cloud_id: str) -> str:
        return f"{ATLASSIAN_API}/ex/jira/{cloud_id}/rest/api/3"

    async def projects(self, cloud_id: str) -> list[JiraProject]:
        output: list[JiraProject] = []
        start_at = 0
        while True:
            data = await self.request(
                "GET", f"{self.site_base(cloud_id)}/project/search",
                params={"startAt": start_at, "maxResults": 50, "expand": "description"},
            )
            values = data.get("values", [])
            output.extend(
                JiraProject(
                    str(item["id"]), str(item.get("key") or ""),
                    str(item.get("name") or item.get("key") or "Jira project"),
                    adf_text(item.get("description")).strip(),
                )
                for item in values if isinstance(item, dict) and item.get("id")
            )
            if data.get("isLast", True) or not values:
                return output
            start_at = int(data.get("startAt", start_at)) + len(values)

    _ISSUE_FIELDS = [
        "summary", "description", "status", "issuetype", "created", "updated",
        "assignee", "reporter", "labels", "components", "parent", "issuelinks", "comment",
    ]

    async def _search_raw(
        self, cloud_id: str, jql: str, fields: list[str],
        on_progress: Callable[[int], None] | None = None,
    ) -> list[dict]:
        output: list[dict] = []
        next_token: str | None = None
        while True:
            payload: dict[str, Any] = {
                "jql": jql, "maxResults": 100, "fields": fields, "expand": "changelog",
            }
            if next_token:
                payload["nextPageToken"] = next_token
            data = await self.request(
                "POST", f"{self.site_base(cloud_id)}/search/jql",
                headers={**self._headers, "Content-Type": "application/json"}, json=payload,
            )
            output.extend(item for item in data.get("issues", []) if isinstance(item, dict) and item.get("id"))
            if on_progress is not None:
                on_progress(len(output))
            next_token = data.get("nextPageToken")
            if data.get("isLast", not bool(next_token)) or not next_token:
                return output

    @staticmethod
    def _parse_issue(item: dict) -> JiraIssue:
        fields = item.get("fields") or {}
        summary = str(fields.get("summary") or item.get("key") or "Jira issue")
        status = str((fields.get("status") or {}).get("name", ""))
        issue_type = str((fields.get("issuetype") or {}).get("name", ""))
        lines = [
            f"Issue: {item.get('key')}", f"Summary: {summary}",
            f"Type: {issue_type}", f"Status: {status}",
        ]

        def _person(raw: Any) -> JiraPerson | None:
            if not isinstance(raw, dict) or not raw.get("accountId"):
                return None
            return JiraPerson(
                str(raw["accountId"]), str(raw.get("displayName") or raw["accountId"]),
                raw.get("emailAddress"),
            )

        assignee = _person(fields.get("assignee"))
        reporter = _person(fields.get("reporter"))
        for label, person in (("Assignee", assignee), ("Reporter", reporter)):
            if person:
                lines.append(f"{label}: {person.display_name}")

        labels = tuple(str(label) for label in (fields.get("labels") or []))
        if labels:
            lines.append("Labels: " + ", ".join(labels))

        description = adf_text(fields.get("description")).strip()
        if description:
            lines.extend(("", "Description:", description))

        comments = ((fields.get("comment") or {}).get("comments") or [])
        for comment in comments:
            author = (comment.get("author") or {}).get("displayName") or "Unknown"
            body = adf_text(comment.get("body")).strip()
            if body:
                lines.extend(("", f"Comment by {author}:", body))

        blocks: list[str] = []
        for link in fields.get("issuelinks") or []:
            if not isinstance(link, dict):
                continue
            link_type = link.get("type") or {}
            outward_name = str(link_type.get("outward") or "").lower()
            outward_issue = link.get("outwardIssue") or {}
            if "block" in outward_name and outward_issue.get("id"):
                blocks.append(str(outward_issue["id"]))

        parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
        parent_id = str(parent["id"]) if parent.get("id") else None
        parent_key = str(parent["key"]) if parent.get("key") else None

        return JiraIssue(
            issue_id=str(item["id"]), key=str(item.get("key") or item["id"]),
            summary=summary, content="\n".join(lines), description=description,
            status=status, issue_type=issue_type, assignee=assignee, reporter=reporter,
            labels=labels, blocks=tuple(blocks),
            created_at=str(fields.get("created") or ""), updated_at=str(fields.get("updated") or ""),
            parent_id=parent_id, parent_key=parent_key,
            changes=tuple(_parse_changelog(item.get("changelog"))),
        )

    async def issues(
        self,
        cloud_id: str,
        project_key: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[JiraIssue]:
        raw = await self._search_raw(
            cloud_id, f'project = "{project_key}" ORDER BY updated ASC', self._ISSUE_FIELDS, on_progress,
        )
        return [self._parse_issue(item) for item in raw]

    async def subtree_keys(self, cloud_id: str, root_key: str, max_depth: int = 8) -> list[str]:
        """BFS every descendant of `root_key`, root included.

        Jira's hierarchy fans out through two different relations depending
        on level: a sub-task points at its parent Task/Story via `parent`,
        while Story/Task -> Epic uses the reserved JQL field `"Epic Link"`
        (classic/company-managed projects) or `parentEpic` (team-managed) --
        never `parent`. A single JQL clause only ever catches one of these
        hops, so an Epic -> Task -> Sub-task chain (verified real case:
        DATAOS-3833 -> DATAOS-3839 -> DATAOS-4346) needs one BFS level per
        hop, not one query.
        """
        visited = {root_key}
        frontier = [root_key]
        for _ in range(max_depth):
            if not frontier:
                break
            keys_clause = ", ".join(f'"{key}"' for key in frontier)
            jql = (f'"Epic Link" in ({keys_clause}) OR parent in ({keys_clause}) '
                   f'OR parentEpic in ({keys_clause})')
            raw = await self._search_raw(cloud_id, jql, ["summary"])
            frontier = []
            for item in raw:
                key = str(item.get("key") or "")
                if key and key not in visited:
                    visited.add(key)
                    frontier.append(key)
        return sorted(visited)

    async def issues_by_keys(
        self, cloud_id: str, keys: list[str],
        on_progress: Callable[[int], None] | None = None,
    ) -> list[JiraIssue]:
        if not keys:
            return []
        output: list[JiraIssue] = []
        # JQL "in" lists are practically capped well below Jira's hard limit;
        # batching keeps this correct regardless of subtree size.
        for start in range(0, len(keys), 100):
            batch = keys[start:start + 100]
            keys_clause = ", ".join(f'"{key}"' for key in batch)
            raw = await self._search_raw(
                cloud_id, f"key in ({keys_clause}) ORDER BY updated ASC", self._ISSUE_FIELDS,
            )
            output.extend(self._parse_issue(item) for item in raw)
            if on_progress is not None:
                on_progress(len(output))
        return output


def _parse_changelog(raw: Any) -> list[JiraChange]:
    if not isinstance(raw, dict):
        return []
    changes: list[JiraChange] = []
    for history in raw.get("histories") or []:
        if not isinstance(history, dict):
            continue
        at = str(history.get("created") or "")
        author = ((history.get("author") or {}).get("displayName") if isinstance(history.get("author"), dict) else None)
        for item in history.get("items") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "")
            if field not in {"assignee", "status"}:
                continue
            changes.append(JiraChange(
                at=at, field=field,
                from_id=str(item["from"]) if item.get("from") else None,
                from_string=str(item["fromString"]) if item.get("fromString") else None,
                to_id=str(item["to"]) if item.get("to") else None,
                to_string=str(item["toString"]) if item.get("toString") else None,
                author=author,
            ))
    changes.sort(key=lambda change: change.at)
    return changes


def field_intervals(
    created_at: str,
    current_id: str | None,
    current_name: str | None,
    changes: tuple[JiraChange, ...] | list[JiraChange],
    field: str,
) -> list[tuple[str, str | None, str | None, str | None]]:
    """Walk a field's changelog into closed [start, end) intervals.

    Each tuple is (valid_from, valid_to, id, display_name). The last interval
    is open (end=None) when it matches the current value; older ones are
    closed at the next change. A source that never changed the field yields
    one open interval from `created_at` if a current value exists.
    """
    relevant = [change for change in changes if change.field == field and change.at]
    if not relevant:
        if created_at and (current_id or current_name):
            return [(created_at, None, current_id, current_name)]
        return []

    intervals: list[tuple[str, str | None, str | None, str | None]] = []
    cursor_id, cursor_name = relevant[0].from_id, relevant[0].from_string
    cursor_start = created_at or relevant[0].at
    for change in relevant:
        if cursor_id or cursor_name:
            intervals.append((cursor_start, change.at, cursor_id, cursor_name))
        cursor_id, cursor_name, cursor_start = change.to_id, change.to_string, change.at
    if cursor_id or cursor_name:
        intervals.append((cursor_start, None, cursor_id, cursor_name))
    return [(start, end, vid, name) for start, end, vid, name in intervals if start and (vid or name)]
