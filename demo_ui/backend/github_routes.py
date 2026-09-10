"""GitHub App OAuth, repository picker and unified-graph sync routes."""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator

from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger
from connectors.github_app.api import (
    GitHubApiClient, GitHubApiError, GitHubFileTextError, GitHubRepository,
    SUPPORTED_FILE_TYPES,
)
from connectors.github_app.auth import (
    GitHubAppSettings, GitHubAuthClient, GitHubAuthError, GitHubConfigurationError,
)
from connectors.github_app.store import GitHubStore, github_state_db_path
from graph import github_pipeline as gp
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from graph.semantic_pass import run_semantic_pass
from util.paths import DATA_DIR
from demo_ui.backend.job_worker import JOB_STORE

router = APIRouter(prefix="/api/connectors/github", tags=["github-connector"])
SESSION_COOKIE = "neuron_github_session"
LEDGER_PATH = DATA_DIR / "connector_ledger.sqlite3"
logger = logging.getLogger("uvicorn.error.github_connector")


class GitHubSyncRequest(BaseModel):
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    file_types: list[str] = Field(default_factory=list, max_length=2)
    include_commit_messages: bool = True

    @model_validator(mode="after")
    def validate_selection(self) -> "GitHubSyncRequest":
        normalized = list(dict.fromkeys(value.lower() for value in self.file_types))
        if any(value not in SUPPORTED_FILE_TYPES for value in normalized):
            raise ValueError("Supported GitHub file types are .py and .md")
        if not normalized and not self.include_commit_messages:
            raise ValueError("Select at least one file type or commit messages")
        self.file_types = normalized
        return self


def _components() -> tuple[GitHubAppSettings, GitHubStore]:
    try:
        settings = GitHubAppSettings.from_env()
        return settings, GitHubStore(github_state_db_path())
    except GitHubConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _session(request: Request, response: Response | None = None, create: bool = False) -> str:
    value = request.cookies.get(SESSION_COOKIE)
    if not value and create:
        value = secrets.token_urlsafe(32)
        if response is not None:
            response.set_cookie(SESSION_COOKIE, value, max_age=30 * 86400, httponly=True,
                                samesite="lax", secure=request.url.scheme == "https", path="/")
    if not value:
        raise HTTPException(status_code=401, detail="Connect GitHub from this browser first")
    return value


def _authorized(request: Request, store: GitHubStore, installation_id: int) -> None:
    session_hash = store.session_hash(_session(request))
    if not store.session_has_installation(session_hash, installation_id):
        raise HTTPException(status_code=404, detail="GitHub installation not found")


