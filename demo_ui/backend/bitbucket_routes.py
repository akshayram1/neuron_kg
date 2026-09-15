"""Bitbucket Cloud OAuth, workspace/repository picker and unified-graph sync.

Combines Jira's OAuth control plane (`OAuthConnectorStore` — classic per-user
grant, not a GitHub-App installation) with GitHub's two-phase sync execution
(fetch files + commits + PRs, write deterministically). LLM extraction is
Notion-only.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
from dataclasses import replace
from uuid import uuid4

from openai import OpenAI
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator

from connectors.bitbucket.api import (
    BitbucketApiClient, BitbucketApiError, BitbucketRepository, BitbucketUnauthorized,
    SUPPORTED_FILE_TYPES,
)
from connectors.bitbucket.oauth import (
    BitbucketConfigurationError, BitbucketOAuthError, BitbucketOAuthSettings,
)
from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger
from connectors.core.oauth_store import OAuthConnectorStore, OAuthStoreError
from graph import bitbucket_pipeline as bp
from graph import multigraph
from graph.embed_batch import close_batch, open_batch
from graph import vector_store as vector_store_module
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from graph.token_usage import TokenUsage
from util.paths import DATA_DIR
from demo_ui.backend.job_worker import JOB_STORE

router = APIRouter(prefix="/api/connectors/bitbucket", tags=["bitbucket-connector"])
SESSION_COOKIE = "neuron_bitbucket_session"
LEDGER_PATH = DATA_DIR / "connector_ledger.sqlite3"
OAUTH_STORE_PATH = DATA_DIR / os.getenv("OAUTH_CONNECTOR_STATE_DB", "oauth_connectors.sqlite3")
logger = logging.getLogger("uvicorn.error.bitbucket_connector")


class BitbucketSyncRequest(BaseModel):
    connection_id: str = Field(min_length=1, max_length=100)
    workspace: str = Field(min_length=1, max_length=200)
    repository_uuid: str = Field(min_length=1, max_length=200)
    # Empty means "the repository's own default branch".
    branch: str = Field(default="", max_length=250)
    file_types: list[str] = Field(default_factory=list, max_length=2)
    include_commit_messages: bool = True
    include_pull_requests: bool = True
    graph_name: str = Field(default=multigraph.DEFAULT_GRAPH_NAME, max_length=40)

    @model_validator(mode="after")
    def validate_selection(self) -> "BitbucketSyncRequest":
        normalized = list(dict.fromkeys(value.lower() for value in self.file_types))
        if any(value not in SUPPORTED_FILE_TYPES for value in normalized):
            raise ValueError("Supported Bitbucket file types are .py and .md")
        if not normalized and not self.include_commit_messages and not self.include_pull_requests:
            raise ValueError("Select at least one file type, commit messages, or pull requests")
        self.file_types = normalized
        return self


def _components() -> tuple[BitbucketOAuthSettings, OAuthConnectorStore]:
    try:
        settings = BitbucketOAuthSettings.from_env()
        store = OAuthConnectorStore(OAUTH_STORE_PATH, "bitbucket", settings.encryption_key)
        return settings, store
    except (BitbucketConfigurationError, OAuthStoreError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _session(request: Request, response: Response | None = None, create: bool = False) -> str:
    value = request.cookies.get(SESSION_COOKIE)
    if not value and create:
        value = secrets.token_urlsafe(32)
        if response is not None:
            response.set_cookie(SESSION_COOKIE, value, max_age=30 * 86400, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https", path="/")
    if not value:
        raise HTTPException(status_code=401, detail="Connect Bitbucket from this browser first")
    return value


def _authorized(request: Request, store: OAuthConnectorStore, connection_id: str) -> None:
    if not store.session_has_connection(store.session_hash(_session(request)), connection_id):
        raise HTTPException(status_code=404, detail="Bitbucket connection not found")


async def _with_refresh(store: OAuthConnectorStore, settings: BitbucketOAuthSettings, connection_id: str, operation):
    connection = store.get_connection(connection_id)
    try:
        async with BitbucketApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)
    except BitbucketUnauthorized:
        refresh_token = connection["token"].get("refresh_token")
        if not refresh_token:
            raise
        token = await settings.refresh(str(refresh_token))
        connection = store.update_token(connection_id, token)
        async with BitbucketApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)


@router.get("/status")
async def bitbucket_status(
    request: Request, response: Response,
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    _, store = _components()
    session_hash = store.session_hash(_session(request, response, True))
    return {"configured": True, "connections": store.list_connections(session_hash),
            "sources": store.list_sources(session_hash),
            "runs": store.list_runs(session_hash, graph_name=graph_name)}


@router.post("/oauth/start")
async def bitbucket_oauth_start(request: Request, response: Response) -> dict:
    settings, store = _components()
    state = store.create_state(_session(request, response, True))
    return {"authorization_url": settings.authorization_url(state)}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def bitbucket_oauth_callback(
    state: str = Query(min_length=20, max_length=500),
    code: str | None = Query(default=None, min_length=1, max_length=4_000),
    error: str | None = Query(default=None, max_length=500),
) -> HTMLResponse:
    settings, store = _components()
    session_hash = store.consume_state(state)
    if not session_hash:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    if error or not code:
        raise HTTPException(status_code=400, detail=f"Bitbucket authorization failed: {error or 'missing code'}")
    try:
        token = await settings.exchange_code(code)
    except BitbucketOAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Persist the grant BEFORE any API call: the token exchange already
    # succeeded, so the connection is valid. Failing to fetch a nicer display
    # name must not throw away a working OAuth grant (and force the user back
    # through an admin-approved consent screen).
    connection_id = uuid4().hex
    store.save_connection(connection_id=connection_id, account_id=connection_id,
                          account_name="Bitbucket account", token=token)
    store.link_session(session_hash, connection_id)

    # Bitbucket no longer lets a token enumerate its workspaces (see
    # BitbucketApiClient.workspace), so the connection is named after the
    # authenticated account instead; the workspace slug is entered per sync.
    account_name = "Bitbucket account"
    try:
        async with BitbucketApiClient(token["access_token"]) as client:
            profile = await client.user()
        account_name = str(profile.get("display_name") or profile.get("username") or account_name)
        store.save_connection(connection_id=connection_id, account_id=str(
            profile.get("account_id") or connection_id
        ), account_name=account_name, token=token)
    except BitbucketApiError as exc:
        logger.warning("Bitbucket connected but profile lookup failed: %s", exc)
    return HTMLResponse(
        "<!doctype html><title>Bitbucket connected</title><h1>Bitbucket connected</h1>"
        f"<p>{html.escape(account_name)} is ready.</p>"
        "<script>if(window.opener){window.opener.postMessage({type:'bitbucket-oauth-complete'},'*');window.close()}</script>"
    )


@router.get("/workspace")
async def get_workspace(
    request: Request, connection_id: str = Query(min_length=1), workspace: str = Query(min_length=1),
) -> dict:
    """Resolve one workspace slug to its real name.

    Bitbucket removed workspace enumeration from this API (verified live: the
    listing endpoints 404 even with every read scope granted), so the slug is
    typed by the user and validated here instead of picked from a dropdown.
    """
    settings, store = _components()
    _authorized(request, store, connection_id)
    try:
        found = await _with_refresh(store, settings, connection_id, lambda c: c.workspace(workspace))
    except (BitbucketUnauthorized, BitbucketApiError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace}' not found or not accessible to this connection ({exc})",
        ) from exc
    return {"workspace": found.__dict__}


async def _repositories(
    store: OAuthConnectorStore, settings: BitbucketOAuthSettings, connection_id: str, workspace: str,
) -> list[BitbucketRepository]:
    try:
        return await _with_refresh(store, settings, connection_id, lambda c: c.repositories(workspace))
    except (BitbucketUnauthorized, BitbucketApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/repositories")
async def list_repositories(
    request: Request, connection_id: str = Query(min_length=1), workspace: str = Query(min_length=1),
) -> dict:
    settings, store = _components()
    _authorized(request, store, connection_id)
    repositories = await _repositories(store, settings, connection_id, workspace)
    return {"repositories": [repo.__dict__ for repo in repositories]}


@router.get("/branches")
async def list_branches(
    request: Request, connection_id: str = Query(min_length=1),
    workspace: str = Query(min_length=1), repository_uuid: str = Query(min_length=1),
) -> dict:
    """Branches available for one repository, default branch first."""
    settings, store = _components()
    _authorized(request, store, connection_id)
    repositories = await _repositories(store, settings, connection_id, workspace)
    repository = next((repo for repo in repositories if repo.uuid == repository_uuid), None)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository is no longer accessible")
    try:
        names = await _with_refresh(
            store, settings, connection_id, lambda c: c.branches(repository)
        )
    except (BitbucketUnauthorized, BitbucketApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"branches": names, "default_branch": repository.main_branch}


async def _run_sync(
    run_id: str, payload: BitbucketSyncRequest, repository: BitbucketRepository,
    settings: BitbucketOAuthSettings, store: OAuthConnectorStore,
) -> None:
    source_id = f"{payload.workspace}:{repository.slug}"
    # A chosen branch is applied by swapping the repository's branch field:
    # `files()`/`commits()` already read every path from `main_branch`, so
    # nothing downstream needs to know a branch was selected.
    if payload.branch and payload.branch != repository.main_branch:
        repository = replace(repository, main_branch=payload.branch)
    try:
        base = {
            "connection_id": payload.connection_id, "repository_full_name": repository.full_name,
            "branch": repository.main_branch,
            "file_types": payload.file_types, "include_commit_messages": payload.include_commit_messages,
            "include_pull_requests": payload.include_pull_requests,
            "records_done": 0, "records_total": 0, "records_written": 0,
            "records_kept": 0, "files_matched": 0, "files_processed": 0,
            "files_too_large": 0, "files_without_text": 0,             "commits_fetched": 0,
            "commit_files_changed": 0,
            "pull_requests_fetched": 0,
            "chunks_ingested": 0, "chunks_total": 0,
            "entities_written": 0, "facts_written": 0,
        }
        store.set_run(run_id, "running", {
            **base, "phase": "fetching", "current": f"Discovering {repository.full_name}…",
        })
        connection = store.get_connection(payload.connection_id)
        async with BitbucketApiClient(connection["token"]["access_token"]) as client:
            async def _commits_with_diffstats() -> list:
                if not payload.include_commit_messages:
                    return []
                found = await client.commits(repository, settings.max_commits_per_sync)
                store.set_run(run_id, "running", {
                    **base, "phase": "fetching",
                    "current": f"Fetching file lists for {len(found)} commits (in parallel with the tree)…",
                    "commits_fetched": len(found),
                })
                return await client.attach_diffstats(repository, found)

            (files, too_large, without_text), commits, pull_requests = await asyncio.gather(
                client.files(repository, set(payload.file_types), settings.max_file_bytes),
                _commits_with_diffstats(),
                client.pull_requests(repository) if payload.include_pull_requests
                else asyncio.sleep(0, result=[]),
            )
            total = len(files) + len(commits) + len(pull_requests)
            files_changed = sum(len(commit.files) for commit in commits)
            base.update({"records_total": total, "files_matched": len(files),
                         "files_too_large": too_large, "files_without_text": without_text,
                         "commits_fetched": len(commits), "pull_requests_fetched": len(pull_requests),
                         "commit_files_changed": files_changed})
            store.set_run(run_id, "running", {
                **base, "phase": "ingesting", "current": f"Writing {total} Bitbucket records…",
            })

        target = multigraph.resolve(
            payload.graph_name, data_dir=DATA_DIR,
            base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
            base_collection=vector_store_module.COLLECTION,
        )
        graph = get_graph(name=target.falkor_name)
        bootstrap_schema(graph)
        vector_store_module.ensure_collection(vector_store_module.client(), collection=target.qdrant_collection)
        ledger = ConnectorLedger(target.ledger_path)
                # One request per batch instead of one per record -- see
        # graph/embed_batch.py. Must be closed on every exit path below.
        open_batch(OpenAI(timeout=30.0, max_retries=2),
                   os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
                   target.qdrant_collection)
        bp.write_repository(graph, ledger, repository, payload.connection_id)
        done = kept = written = 0
        present_file_keys = {
            f"bitbucket:{payload.connection_id}:source_file:{repository.uuid}:{file.path}"
            for file in files
        }
        present_commit_keys = {
            f"bitbucket:{payload.connection_id}:commit:{repository.uuid}:{commit.commit_hash}"
            for commit in commits
        }
        present_pr_keys = {
            f"bitbucket:{payload.connection_id}:pull_request:{repository.uuid}:{pr.id}"
            for pr in pull_requests
        }
        for file in files:
            action = bp.write_file(
                graph, ledger, repository, file, payload.connection_id,
                collection=target.qdrant_collection,
            )
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
            done += 1
            store.set_run(run_id, "running", {
                **base, "phase": "ingesting", "current": file.path,
                "records_done": done, "records_kept": kept, "records_written": written,
                "files_processed": done,
            })
        present_paths = {file.path for file in files}
        for commit in commits:
            action = bp.write_commit(
                graph, ledger, repository, commit, payload.connection_id,
                collection=target.qdrant_collection,
                present_paths=present_paths,
            )
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
            done += 1
            store.set_run(run_id, "running", {
                **base, "phase": "ingesting", "current": commit.message.splitlines()[0][:120],
                "records_done": done, "records_kept": kept, "records_written": written,
                "files_processed": len(files),
            })
        for pr in pull_requests:
            action = bp.write_pull_request(
                graph, ledger, repository, pr, payload.connection_id,
                collection=target.qdrant_collection,
            )
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
            done += 1
            store.set_run(run_id, "running", {
                **base, "phase": "ingesting", "current": f"PR #{pr.id} — {pr.title[:100]}",
                "records_done": done, "records_kept": kept, "records_written": written,
                "files_processed": len(files),
            })

        removed = bp.reconcile_repository_records(
            graph, ledger, payload.connection_id, repository.uuid,
            present_file_keys, present_commit_keys, present_pr_keys,
        )

        orphans_removed = bp.delete_orphaned_shared_entities(graph)
        result = {
            **base, "phase": "done", "current": f"Finished {repository.full_name}",
            "records_done": total, "records_total": total, "records_kept": kept,
            "records_written": written, "files_processed": len(files),
            "records_removed": removed,
            "chunks_ingested": 0,
            "entities_written": 0, "facts_written": 0,
            "orphans_removed": orphans_removed,
            **TokenUsage().as_dict("ingestion"),
        }
        store.save_source(
            payload.connection_id, source_id,
            f"{repository.full_name} @ {repository.main_branch}",
            f"bitbucket_{payload.connection_id}_{repository.slug}",
            # Remember the branch so a later re-sync of this source stays on
            # the branch that was actually ingested, not the repo default.
            {"workspace": payload.workspace, "repository_uuid": repository.uuid,
             "repository_slug": repository.slug, "branch": repository.main_branch,
             "file_types": payload.file_types,
             "include_commit_messages": payload.include_commit_messages,
             "include_pull_requests": payload.include_pull_requests},
        )
        close_batch()
        store.set_run(run_id, "completed", result)
        logger.info("Bitbucket sync completed run=%s %s", run_id, result)
    except asyncio.CancelledError:
        close_batch()
        store.set_run(run_id, "failed", error="Sync worker stopped before completion")
        raise
    except Exception as exc:
        logger.exception("Bitbucket sync failed run=%s", run_id)
        close_batch()
        store.set_run(run_id, "failed", error=str(exc)[:1_000])
        raise


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(payload: BitbucketSyncRequest, request: Request) -> dict:
    settings, store = _components()
    _authorized(request, store, payload.connection_id)
    repositories = await _repositories(store, settings, payload.connection_id, payload.workspace)
    repository = next((repo for repo in repositories if repo.uuid == payload.repository_uuid), None)
    if not repository:
        raise HTTPException(status_code=404, detail="Selected repository is no longer accessible")
    run_id = uuid4().hex
    source_id = f"{payload.workspace}:{repository.slug}"
    try:
        store.create_run(run_id, payload.connection_id, source_id, graph_name=payload.graph_name)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    JOB_STORE.enqueue(run_id, "bitbucket", {
        "request": payload.model_dump(), "repository": repository.__dict__,
    })
    return {"run_id": run_id, "status": "queued"}


@router.get("/sync/{run_id}")
async def sync_status(run_id: str, request: Request) -> dict:
    _, store = _components()
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Bitbucket sync run not found")
    _authorized(request, store, run["connection_id"])
    return run


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str, request: Request) -> dict:
    _, store = _components()
    _authorized(request, store, connection_id)

    graph = get_graph()
    ledger = ConnectorLedger(LEDGER_PATH)
    record_keys = ledger.record_keys_with_prefix(f"bitbucket:{connection_id}:")
    for record_key in record_keys:
        bp.delete_record(graph, ledger, record_key)
    orphans_removed = bp.delete_orphaned_shared_entities(graph)

    store.delete_connection(connection_id)
    logger.info(
        "Bitbucket connection deleted connection_id=%s records_removed=%s orphans_removed=%s",
        connection_id, len(record_keys), orphans_removed,
    )
    return {"deleted": True, "records_removed": len(record_keys), "orphans_removed": orphans_removed}
