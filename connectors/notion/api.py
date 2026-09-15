"""Async Notion API reader used by the OAuth ingestion pipeline."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

logger = logging.getLogger("notion_connector")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
HEADING_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}


class NotionApiError(RuntimeError):
    pass


class NotionUnauthorized(NotionApiError):
    pass


@dataclass(frozen=True)
class NotionPage:
    page_id: str
    title: str
    url: str
    content: str
    last_edited_time: str
    parent_page_id: str | None = None


class NotionApiClient:
    """A rate-limited, retrying reader for pages and nested block children."""

    def __init__(
        self,
        access_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        request_interval: float | None = None,
    ):
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=30)
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Notion-Version": NOTION_API_VERSION,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        self._request_interval = (
            float(os.getenv("NOTION_REQUEST_INTERVAL_SECONDS", "0.34"))
            if request_interval is None
            else request_interval
        )
        self._next_request_at = 0.0
        self._rate_lock = asyncio.Lock()

    async def __aenter__(self) -> "NotionApiClient":
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _wait_for_slot(self) -> None:
        async with self._rate_lock:
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request_at = time.monotonic() + self._request_interval

    async def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        for attempt in range(MAX_RETRIES):
            await self._wait_for_slot()
            try:
                response = await self._client.request(
                    method,
                    f"{NOTION_API_BASE}{path}",
                    headers=self._headers,
                    **kwargs,
                )
            except httpx.TransportError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise NotionApiError("Notion API network request failed") from exc
                await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401:
                raise NotionUnauthorized("Notion access token is expired or revoked")
            if response.status_code in TRANSIENT_STATUSES and attempt < MAX_RETRIES - 1:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                logger.warning(
                    "Notion API returned %s; retrying in %.1fs", response.status_code, delay
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                try:
                    message = response.json().get("message")
                except (ValueError, AttributeError):
                    message = None
                raise NotionApiError(
                    f"Notion API request failed ({response.status_code})"
                    + (f": {message}" if message else "")
                )
            data = response.json()
            if not isinstance(data, dict):
                raise NotionApiError("Notion API returned an invalid JSON object")
            return data
        raise NotionApiError("Notion API retry budget exhausted")

    async def paginate(self, method: str, path: str, **kwargs) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request_kwargs = dict(kwargs)
            if method.upper() == "POST":
                payload = dict(request_kwargs.pop("json", {}) or {})
                payload["page_size"] = 100
                if cursor:
                    payload["start_cursor"] = cursor
                request_kwargs["json"] = payload
            else:
                params = dict(request_kwargs.pop("params", {}) or {})
                params["page_size"] = 100
                if cursor:
                    params["start_cursor"] = cursor
                request_kwargs["params"] = params

            response = await self.request(method, path, **request_kwargs)
            results.extend(item for item in response.get("results", []) if isinstance(item, dict))
            cursor = response.get("next_cursor")
            if not response.get("has_more") or not cursor:
                return results

    async def fetch_pages(
        self,
        on_progress: Callable[[int, str], None] | None = None,
        *,
        include_child_pages: bool = True,
    ) -> list[NotionPage]:
        raw_pages = await self.paginate(
            "POST",
            "/search",
            json={
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "ascending", "timestamp": "last_edited_time"},
            },
        )
        queue: list[dict[str, Any]] = []
        queued: set[str] = set()
        for page in raw_pages:
            self._offer_page(page, queue, queued)

        pages: list[NotionPage] = []
        seen: set[str] = set()
        index = 0
        while index < len(queue):
            page = queue[index]
            index += 1
            page_id = str(page.get("id") or "")
            if not page_id or page_id in seen:
                continue
            seen.add(page_id)
            content, child_page_ids, child_database_ids = await self.render_children(page_id)
            pages.append(_to_notion_page(page, content))
            if on_progress is not None:
                on_progress(len(pages), pages[-1].title)
            if not include_child_pages:
                continue
            for child_id in child_page_ids:
                await self._enqueue_child_page(child_id, queue, queued)
            for database_id in child_database_ids:
                for db_page in await self._fetch_database_pages(database_id):
                    self._offer_page(db_page, queue, queued)
        return pages

    def _offer_page(
        self,
        page: dict[str, Any],
        queue: list[dict[str, Any]],
        queued: set[str],
    ) -> None:
        if page.get("archived") or page.get("in_trash"):
            return
        page_id = str(page.get("id") or "")
        if not page_id or page_id in queued:
            return
        queued.add(page_id)
        queue.append(page)

    async def _enqueue_child_page(
        self,
        page_id: str,
        queue: list[dict[str, Any]],
        queued: set[str],
    ) -> None:
        if page_id in queued:
            return
        queued.add(page_id)
        fetched = await self._get_optional_page(page_id)
        if fetched is not None:
            queue.append(fetched)

    async def _get_optional_page(self, page_id: str) -> dict[str, Any] | None:
        try:
            data = await self.request("GET", f"/pages/{page_id}")
        except NotionApiError as exc:
            if _is_inaccessible(exc):
                logger.info(
                    "Skipping Notion page %s — not shared with this integration", page_id
                )
                return None
            raise
        if data.get("archived") or data.get("in_trash"):
            return None
        return data

    async def _fetch_database_pages(self, database_id: str) -> list[dict[str, Any]]:
        try:
            return await self.paginate("POST", f"/databases/{database_id}/query", json={})
        except NotionApiError as exc:
            if _is_inaccessible(exc):
                logger.info(
                    "Skipping Notion database %s — not shared with this integration",
                    database_id,
                )
                return []
            raise

    async def render_children(
        self, block_id: str, depth: int = 0
    ) -> tuple[str, list[str], list[str]]:
        if depth > 20:
            return "", [], []
        blocks = await self.paginate("GET", f"/blocks/{block_id}/children")
        lines: list[str] = []
        child_page_ids: list[str] = []
        child_database_ids: list[str] = []
        for block in blocks:
            rendered = _render_block(block)
            if rendered:
                lines.append(rendered)
            block_type = str(block.get("type") or "")
            nested_id = str(block.get("id") or "")
            # Child pages and databases are ingested as their own documents.
            # Recursing into them here would duplicate their complete content
            # inside the parent page episode.
            if block_type == "child_page" and nested_id:
                child_page_ids.append(nested_id)
                continue
            if block_type == "child_database" and nested_id:
                child_database_ids.append(nested_id)
                continue
            if block.get("has_children") and nested_id:
                nested, nested_pages, nested_dbs = await self.render_children(
                    nested_id, depth + 1
                )
                if nested:
                    lines.append(nested)
                child_page_ids.extend(nested_pages)
                child_database_ids.extend(nested_dbs)
        return "\n".join(lines).strip(), child_page_ids, child_database_ids


def _is_inaccessible(exc: NotionApiError) -> bool:
    text = str(exc)
    return "(403)" in text or "(404)" in text


def _to_notion_page(page: dict[str, Any], content: str) -> NotionPage:
    parent = page.get("parent") or {}
    return NotionPage(
        page_id=str(page.get("id") or ""),
        title=_page_title(page) or "Untitled",
        url=str(page.get("url") or ""),
        content=content,
        last_edited_time=str(page.get("last_edited_time") or ""),
        parent_page_id=(
            str(parent.get("page_id")) if parent.get("type") == "page_id" else None
        ),
    )


def _page_title(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text(prop.get("title"))
    return ""


def _render_block(block: dict[str, Any]) -> str:
    block_type = str(block.get("type") or "")
    payload = block.get(block_type) or {}
    if not isinstance(payload, dict):
        return ""
    text = _rich_text(payload.get("rich_text"))

    if block_type in HEADING_PREFIX:
        return f"{HEADING_PREFIX[block_type]}{text}" if text else ""
    if block_type == "bulleted_list_item":
        return f"- {text}" if text else ""
    if block_type == "numbered_list_item":
        return f"1. {text}" if text else ""
    if block_type == "to_do":
        return f"- [{'x' if payload.get('checked') else ' '}] {text}" if text else ""
    if block_type == "code":
        return f"```{payload.get('language') or ''}\n{text}\n```" if text else ""
    if block_type == "quote":
        return f"> {text}" if text else ""
    if block_type == "divider":
        return "---"
    if block_type == "child_page":
        return f"## {payload.get('title')}" if payload.get("title") else ""
    if block_type == "table_row":
        cells = [_rich_text(cell) for cell in payload.get("cells", [])]
        return "| " + " | ".join(cells) + " |" if cells else ""
    if block_type in {"bookmark", "embed", "link_preview"}:
        url = str(payload.get("url") or "")
        caption = _rich_text(payload.get("caption")) or url
        return f"[{caption}]({url})" if url else caption
    if block_type in {"image", "video", "file", "pdf"}:
        caption = _rich_text(payload.get("caption"))
        file_data = payload.get(payload.get("type")) or {}
        url = str(file_data.get("url") or "") if isinstance(file_data, dict) else ""
        return f"{caption or block_type}: {url}" if url else caption

    return text


def _rich_text(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("plain_text") or "") for item in value if isinstance(item, dict)
    )
