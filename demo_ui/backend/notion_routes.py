"""Notion OAuth and unified-graph sync routes."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger
from connectors.notion.api import NotionApiClient
from connectors.notion.oauth import (
    NotionConfigurationError, NotionOAuthClient, NotionOAuthError,
    NotionOAuthSettings, NotionStore, notion_state_db_path,
)
from graph import jira_pipeline as common_pipeline
from graph import notion_pipeline as np
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from graph.semantic_pass import run_semantic_pass
from util.paths import DATA_DIR
from demo_ui.backend.job_worker import JOB_STORE

router = APIRouter(prefix="/api/connectors/notion", tags=["notion-connector"])
SESSION_COOKIE = "neuron_notion_session"
LEDGER_PATH = DATA_DIR / "connector_ledger.sqlite3"
logger = logging.getLogger("uvicorn.error.notion_connector")


class NotionSyncRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)


def _components() -> tuple[NotionOAuthSettings, NotionStore]:
    try:
        settings = NotionOAuthSettings.from_env()
        return settings, NotionStore(notion_state_db_path(), settings.encryption_key)
    except NotionConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _session(request: Request, response: Response | None = None, create: bool = False) -> str:
    value = request.cookies.get(SESSION_COOKIE)
    if not value and create:
        value = secrets.token_urlsafe(32)
        if response is not None:
            response.set_cookie(SESSION_COOKIE, value, max_age=30 * 86400, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https", path="/")
    if not value:
        raise HTTPException(status_code=401, detail="Connect Notion from this browser first")
    return value


def _authorized(request: Request, store: NotionStore, workspace_id: str) -> None:
    session_hash = store.session_hash(_session(request))
    if not store.session_has_connection(session_hash, workspace_id):
        raise HTTPException(status_code=404, detail="Notion workspace not found")


@router.get("/status")
async def notion_status(request: Request, response: Response) -> dict:
    _, store = _components()
    session_hash = store.session_hash(_session(request, response, True))
    return {"configured": True, "connections": store.list_connections(session_hash),
            "runs": store.list_sync_runs(session_hash)}


@router.post("/oauth/start")
async def notion_oauth_start(request: Request, response: Response) -> dict:
    settings, store = _components()
    state_value = store.create_oauth_state(_session(request, response, True))
    return {"authorization_url": NotionOAuthClient(settings).authorization_url(state_value)}


@router.get("/oauth/callback", response_class=HTMLResponse, response_model=None)
async def notion_oauth_callback(
    state: str = Query(min_length=20, max_length=500),
    code: str | None = Query(default=None, min_length=1, max_length=2_000),
    error: str | None = Query(default=None, max_length=500),
) -> Response:
    settings, store = _components()
    session_hash = store.consume_oauth_state(state)
    if not session_hash:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    if error or not code:
        raise HTTPException(status_code=400, detail=f"Notion authorization failed: {error or 'missing code'}")
    try:
        payload = await NotionOAuthClient(settings).exchange_code(code)
        connection = store.save_connection(payload)
        store.link_session_connection(session_hash, connection.workspace_id)
    except NotionOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if settings.success_redirect:
        separator = "&" if "?" in settings.success_redirect else "?"
        return RedirectResponse(f"{settings.success_redirect}{separator}notion=connected", status_code=302)
    return HTMLResponse(
        "<!doctype html><title>Notion connected</title><h1>Notion connected</h1>"
        f"<p>{html.escape(connection.workspace_name)} is ready.</p>"
        "<script>if(window.opener){window.opener.postMessage({type:'notion-oauth-complete'},'*');window.close()}</script>"
    )


async def _run_sync(
    run_id: str, payload: NotionSyncRequest,
    settings: NotionOAuthSettings, store: NotionStore,
) -> None:
    try:
        connection = store.get_connection(payload.workspace_id)
        base = {
            "workspace_id": payload.workspace_id, "workspace_name": connection.workspace_name,
            "records_done": 0, "records_total": 0, "records_written": 0,
            "records_kept": 0, "pages_fetched": 0, "chunks_ingested": 0,
            "chunks_total": 0, "entities_written": 0, "facts_written": 0,
        }
        store.set_sync_run(run_id, "running", {
            **base, "phase": "fetching", "current": "Discovering shared Notion pages…",
        })

        def fetch_progress(count: int, title: str) -> None:
            store.set_sync_run(run_id, "running", {
                **base, "phase": "fetching", "current": f"Fetched {title}",
                "pages_fetched": count,
            })

        async with NotionApiClient(connection.access_token) as client:
            pages = await client.fetch_pages(on_progress=fetch_progress)

        total = len(pages)
        base.update({"pages_fetched": total, "records_total": total})
        store.set_sync_run(run_id, "running", {
            **base, "phase": "ingesting", "current": f"Writing {total} Notion pages…",
        })
        graph = get_graph()
        bootstrap_schema(graph)
        ledger = ConnectorLedger(LEDGER_PATH)
        np.write_workspace(graph, ledger, payload.workspace_id, connection.workspace_name)
        kept = written = 0
        for index, page in enumerate(pages, 1):
            action = np.write_page(graph, ledger, page, payload.workspace_id, connection.workspace_name)
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
            store.set_sync_run(run_id, "running", {
                **base, "phase": "ingesting", "current": page.title,
                "records_done": index, "records_kept": kept, "records_written": written,
            })

        # Notion documents that disappear from /search are retained because
        # Notion explicitly documents that search is not exhaustive.
        missing_retained = store.reconcile_pages(payload.workspace_id, [
            {"page_id": page.page_id, "title": page.title, "url": page.url,
             "last_edited_time": page.last_edited_time}
            for page in pages
        ])

        def semantic_progress(done_chunks: int, total_chunks: int, record_key: str, current) -> None:
            store.set_sync_run(run_id, "running", {
                **base, "phase": "semantic", "current": f"Understanding {record_key.rsplit(':', 1)[-1]}…",
                "records_done": total, "records_kept": kept, "records_written": written,
                "chunks_ingested": done_chunks, "chunks_total": total_chunks,
                "entities_written": current.entities_written, "facts_written": current.facts_written,
                **current.token_usage.as_dict("ingestion"),
            })

        # See bitbucket_routes.py's identical wrap: this call blocks for
        # minutes and would otherwise freeze the whole server's event loop.
        semantic = await run_in_threadpool(
            run_semantic_pass, graph, ledger,
            record_prefix=f"notion:{payload.workspace_id}:",
            on_progress=semantic_progress,
        )
        orphans_removed = common_pipeline.delete_orphaned_shared_entities(graph)
        result = {
            **base, "phase": "done", "current": f"Finished {total} Notion pages",
            "records_done": total, "records_kept": kept, "records_written": written,
            "missing_pages_retained": missing_retained,
            "chunks_ingested": semantic.chunks_processed,
            "entities_written": semantic.entities_written, "facts_written": semantic.facts_written,
            "orphans_removed": orphans_removed,
            **semantic.token_usage.as_dict("ingestion"),
        }
        store.finish_connection_sync(payload.workspace_id)
        store.set_sync_run(run_id, "completed", result)
        logger.info("Notion sync completed run=%s %s", run_id, result)
    except asyncio.CancelledError:
        store.set_sync_run(run_id, "failed", error="Sync worker stopped before completion")
        raise
    except Exception as exc:
        logger.exception("Notion sync failed run=%s", run_id)
        store.finish_connection_sync(payload.workspace_id, str(exc)[:1_000])
        store.set_sync_run(run_id, "failed", error=str(exc)[:1_000])
        raise


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(payload: NotionSyncRequest, request: Request) -> dict:
    settings, store = _components()
    _authorized(request, store, payload.workspace_id)
    try:
        store.get_connection(payload.workspace_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run_id = uuid4().hex
    try:
        store.create_sync_run(run_id, payload.workspace_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    JOB_STORE.enqueue(run_id, "notion", {"request": payload.model_dump()})
    return {"run_id": run_id, "status": "queued"}


@router.get("/sync/{run_id}")
async def sync_status(run_id: str, request: Request) -> dict:
    _, store = _components()
    run = store.get_sync_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Notion sync run not found")
    _authorized(request, store, str(run["workspace_id"]))
    return run


@router.delete("/connections/{workspace_id}")
async def delete_connection(workspace_id: str, request: Request) -> dict:
    _, store = _components()
    _authorized(request, store, workspace_id)
    graph = get_graph()
    ledger = ConnectorLedger(LEDGER_PATH)
    record_keys = ledger.record_keys_with_prefix(f"notion:{workspace_id}:")
    for record_key in record_keys:
        common_pipeline.delete_record(graph, ledger, record_key)
    orphans_removed = common_pipeline.delete_orphaned_shared_entities(graph)
    store.delete_connection(workspace_id)
    return {"deleted": True, "records_removed": len(record_keys),
            "orphans_removed": orphans_removed}