async def _repositories(settings: GitHubAppSettings, installation_id: int) -> list[GitHubRepository]:
    try:
        token = await GitHubAuthClient(settings).installation_token(installation_id)
        async with GitHubApiClient(token) as client:
            return await client.list_repositories()
    except (GitHubAuthError, GitHubApiError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/status")
async def github_status(request: Request, response: Response) -> dict:
    _, store = _components()
    session_hash = store.session_hash(_session(request, response, True))
    return {
        "configured": True,
        "installations": store.list_installations(session_hash),
        "sources": store.list_sources(session_hash),
        "runs": store.list_sync_runs(session_hash),
    }


@router.post("/oauth/start")
async def github_oauth_start(request: Request, response: Response) -> dict:
    settings, store = _components()
    state_value = store.create_oauth_state(_session(request, response, True))
    return {"installation_url": settings.installation_url(state_value)}


@router.get("/oauth/callback", response_class=HTMLResponse)
async def github_oauth_callback(
    state: str = Query(min_length=20, max_length=500),
    code: str | None = Query(default=None, min_length=1, max_length=4_000),
    installation_id: int | None = Query(default=None, gt=0),
    error: str | None = Query(default=None, max_length=500),
    error_description: str | None = Query(default=None, max_length=2_000),
) -> HTMLResponse:
    settings, store = _components()
    session_hash = store.consume_oauth_state(state)
    if not session_hash:
        raise HTTPException(status_code=400, detail="OAuth state is invalid or expired")
    if error or not code:
        raise HTTPException(status_code=400, detail=f"GitHub authorization failed: {error_description or error or 'missing code'}")
    try:
        auth = GitHubAuthClient(settings)
        user_token = await auth.exchange_user_code(code)
        if installation_id:
            installations = [await auth.user_installation(user_token, installation_id)]
        else:
            installations = [
                item for item in await auth.user_installations(user_token)
                if str(item.get("app_id") or "") == settings.app_id
            ]
        if not installations:
            raise GitHubAuthError("No installation of this GitHub App is available")
        for installation in installations:
            verified_id = store.save_installation(installation)
            store.link_session_installation(session_hash, verified_id)
    except (GitHubAuthError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    account = html.escape(str((installations[0].get("account") or {}).get("login") or "GitHub"))
    return HTMLResponse(
        "<!doctype html><title>GitHub connected</title><h1>GitHub connected</h1>"
        f"<p>{account} is ready.</p>"
        "<script>if(window.opener){window.opener.postMessage({type:'github-oauth-complete'},'*');window.close()}</script>"
    )


@router.get("/repositories")
async def list_repositories(request: Request, installation_id: int = Query(gt=0)) -> dict:
    settings, store = _components()
    _authorized(request, store, installation_id)
    repositories = await _repositories(settings, installation_id)
    return {"repositories": [repo.__dict__ for repo in repositories]}


async def _run_sync(
    run_id: str, payload: GitHubSyncRequest, repository: GitHubRepository,
    settings: GitHubAppSettings, store: GitHubStore,
) -> None:
    try:
        base = {
            "installation_id": payload.installation_id,
            "repository_id": payload.repository_id,
            "repository_full_name": repository.full_name,
            "file_types": payload.file_types,
            "include_commit_messages": payload.include_commit_messages,
            "records_done": 0, "records_total": 0, "records_written": 0,
            "records_kept": 0, "files_matched": 0, "files_processed": 0,
            "files_too_large": 0, "files_without_text": 0, "commits_fetched": 0,
            "chunks_ingested": 0, "chunks_total": 0,
            "entities_written": 0, "facts_written": 0,
        }
        store.save_source(payload.installation_id, payload.repository_id, repository.full_name,
                          repository.default_branch, payload.file_types,
                          payload.include_commit_messages)
        store.set_sync_run(run_id, "running", {
            **base, "phase": "fetching", "current": f"Discovering {repository.full_name}…",
        })
        token = await GitHubAuthClient(settings).installation_token(payload.installation_id)
        async with GitHubApiClient(token) as client:
            files_task = client.list_files(repository, set(payload.file_types), settings.max_file_bytes)
            commits_task = (
                client.list_commits(repository, settings.max_commits_per_sync)
                if payload.include_commit_messages else asyncio.sleep(0, result=[])
            )
            (files, too_large), commits = await asyncio.gather(files_task, commits_task)
            total = len(files) + len(commits)
            base.update({"records_total": total, "files_matched": len(files),
                         "files_too_large": too_large, "commits_fetched": len(commits)})
            store.set_sync_run(run_id, "running", {
                **base, "phase": "ingesting", "current": f"Writing {total} GitHub records…",
            })

            graph = get_graph()
            bootstrap_schema(graph)
            ledger = ConnectorLedger(LEDGER_PATH)
            gp.write_repository(graph, ledger, repository, payload.installation_id)
            done = kept = written = without_text = 0
            present_file_keys = {
                f"github:{payload.installation_id}:source_file:{repository.repository_id}:{file.path}"
                for file in files
            }
            present_commit_keys = {
                f"github:{payload.installation_id}:commit:{repository.repository_id}:{commit.sha}"
                for commit in commits
            }
            for file in files:
                try:
                    content = await client.read_blob(repository, file.sha)
                    action = gp.write_file(graph, ledger, repository, file, content, payload.installation_id)
                    kept += action == RecordAction.KEEP
                    written += action != RecordAction.KEEP
                except GitHubFileTextError:
                    without_text += 1
                done += 1
                store.set_sync_run(run_id, "running", {
                    **base, "phase": "ingesting", "current": file.path,
                    "records_done": done, "records_kept": kept, "records_written": written,
                    "files_processed": done, "files_without_text": without_text,
                })
            for commit in commits:
                action = gp.write_commit(graph, ledger, repository, commit, payload.installation_id)
                kept += action == RecordAction.KEEP
                written += action != RecordAction.KEEP
                done += 1
                store.set_sync_run(run_id, "running", {
                    **base, "phase": "ingesting", "current": commit.message.splitlines()[0][:120],
                    "records_done": done, "records_kept": kept, "records_written": written,
                    "files_processed": len(files), "files_without_text": without_text,
                })

        removed = gp.reconcile_repository_records(
            graph, ledger, payload.installation_id, repository.repository_id,
            present_file_keys, present_commit_keys,
        )

        def semantic_progress(done_chunks: int, total_chunks: int, record_key: str, current) -> None:
            store.set_sync_run(run_id, "running", {
                **base, "phase": "semantic", "current": f"Understanding {record_key.rsplit(':', 1)[-1]}…",
                "records_done": total, "records_total": total,
                "records_kept": kept, "records_written": written,
                "files_processed": len(files), "files_without_text": without_text,
                "chunks_ingested": done_chunks, "chunks_total": total_chunks,
                "entities_written": current.entities_written, "facts_written": current.facts_written,
                **current.token_usage.as_dict("ingestion"),
            })

        # See bitbucket_routes.py's identical wrap: this call blocks for
        # minutes and would otherwise freeze the whole server's event loop.
        semantic = await run_in_threadpool(
            run_semantic_pass, graph, ledger,
            record_prefix=f"github:{payload.installation_id}:",
            on_progress=semantic_progress,
        )
        orphans_removed = gp.delete_orphaned_shared_entities(graph)
        result = {
            **base, "phase": "done", "current": f"Finished {repository.full_name}",
            "records_done": total, "records_total": total, "records_kept": kept,
            "records_written": written, "files_processed": len(files),
            "files_without_text": without_text, "records_removed": removed,
            "chunks_ingested": semantic.chunks_processed,
            "entities_written": semantic.entities_written, "facts_written": semantic.facts_written,
            "orphans_removed": orphans_removed,
            **semantic.token_usage.as_dict("ingestion"),
        }
        store.finish_source_sync(payload.installation_id, payload.repository_id)
        store.set_sync_run(run_id, "completed", result)
        logger.info("GitHub sync completed run=%s %s", run_id, result)
    except asyncio.CancelledError:
        store.set_sync_run(run_id, "failed", error="Sync worker stopped before completion")
        raise
    except Exception as exc:
        logger.exception("GitHub sync failed run=%s", run_id)
        store.finish_source_sync(payload.installation_id, payload.repository_id, str(exc)[:1_000])
        store.set_sync_run(run_id, "failed", error=str(exc)[:1_000])
        raise


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(payload: GitHubSyncRequest, request: Request) -> dict:
    settings, store = _components()
    _authorized(request, store, payload.installation_id)
    repositories = await _repositories(settings, payload.installation_id)
    repository = next((repo for repo in repositories if repo.repository_id == payload.repository_id), None)
    if not repository:
        raise HTTPException(status_code=404, detail="Selected repository is no longer accessible")
    run_id = uuid4().hex
    try:
        store.create_sync_run(run_id, payload.installation_id, payload.repository_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    JOB_STORE.enqueue(run_id, "github", {
        "request": payload.model_dump(), "repository": repository.__dict__,
    })
    return {"run_id": run_id, "status": "queued"}


@router.get("/sync/{run_id}")
async def sync_status(run_id: str, request: Request) -> dict:
    _, store = _components()
    run = store.get_sync_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="GitHub sync run not found")
    _authorized(request, store, int(run["installation_id"]))
    return run


@router.delete("/installations/{installation_id}")
async def delete_installation(installation_id: int, request: Request) -> dict:
    _, store = _components()
    _authorized(request, store, installation_id)
    graph = get_graph()
    ledger = ConnectorLedger(LEDGER_PATH)
    record_keys = ledger.record_keys_with_prefix(f"github:{installation_id}:")
    for record_key in record_keys:
        gp.delete_record(graph, ledger, record_key)
    orphans_removed = gp.delete_orphaned_shared_entities(graph)
    store.delete_installation(installation_id)
    return {"deleted": True, "records_removed": len(record_keys),
            "orphans_removed": orphans_removed}
