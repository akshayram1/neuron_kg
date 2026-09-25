"""Jira connector routes (plan.md §6a.1). OAuth flow, session handling, and
sites/projects listing are ported as-is from
`graphiti_context_explorer/demo_ui/backend/jira_routes.py` — none of that
touched Graphiti. What's rewritten:

  - sync orchestration (`_run`): calls `graph.jira_pipeline` (Pass A only).
    LLM extraction is Notion-only.
  - `delete_connection`: purges by (provider, connection_id) directly against
    the unified graph instead of deleting a per-source FalkorDB group —
    plan.md §7 dropped `graph/bridge/resolver.py` and its group-based purge
    along with it.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
from uuid import uuid4

from openai import OpenAI
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from connectors.core.ledger import ConnectorLedger
from connectors.core.oauth_store import OAuthConnectorStore, OAuthStoreError
from connectors.jira.api import JiraApiClient, JiraApiError, JiraSite, JiraUnauthorized
from connectors.jira.oauth import JiraConfigurationError, JiraOAuthError, JiraOAuthSettings
from graph import jira_pipeline as jp
from graph import multigraph
from graph.embed_batch import close_batch, open_batch
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from graph import vector_store as vector_store_module
from graph.token_usage import TokenUsage
from util.paths import DATA_DIR
from demo_ui.backend.job_worker import JOB_STORE

router = APIRouter(prefix="/api/connectors/jira", tags=["jira-connector"])
SESSION_COOKIE = "neuron_jira_session"
LEDGER_PATH = DATA_DIR / "connector_ledger.sqlite3"
OAUTH_STORE_PATH = DATA_DIR / os.getenv("OAUTH_CONNECTOR_STATE_DB", "oauth_connectors.sqlite3")
logger = logging.getLogger("uvicorn.error.jira_connector")


class JiraSyncRequest(BaseModel):
    connection_id: str = Field(min_length=1, max_length=100)
    cloud_id: str = Field(min_length=1, max_length=200)
    site_name: str = Field(min_length=1, max_length=300)
    site_url: str = Field(default="", max_length=1000)
    project_id: str = Field(min_length=1, max_length=100)
    project_key: str = Field(min_length=1, max_length=100)
    project_name: str = Field(min_length=1, max_length=300)
    # Optional: ingest only this issue's subtree (epic/task/sub-task
    # descendants) instead of the whole project. Empty = whole project.
    scope_issue_key: str = Field(default="", max_length=100)
    graph_name: str = Field(default=multigraph.DEFAULT_GRAPH_NAME, max_length=40)


def _components() -> tuple[JiraOAuthSettings, OAuthConnectorStore]:
    try:
        settings = JiraOAuthSettings.from_env()
        store = OAuthConnectorStore(OAUTH_STORE_PATH, "jira", settings.encryption_key)
        return settings, store
    except (JiraConfigurationError, OAuthStoreError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _session(request: Request, response: Response | None = None, create: bool = False) -> str:
    value = request.cookies.get(SESSION_COOKIE)
    if not value and create:
        value = secrets.token_urlsafe(32)
        if response is not None:
            response.set_cookie(SESSION_COOKIE, value, max_age=30 * 86400, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https", path="/")
    if not value:
        raise HTTPException(status_code=401, detail="Connect Jira from this browser first")
    return value


def _authorized(request: Request, store: OAuthConnectorStore, connection_id: str) -> None:
    if not store.session_has_connection(store.session_hash(_session(request)), connection_id):
        raise HTTPException(status_code=404, detail="Jira connection not found")


async def _with_refresh(store, settings, connection_id, operation):
    connection = store.get_connection(connection_id)
    try:
        async with JiraApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)
    except JiraUnauthorized:
        refresh_token = connection["token"].get("refresh_token")
        if not refresh_token:
            raise
        token = await settings.refresh(str(refresh_token))
        connection = store.update_token(connection_id, token)
        async with JiraApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)


@router.get("/status")
async def jira_status(
    request: Request, response: Response,
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    _, store = _components()
    session_hash = store.session_hash(_session(request, response, True))
    return {"configured": True, "connections": store.list_connections(session_hash),
            "sources": store.list_sources(session_hash),
            "runs": store.list_runs(session_hash, graph_name=graph_name)}


@router.post("/oauth/start")
async def jira_oauth_start(request: Request, response: Response) -> dict:
    settings, store = _components()
    state = store.create_state(_session(request, response, True))
    return {"authorization_url": settings.authorization_url(state)}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def jira_oauth_callback(
    state: str = Query(min_length=20, max_length=500),
    code: str | None = Query(default=None, min_length=1, max_length=4000),
    error: str | None = Query(default=None, max_length=500),
) -> HTMLResponse:
    settings, store = _components()
    session_hash = store.consume_state(state)
    if not session_hash:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    if error or not code:
        raise HTTPException(status_code=400, detail=f"Jira authorization failed: {error or 'missing code'}")
    try:
        token = await settings.exchange_code(code)
        async with JiraApiClient(token["access_token"]) as client:
            sites = await client.sites()
        connection_id = uuid4().hex
        account_name = ", ".join(site.name for site in sites[:3]) or "Atlassian account"
        store.save_connection(connection_id=connection_id, account_id=connection_id,
                              account_name=account_name, token=token)
        store.link_session(session_hash, connection_id)
    except (JiraOAuthError, JiraApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return HTMLResponse(
        "<!doctype html><title>Jira connected</title><h1>Jira connected</h1>"
        f"<p>{html.escape(account_name)} is ready.</p>"
        "<script>if(window.opener){window.opener.postMessage({type:'jira-oauth-complete'},'*');window.close()}</script>"
    )


@router.get("/sites")
async def list_sites(request: Request, connection_id: str = Query(min_length=1)) -> dict:
    settings, store = _components(); _authorized(request, store, connection_id)
    sites = await _with_refresh(store, settings, connection_id, lambda c: c.sites())
    return {"sites": [site.__dict__ for site in sites]}


@router.get("/projects")
async def list_projects(request: Request, connection_id: str, cloud_id: str) -> dict:
    settings, store = _components(); _authorized(request, store, connection_id)
    projects = await _with_refresh(store, settings, connection_id, lambda c: c.projects(cloud_id))
    return {"projects": [project.__dict__ for project in projects]}


async def _run(run_id: str, payload: JiraSyncRequest, settings: JiraOAuthSettings, store: OAuthConnectorStore) -> None:
    try:
        store.set_run(run_id, "running", {
            "phase": "fetching", "project_key": payload.project_key,
            "current": f"Connecting to Jira for {payload.project_key}…",
            "records_done": 0, "records_total": 0, "records_kept": 0, "records_written": 0,
            "issues_fetched": 0, "chunks_ingested": 0, "chunks_total": 0,
            "entities_written": 0, "facts_written": 0,
        })
        site = JiraSite(payload.cloud_id, payload.site_name, payload.site_url)
        projects = await _with_refresh(store, settings, payload.connection_id, lambda c: c.projects(payload.cloud_id))
        project = next((item for item in projects if item.project_id == payload.project_id), None)
        if not project:
            raise JiraApiError("Selected Jira project is no longer accessible")

        target = multigraph.resolve(
            payload.graph_name, data_dir=DATA_DIR,
            base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
            base_collection=vector_store_module.COLLECTION,
        )
        graph = get_graph(name=target.falkor_name)
        bootstrap_schema(graph)
        vector_store_module.ensure_collection(vector_store_module.client(), collection=target.qdrant_collection)
        ledger = ConnectorLedger(target.ledger_path)

        scope = payload.scope_issue_key.strip().upper()
        fetch_label = f"{scope} subtree" if scope else payload.project_key
        store.set_run(run_id, "running", {
            "phase": "fetching", "project_key": payload.project_key,
            "current": f"Fetching issues from {fetch_label}…",
            "records_done": 0, "records_total": 0, "records_kept": 0, "records_written": 0,
            "issues_fetched": 0, "entities_written": 0, "facts_written": 0,
        })
        def fetch_progress(issue_count: int) -> None:
            store.set_run(run_id, "running", {
                "phase": "fetching", "project_key": payload.project_key,
                "current": f"Fetched {issue_count} issues from Jira…",
                "records_done": 0, "records_total": 0,
                "records_kept": 0, "records_written": 0,
                "issues_fetched": issue_count, "chunks_ingested": 0, "chunks_total": 0,
                "entities_written": 0, "facts_written": 0,
            })

        if scope:
            keys = await _with_refresh(
                store, settings, payload.connection_id,
                lambda c: c.subtree_keys(payload.cloud_id, scope),
            )
            issues = await _with_refresh(
                store, settings, payload.connection_id,
                lambda c: c.issues_by_keys(payload.cloud_id, keys, on_progress=fetch_progress),
            )
        else:
            issues = await _with_refresh(
                store,
                settings,
                payload.connection_id,
                lambda c: c.issues(payload.cloud_id, project.key, on_progress=fetch_progress),
            )

                # One request per batch instead of one per record -- see
        # graph/embed_batch.py. Must be closed on every exit path below.
        open_batch(OpenAI(timeout=30.0, max_retries=2),
                   os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                   target.qdrant_collection)
        jp.write_project(graph, ledger, project, site, payload.connection_id)
        total = len(issues)
        kept = written = 0
        for index, issue in enumerate(issues, 1):
            action = jp.write_issue(
                graph, ledger, issue, project, site, payload.connection_id,
                collection=target.qdrant_collection,
            )
            if str(action) == "keep":
                kept += 1
            else:
                written += 1
            store.set_run(run_id, "running", {
                "phase": "ingesting", "project_key": payload.project_key, "current": issue.key,
                "records_done": index, "records_total": total,
                "records_kept": kept, "records_written": written,
                "issues_fetched": total, "entities_written": 0, "facts_written": 0,
            })

        orphans_removed = jp.delete_orphaned_shared_entities(graph)

        # Sync coverage (plan.md Phase 0.4): re-read the ledger for the exact
        # issues this run just tried to write, rather than trusting
        # kept+written, so a silent partial commit would show up here.
        # `provider_reported_total` is left None -- Jira's `/search/jql`
        # endpoint (see connectors/jira/api.py::_search_raw) is token-paginated
        # and does not return a total count, unlike the deprecated offset
        # `/search` endpoint, so there is nothing honest to report here.
        record_keys = [
            jp.issue_record(issue, project, site, payload.connection_id).record_key
            for issue in issues
        ]
        sync_ledger_count = ledger.count_present(record_keys)
        ledger.record_sync_coverage(
            run_id, "jira", connection_id=payload.connection_id,
            provider_reported_total=None,
            fetched_count=total, ledger_count=sync_ledger_count,
            skipped_by_rule_count=0,
        )

        result = {
            "phase": "done", "project_key": payload.project_key,
            "current": f"Finished {total} issues from {project.key}",
            "records_done": total, "records_total": total, "records_kept": kept, "records_written": written,
            "issues_fetched": total, "entities_written": 0,
            "facts_written": 0, "chunks_ingested": 0,
            "orphans_removed": orphans_removed,
            "provider_reported_total": None,
            "fetched_count": total, "ledger_count": sync_ledger_count, "skipped_by_rule_count": 0,
            **TokenUsage().as_dict("ingestion"),
        }
        source_id = f"{payload.cloud_id}:{payload.project_id}"
        store.save_source(
            payload.connection_id,
            source_id,
            f"{project.key} · {project.name}",
            "jira",
            {
                "cloud_id": payload.cloud_id,
                "site_name": payload.site_name,
                "site_url": payload.site_url,
                "project_id": payload.project_id,
                "project_key": project.key,
                "project_name": project.name,
            },
        )
        close_batch()
        store.set_run(run_id, "completed", result)
        logger.info("Jira sync completed run=%s %s", run_id, result)
    except Exception as exc:
        logger.exception("Jira sync failed run=%s", run_id)
        close_batch()
        store.set_run(run_id, "failed", error=str(exc)[:1000])
        raise


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(payload: JiraSyncRequest, request: Request) -> dict:
    settings, store = _components(); _authorized(request, store, payload.connection_id)
    source_id = f"{payload.cloud_id}:{payload.project_id}"
    run_id = uuid4().hex
    try:
        store.create_run(run_id, payload.connection_id, source_id, graph_name=payload.graph_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    JOB_STORE.enqueue(run_id, "jira", {"request": payload.model_dump()})
    return {"run_id": run_id, "status": "queued"}


@router.get("/sync/{run_id}")
async def sync_status(run_id: str, request: Request) -> dict:
    _, store = _components(); run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Jira sync run not found")
    _authorized(request, store, run["connection_id"])
    return run


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, request: Request) -> dict:
    """Disconnect this Jira account: invalidate every fact its records
    support, mark its SourceRecords deleted, and remove the OAuth connection
    and ledger rows. Unlike the source project, there is no separate FalkorDB
    group to drop — everything lives in the one unified graph, scoped by
    (provider, connection_id) instead (plan.md §7)."""
    _, store = _components()
    _authorized(request, store, connection_id)

    graph = get_graph()
    ledger = ConnectorLedger(LEDGER_PATH)
    record_keys = ledger.record_keys_with_prefix(f"jira:{connection_id}:")
    for record_key in record_keys:
        jp.delete_record(graph, ledger, record_key)
    orphans_removed = jp.delete_orphaned_shared_entities(graph)

    store.delete_connection(connection_id)
    logger.info(
        "Jira connection deleted connection_id=%s records_removed=%s orphans_removed=%s",
        connection_id, len(record_keys), orphans_removed,
    )
    return {"deleted": True, "records_removed": len(record_keys), "orphans_removed": orphans_removed}
