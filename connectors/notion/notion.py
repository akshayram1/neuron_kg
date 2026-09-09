"""Notion connector — standalone, no cognee/dlt dependency.

Fetches Notion pages and renders their block children to markdown.

    from connectors.notion.notion import build_notion_client, iter_notion_pages

    client = build_notion_client(token="secret_...")
    for page in iter_notion_pages(client):
        ...  # page == {"id", "url", "title", "content"}

Full-snapshot semantics: each call to ``iter_notion_pages`` yields exactly the
pages currently visible to the integration (archived/trashed pages are
skipped). If you need delete-tracking, diff the set of ids you got this run
against the previous run's ids yourself — Notion's API has no delete feed.

Design
------
* Restrict scope with ``page_ids`` or ``database_ids``; omit both to search
  every page the integration can see.
* Rate-limited / transient errors (429, 5xx, timeouts) are retried with
  backoff (Notion enforces ~3 requests/second).
* Only ``title``/``content`` (+ ``id``/``url``) are kept, so a metadata-only
  edit that doesn't change the text won't look like new content downstream.

Adapted from cognee-community (Apache-2.0):
https://github.com/topoteretes/cognee-community/tree/main/packages/connector/notion
"""

from __future__ import annotations

import os
import time
from typing import Any
import logging

logger = logging.getLogger("notion_connector")

# Pin the Notion API version so upstream changes can't silently alter parsing.
_NOTION_VERSION = "2022-06-28"

# Retry budget for rate-limited / transient Notion API responses.
_MAX_RETRIES = 5

_HEADING_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}


def build_notion_client(token: str | None = None) -> Any:
    """Build a ``notion_client.Client``. Falls back to the ``NOTION_API_KEY`` env var.

    Requires: notion-client.
    """
    try:
        from notion_client import Client
    except ImportError as exc:
        raise ImportError("Notion connector requires: pip install notion-client") from exc

    resolved_token = token or os.environ.get("NOTION_API_KEY")
    if not resolved_token:
        raise ValueError("Notion integration token required: pass token= or set NOTION_API_KEY.")
    return Client(auth=resolved_token, notion_version=_NOTION_VERSION)


def iter_notion_pages(
    client: Any,
    page_ids: list[str] | None = None,
    database_ids: list[str] | None = None,
):
    """Yield ``{"id", "url", "title", "content"}`` for every in-scope, live page.

    Args:
        client: A ``notion_client.Client`` (see ``build_notion_client``).
        page_ids: Restrict to these page ids. When omitted (and no
            ``database_ids``), all pages the integration can see are searched.
        database_ids: Restrict to pages in these databases.
    """
    count = 0
    for page in _iter_pages(client, page_ids, database_ids):
        if page.get("archived") or page.get("in_trash"):
            continue
        count += 1
        yield _page_to_row(client, page)
    logger.info("Notion: synced %d page(s).", count)


