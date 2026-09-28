"""Synthetic-fixture ingestion: Bitbucket, Jira and Notion from local JSON
instead of a live OAuth connection (replaces the removed `story` demo).

Each of the three endpoints below is one ingestion *step* the UI triggers
directly (no connect/OAuth/pick-a-project flow): it loads
`synthetic/mcp-access/<provider>_real/`, via `connectors.synthetic.loader`,
and runs it through the exact same `graph.<provider>_pipeline` writers a real
sync uses. The fixture is small (a handful of issues/PRs/pages), so unlike
the real connector routes this runs to completion inside the request instead
of a polled background run — there is no `POST .../sync` + `GET .../sync/{id}`
pair here, just one `POST .../ingest` that returns the finished result.

LLM extraction (the semantic pass) is Notion-only here too, matching the real
Notion route — Jira/Bitbucket facts are deterministic (plan.md §4).
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from openai import OpenAI
from pydantic import BaseModel

from connectors.bitbucket.api import BitbucketPullRequest
from connectors.core.actions import RecordAction
from connectors.core.ledger import ConnectorLedger
from connectors.synthetic import loader as synthetic
from graph import bitbucket_pipeline as bp
from graph import finding_bridge
from graph import jira_pipeline as jp
from graph import multigraph
from graph import notion_pipeline as np
from graph import vector_store as vector_store_module
from graph import writer as w
from graph.embed_batch import close_batch, open_batch
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from graph.semantic_pass import run_semantic_pass
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/synthetic", tags=["synthetic-ingestion"])
logger = logging.getLogger("uvicorn.error.synthetic")


class SyntheticIngestRequest(BaseModel):
    graph_name: str = multigraph.DEFAULT_GRAPH_NAME


def _target(graph_name: str):
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    graph = get_graph(name=target.falkor_name)
    bootstrap_schema(graph)
    vector_store_module.ensure_collection(vector_store_module.client(), collection=target.qdrant_collection)
    ledger = ConnectorLedger(target.ledger_path)
    return target, graph, ledger


def _open_embedding_batch(collection: str) -> None:
    # Same call every real connector route makes; see graph/embed_batch.py.
    open_batch(OpenAI(timeout=30.0, max_retries=2), os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"), collection)


def _record_count(ledger: ConnectorLedger, provider: str, connection_id: str) -> int:
    return len(ledger.record_keys_with_prefix(f"{provider}:{connection_id}:"))


def _reset(graph, ledger: ConnectorLedger, provider: str, connection_id: str) -> dict:
    """Scoped delete: only this provider's synthetic connection, never a
    full graph/ledger clear (see the standing scoped-cleanup rule)."""
    prefix = f"{provider}:{connection_id}:"
    record_keys = ledger.record_keys_with_prefix(prefix)
    for record_key in record_keys:
        jp.delete_record(graph, ledger, record_key)
    orphans_removed = jp.delete_orphaned_shared_entities(graph)
    finding_keys = ledger.delete_findings_with_prefix(prefix)
    for finding_key in finding_keys:
        w.delete_node(graph, w.make_uid("Finding", finding_key))
    return {
        "records_removed": len(record_keys), "orphans_removed": orphans_removed,
        "findings_removed": len(finding_keys),
    }


@router.get("/status")
async def synthetic_status(graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME)) -> dict:
    _, _, ledger = _target(graph_name)
    available = synthetic.availability()
    return {
        "steps": {
            "jira": {
                "available": available["jira"],
                "ingested_records": _record_count(ledger, "jira", synthetic.SYNTHETIC_JIRA_CONNECTION),
            },
            "bitbucket": {
                "available": available["bitbucket"],
                "ingested_records": _record_count(ledger, "bitbucket", synthetic.SYNTHETIC_BITBUCKET_CONNECTION),
            },
            "notion": {
                "available": available["notion"],
                "ingested_records": _record_count(ledger, "notion", synthetic.SYNTHETIC_NOTION_CONNECTION),
            },
        },
    }


@router.post("/jira/ingest")
async def ingest_jira(payload: SyntheticIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    site, project, issues = synthetic.load_jira()
    connection_id = synthetic.SYNTHETIC_JIRA_CONNECTION
    _open_embedding_batch(target.qdrant_collection)
    try:
        jp.write_project(graph, ledger, project, site, connection_id)
        kept = written = 0
        for issue in issues:
            action = jp.write_issue(
                graph, ledger, issue, project, site, connection_id,
                collection=target.qdrant_collection,
            )
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
        orphans_removed = jp.delete_orphaned_shared_entities(graph)
    finally:
        close_batch()
    return {
        "provider": "jira", "project_key": project.key,
        "issues_fetched": len(issues), "records_kept": kept, "records_written": written,
        "orphans_removed": orphans_removed,
    }


@router.post("/bitbucket/ingest")
async def ingest_bitbucket(payload: SyntheticIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    repository, prs, commits_by_pr = synthetic.load_bitbucket()
    connection_id = synthetic.SYNTHETIC_BITBUCKET_CONNECTION
    _open_embedding_batch(target.qdrant_collection)
    try:
        bp.write_repository(graph, ledger, repository, connection_id)
        kept = written = 0
        commits_written = 0
        pr: BitbucketPullRequest
        for pr in prs:
            action = bp.write_pull_request(
                graph, ledger, repository, pr, connection_id,
                collection=target.qdrant_collection,
            )
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
            for commit in commits_by_pr.get(pr.id, []):
                commit_action = bp.write_commit(
                    graph, ledger, repository, commit, connection_id,
                    collection=target.qdrant_collection,
                )
                commits_written += commit_action != RecordAction.KEEP
        orphans_removed = jp.delete_orphaned_shared_entities(graph)
    finally:
        close_batch()
    return {
        "provider": "bitbucket", "repository": repository.full_name,
        "pull_requests_fetched": len(prs), "records_kept": kept, "records_written": written,
        "commits_written": commits_written, "orphans_removed": orphans_removed,
    }


@router.post("/notion/ingest")
async def ingest_notion(payload: SyntheticIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    workspace_id, workspace_name, pages = synthetic.load_notion()
    _open_embedding_batch(target.qdrant_collection)
    try:
        np.write_workspace(graph, ledger, workspace_id, workspace_name)
        kept = written = 0
        for page in pages:
            action = np.write_page(graph, ledger, page, workspace_id, workspace_name)
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP

        # Same threadpool wrap the real Notion route uses: this can run an
        # LLM extraction pass lasting minutes and must not freeze the event loop.
        semantic = await run_in_threadpool(
            run_semantic_pass, graph, ledger,
            record_prefix=f"notion:{workspace_id}:",
            collection=target.qdrant_collection,
        )
        orphans_removed = jp.delete_orphaned_shared_entities(graph)
        # Bring any should_flag=True judgements the semantic pass just
        # recorded in the ledger (record_ingestion_assessments) into the
        # graph as Finding nodes -- see graph/finding_bridge.py for why this
        # can't happen inside the semantic pass itself.
        findings_synced = finding_bridge.sync_ledger_findings(
            graph, ledger, record_prefix=f"notion:{workspace_id}:",
            collection=target.qdrant_collection,
        )
    finally:
        close_batch()
    return {
        "provider": "notion", "workspace": workspace_name,
        "pages_fetched": len(pages), "records_kept": kept, "records_written": written,
        "chunks_ingested": semantic.chunks_processed,
        "entities_written": semantic.entities_written, "facts_written": semantic.facts_written,
        "orphans_removed": orphans_removed, "findings_synced": findings_synced,
        **semantic.token_usage.as_dict("ingestion"),
    }


@router.delete("/{provider}")
async def reset_provider(provider: str, graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME)) -> dict:
    connection_by_provider = {
        "jira": synthetic.SYNTHETIC_JIRA_CONNECTION,
        "bitbucket": synthetic.SYNTHETIC_BITBUCKET_CONNECTION,
        "notion": synthetic.SYNTHETIC_NOTION_CONNECTION,
    }
    if provider not in connection_by_provider:
        return {"deleted": False, "error": f"Unknown provider {provider!r}"}
    _, graph, ledger = _target(graph_name)
    result = _reset(graph, ledger, provider, connection_by_provider[provider])
    logger.info("synthetic %s reset: %s", provider, result)
    return {"deleted": True, **result}
