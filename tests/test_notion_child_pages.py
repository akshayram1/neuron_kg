import asyncio
import json

import httpx

from connectors.notion.api import NotionApiClient


def _page(page_id: str, title: str, parent_id: str | None = None) -> dict:
    parent = {"type": "page_id", "page_id": parent_id} if parent_id else {"type": "workspace"}
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "last_edited_time": "2026-09-01T00:00:00.000Z",
        "parent": parent,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
    }


def _blocks(*blocks: dict) -> dict:
    return {"results": list(blocks), "has_more": False, "next_cursor": None}


def _search(*pages: dict) -> dict:
    return {"results": list(pages), "has_more": False, "next_cursor": None}


def _child_page(page_id: str, title: str) -> dict:
    return {"id": page_id, "type": "child_page", "has_children": True, "child_page": {"title": title}}


def _paragraph(text: str) -> dict:
    return {
        "id": f"block-{text}",
        "type": "paragraph",
        "has_children": False,
        "paragraph": {"rich_text": [{"plain_text": text}]},
    }


def _fetch(handler, **kwargs):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = NotionApiClient("token", client=http, request_interval=0)
            return await client.fetch_pages(**kwargs)

    return asyncio.run(run())


def test_search_only_parent_still_ingests_child_and_grandchild():
    parent = _page("parent", "All things Nilus")
    child = _page("child", "Playbooks", "parent")
    grandchild = _page("grand", "Onboarding", "child")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent))
        if path == "/v1/blocks/parent/children":
            return httpx.Response(200, json=_blocks(_paragraph("hub"), _child_page("child", "Playbooks")))
        if path == "/v1/pages/child":
            return httpx.Response(200, json=child)
        if path == "/v1/blocks/child/children":
            return httpx.Response(200, json=_blocks(_child_page("grand", "Onboarding")))
        if path == "/v1/pages/grand":
            return httpx.Response(200, json=grandchild)
        if path == "/v1/blocks/grand/children":
            return httpx.Response(200, json=_blocks(_paragraph("welcome")))
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler)
    assert [page.page_id for page in pages] == ["parent", "child", "grand"]
    assert pages[0].content == "hub\n## Playbooks"
    assert pages[1].parent_page_id == "parent"
    assert pages[2].parent_page_id == "child"
    assert pages[2].content == "welcome"


def test_child_already_in_search_is_not_duplicated():
    parent = _page("parent", "Hub")
    child = _page("child", "Playbooks", "parent")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent, child))
        if path.endswith("/children"):
            block_id = path.split("/")[3]
            if block_id == "parent":
                return httpx.Response(200, json=_blocks(_child_page("child", "Playbooks")))
            return httpx.Response(200, json=_blocks())
        if path == "/v1/pages/child":
            raise AssertionError("child was already queued from search")
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler)
    assert [page.page_id for page in pages] == ["parent", "child"]


def test_inaccessible_child_is_skipped():
    parent = _page("parent", "Hub")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent))
        if path == "/v1/blocks/parent/children":
            return httpx.Response(200, json=_blocks(_child_page("secret", "Private")))
        if path == "/v1/pages/secret":
            return httpx.Response(404, json={"message": "Could not find page"})
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler)
    assert [page.page_id for page in pages] == ["parent"]


def test_include_child_pages_false_keeps_search_hits_only():
    parent = _page("parent", "Hub")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent))
        if path == "/v1/blocks/parent/children":
            return httpx.Response(200, json=_blocks(_child_page("child", "Playbooks")))
        if path.startswith("/v1/pages/"):
            raise AssertionError("child walk is disabled")
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler, include_child_pages=False)
    assert [page.page_id for page in pages] == ["parent"]


def test_child_database_rows_are_ingested():
    parent = _page("parent", "Hub")
    row = _page("row-1", "Sprint notes", "parent")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent))
        if path == "/v1/blocks/parent/children":
            return httpx.Response(
                200,
                json=_blocks(
                    {
                        "id": "db-1",
                        "type": "child_database",
                        "has_children": True,
                        "child_database": {"title": "Notes"},
                    }
                ),
            )
        if path == "/v1/databases/db-1/query":
            body = json.loads(request.content or b"{}")
            assert body.get("page_size") == 100
            return httpx.Response(200, json={"results": [row], "has_more": False})
        if path == "/v1/blocks/row-1/children":
            return httpx.Response(200, json=_blocks(_paragraph("ship it")))
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler)
    assert [page.page_id for page in pages] == ["parent", "row-1"]
    assert pages[1].title == "Sprint notes"
    assert pages[1].content == "ship it"


def test_child_page_nested_inside_toggle_is_found():
    parent = _page("parent", "Hub")
    child = _page("child", "Hidden", "parent")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/search":
            return httpx.Response(200, json=_search(parent))
        if path == "/v1/blocks/parent/children":
            return httpx.Response(
                200,
                json=_blocks(
                    {
                        "id": "toggle-1",
                        "type": "toggle",
                        "has_children": True,
                        "toggle": {"rich_text": [{"plain_text": "More"}]},
                    }
                ),
            )
        if path == "/v1/blocks/toggle-1/children":
            return httpx.Response(200, json=_blocks(_child_page("child", "Hidden")))
        if path == "/v1/pages/child":
            return httpx.Response(200, json=child)
        if path == "/v1/blocks/child/children":
            return httpx.Response(200, json=_blocks(_paragraph("nested")))
        raise AssertionError(f"unexpected {request.method} {path}")

    pages = _fetch(handler)
    assert [page.page_id for page in pages] == ["parent", "child"]
    assert "More" in pages[0].content
