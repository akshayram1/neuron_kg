"""Process durable connector jobs registered by the HTTP routes."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from uuid import uuid4

from connectors.bitbucket.api import BitbucketRepository
from connectors.core.jobs import ConnectorJob, ConnectorJobStore
from connectors.github_app.api import GitHubRepository
from util.paths import DATA_DIR

logger = logging.getLogger("uvicorn.error.connector_worker")
JOB_STORE = ConnectorJobStore(DATA_DIR / os.getenv("CONNECTOR_JOB_DB", "connector_jobs.sqlite3"))


async def dispatch(job: ConnectorJob) -> None:
    if job.provider == "jira":
        from demo_ui.backend import jira_routes
        payload = jira_routes.JiraSyncRequest.model_validate(job.payload["request"])
        settings, store = jira_routes._components()
        await jira_routes._run(job.job_id, payload, settings, store)
        return
    if job.provider == "github":
        from demo_ui.backend import github_routes
        payload = github_routes.GitHubSyncRequest.model_validate(job.payload["request"])
        repository = GitHubRepository(**job.payload["repository"])
        settings, store = github_routes._components()
        await github_routes._run_sync(job.job_id, payload, repository, settings, store)
        return
    if job.provider == "notion":
        from demo_ui.backend import notion_routes
        payload = notion_routes.NotionSyncRequest.model_validate(job.payload["request"])
        settings, store = notion_routes._components()
        await notion_routes._run_sync(job.job_id, payload, settings, store)
        return
    if job.provider == "bitbucket":
        from demo_ui.backend import bitbucket_routes
        payload = bitbucket_routes.BitbucketSyncRequest.model_validate(job.payload["request"])
        repository = BitbucketRepository(**job.payload["repository"])
        settings, store = bitbucket_routes._components()
        await bitbucket_routes._run_sync(job.job_id, payload, repository, settings, store)
        return
    raise ValueError(f"Unknown connector job provider: {job.provider}")


async def run_worker(stop: asyncio.Event) -> None:
    worker_id = f"neuron-{os.getpid()}-{uuid4().hex[:8]}"
    while not stop.is_set():
        job = JOB_STORE.claim(worker_id)
        if job is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.75)
            except TimeoutError:
                pass
            continue

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                if not JOB_STORE.heartbeat(job.job_id, worker_id):
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            logger.info("claimed connector job=%s provider=%s attempt=%s", job.job_id, job.provider, job.attempts)
            await dispatch(job)
        except asyncio.CancelledError:
            JOB_STORE.fail(job, worker_id, "Worker stopped; retry is queued")
            raise
        except Exception as exc:
            state = JOB_STORE.fail(job, worker_id, str(exc))
            logger.exception("connector job=%s failed state=%s", job.job_id, state)
        else:
            JOB_STORE.complete(job.job_id, worker_id)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