# ---------------------------------------------------------------------------
# Notion API helpers (module-private)
# ---------------------------------------------------------------------------
def _request(method, **kwargs):
    """Call a Notion API method, retrying rate-limit / transient errors.

    notion-client does not retry or honor ``Retry-After`` itself, and Notion
    enforces ~3 requests/second, so a page with many nested blocks would
    otherwise 429 and abort the sync. Rate-limit (429), server (5xx), timeout,
    and network errors are retried with backoff; permanent errors (auth,
    not-found) and exhausted retries propagate so the caller can decide.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            return method(**kwargs)
        except Exception as exc:
            if attempt == _MAX_RETRIES - 1 or not _is_transient(exc):
                raise
            delay = _retry_after(getattr(exc, "headers", None), attempt)
            logger.warning(
                "Notion: %s — retrying in %.1fs (%d/%d).", exc, delay, attempt + 1, _MAX_RETRIES
            )
            time.sleep(delay)


def _is_transient(exc: Exception) -> bool:
    """True for rate-limit / server / timeout / network errors worth retrying."""
    import httpx
    from notion_client.errors import HTTPResponseError, RequestTimeoutError

    if isinstance(exc, (RequestTimeoutError, httpx.TransportError)):
        return True
    # APIResponseError subclasses HTTPResponseError; both expose .status.
    if isinstance(exc, HTTPResponseError):
        return getattr(exc, "status", None) in (429, 500, 502, 503, 504)
    return False


def _is_gone(exc: Exception) -> bool:
    """True when a page is permanently gone / not shared (skip, don't fail)."""
    from notion_client.errors import APIResponseError

    return isinstance(exc, APIResponseError) and getattr(exc, "status", None) in (403, 404)


def _retry_after(headers, attempt: int) -> float:
    """Seconds to wait before retrying: the Retry-After header, else backoff."""
    header = (headers or {}).get("retry-after") or (headers or {}).get("Retry-After")
    try:
        return float(header)
    except (TypeError, ValueError):
        return float(2**attempt)


def _iter_pages(client, page_ids, database_ids):
    """Yield raw Notion page objects for the configured scope."""
    if page_ids:
        for page_id in page_ids:
            try:
                yield _request(client.pages.retrieve, page_id=page_id)
            except Exception as exc:
                # A permanently-gone page is skipped; a transient error re-raises.
                if _is_gone(exc):
                    logger.warning("Notion: page %s is gone, skipping: %s", page_id, exc)
                    continue
                raise
        return

    if database_ids:
        for database_id in database_ids:
            yield from _paginate(client.databases.query, database_id=database_id)
        return

    # No explicit scope: search every page the integration can see.
    yield from _paginate(client.search, filter={"property": "object", "value": "page"})


def _paginate(method, **kwargs):
    """Yield results across Notion's cursor-based pagination."""
    cursor = None
    while True:
        response = (
            _request(method, start_cursor=cursor, **kwargs)
            if cursor
            else _request(method, **kwargs)
        )
        yield from response.get("results", [])
        cursor = response.get("next_cursor")
        # Stop on the last page, or if Notion signals "more" without a cursor
        # (contract violation) so we can't loop forever.
        if not response.get("has_more") or not cursor:
            return


def _page_to_row(client, page: dict) -> dict:
    """Flatten a Notion page + its block children into a document row."""
    return {
        "id": page.get("id"),
        "url": page.get("url"),
        "title": _page_title(page),
        "content": _render_blocks(client, page.get("id")),
    }


def _page_title(page: dict) -> str:
    """Extract the page title from its title property."""
    properties = page.get("properties") or {}
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return _rich_text(prop.get("title"))
    return ""


def _render_blocks(client, block_id: str | None, depth: int = 0) -> str:
    """Render a block's children to markdown, recursing into nested blocks."""
    # Guard against pathological nesting / cycles.
    if not block_id or depth > 10:
        return ""

    lines: list[str] = []
    for block in _paginate(client.blocks.children.list, block_id=block_id):
        rendered = _render_block(block)
        if rendered:
            lines.append(rendered)
        if block.get("has_children"):
            nested = _render_blocks(client, block.get("id"), depth + 1)
            if nested:
                lines.append(nested)

    return "\n".join(lines)


def _render_block(block: dict) -> str:
    """Render a single Notion block to a markdown line."""
    block_type = block.get("type")
    if not block_type:
        return ""

    payload = block.get(block_type) or {}
    text = _rich_text(payload.get("rich_text"))

    if block_type in _HEADING_PREFIX:
        return f"{_HEADING_PREFIX[block_type]}{text}" if text else ""
    if block_type == "bulleted_list_item":
        return f"- {text}" if text else ""
    if block_type == "numbered_list_item":
        return f"1. {text}" if text else ""
    if block_type == "to_do":
        checked = "x" if payload.get("checked") else " "
        return f"- [{checked}] {text}" if text else ""
    if block_type == "code":
        language = payload.get("language") or ""
        return f"```{language}\n{text}\n```" if text else ""

    # Paragraph, quote, callout, toggle, and any other rich_text block render as
    # their plain text.
    return text


def _rich_text(rich_text: Any) -> str:
    """Concatenate the plain_text of a Notion rich_text array."""
    if not isinstance(rich_text, list):
        return ""
    return "".join(part.get("plain_text", "") for part in rich_text if isinstance(part, dict))
