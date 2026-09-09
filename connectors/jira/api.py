from __future__ import annotations

import asyncio
from dataclasses import dataclass
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

    async def issues(
        self,
        cloud_id: str,
        project_key: str,
        on_progress: Callable[[int], None] | None = None,
    ) -> list[JiraIssue]:
        output: list[JiraIssue] = []
        next_token: str | None = None
        while True:
            payload: dict[str, Any] = {
                "jql": f'project = "{project_key}" ORDER BY updated ASC',
                "maxResults": 50,
                "fields": [
                    "summary", "description", "status", "issuetype", "created", "updated",
                    "assignee", "reporter", "labels", "components", "parent", "issuelinks", "comment",
                ],
            }
            if next_token:
                payload["nextPageToken"] = next_token
            data = await self.request(
                "POST", f"{self.site_base(cloud_id)}/search/jql",
                headers={**self._headers, "Content-Type": "application/json"}, json=payload,
            )
            values = data.get("issues", [])
            for item in values:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
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

                output.append(
                    JiraIssue(
                        issue_id=str(item["id"]), key=str(item.get("key") or item["id"]),
                        summary=summary, content="\n".join(lines), description=description,
                        status=status, issue_type=issue_type, assignee=assignee, reporter=reporter,
                        labels=labels, blocks=tuple(blocks),
                        created_at=str(fields.get("created") or ""), updated_at=str(fields.get("updated") or ""),
                    )
                )
            if on_progress is not None:
                on_progress(len(output))
            next_token = data.get("nextPageToken")
            if data.get("isLast", not bool(next_token)) or not next_token:
                return output
