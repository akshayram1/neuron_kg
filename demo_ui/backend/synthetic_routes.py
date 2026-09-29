"""Manual ingestion of the real Jira, Bitbucket and Notion export.

Each of the three endpoints below is one ingestion *step* the UI triggers
directly (no connect/OAuth/pick-a-project flow): it loads
``nilus_data/<provider>_real/``, via ``connectors.synthetic.loader``,
and runs it through the exact same `graph.<provider>_pipeline` writers a real
sync uses. No live provider request is made; all source material comes from
the saved local export.

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
from connectors.synthetic import loader as local_data
from util.flow_log import log_box
from graph.ingestion import bitbucket_pipeline as bp
from graph.ingestion import finding_bridge
from graph.ingestion import jira_pipeline as jp
from graph.storage import multigraph
from graph.ingestion import notion_pipeline as np
from graph.storage import vector_store as vector_store_module
from graph.storage import writer as w
from graph.storage.embeddings import close_batch, open_batch
from graph.storage.falkor_client import get_graph
from graph.storage.schema import bootstrap_schema
from graph.ingestion.semantic_pass import run_semantic_pass
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/local-data", tags=["local-data-ingestion"])
logger = logging.getLogger("uvicorn.error.local_data")


class LocalDataIngestRequest(BaseModel):
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
    # Same call every real connector route makes; see graph/storage/embeddings.py.
    open_batch(OpenAI(timeout=30.0, max_retries=2), os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3"), collection)


def _record_count(ledger: ConnectorLedger, provider: str, connection_id: str) -> int:
    return len(ledger.record_keys_with_prefix(f"{provider}:{connection_id}:"))


def _actions() -> dict[str, int]:
    return {"insert": 0, "update": 0, "keep": 0, "delete": 0}


def _count(actions: dict[str, int], action: RecordAction) -> None:
    actions[action.value] = actions.get(action.value, 0) + 1


def _reset(graph, ledger: ConnectorLedger, provider: str, connection_id: str) -> dict:
    """Scoped delete: only this provider's local-export connection, never a
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
async def local_data_status(graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME)) -> dict:
    _, _, ledger = _target(graph_name)
    available = local_data.availability()
    summary = local_data.data_summary()
    return {
        "path": str(local_data.data_dir()),
        "steps": {
            "jira": {
                "available": available["jira"],
                "available_records": summary["jira"],
                "ingested_records": _record_count(ledger, "jira", local_data.LOCAL_DATA_JIRA_CONNECTION),
            },
            "bitbucket": {
                "available": available["bitbucket"],
                "available_records": summary["bitbucket"],
                "ingested_records": _record_count(ledger, "bitbucket", local_data.LOCAL_DATA_BITBUCKET_CONNECTION),
            },
            "notion": {
                "available": available["notion"],
                "available_records": summary["notion"],
                "ingested_records": _record_count(ledger, "notion", local_data.LOCAL_DATA_NOTION_CONNECTION),
            },
        },
    }


def _ingest_jira(payload: LocalDataIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    site, project, issues = local_data.load_jira()
    connection_id = local_data.LOCAL_DATA_JIRA_CONNECTION
    log_box(logger, "INGEST jira  deterministic pass", [
        f"graph={target.name}  project={project.key}  issues={len(issues)}",
        "pass A writes Project/WorkItem/Person and structural edges from API fields",
        "unchanged content hash => keep (no rewrite); changed hash => update",
        "free-text chunks are queued; this route does not run the semantic LLM pass",
    ])
    _open_embedding_batch(target.qdrant_collection)
    actions = _actions()
    kept = written = 0
    payload_out = None
    try:
        _count(actions, jp.write_project(graph, ledger, project, site, connection_id))
        for issue in issues:
            action = jp.write_issue(
                graph, ledger, issue, project, site, connection_id,
                collection=target.qdrant_collection,
            )
            _count(actions, action)
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
        orphans_removed = jp.delete_orphaned_shared_entities(graph)
        payload_out = {
            "provider": "jira", "project_key": project.key,
            "issues_fetched": len(issues), "records_kept": kept, "records_written": written,
            "orphans_removed": orphans_removed,
        }
    finally:
        flushed = close_batch()
        if payload_out is not None:
            log_box(logger, "INGEST jira  done", [
                f"insert={actions['insert']}  update={actions['update']}  keep={actions['keep']}  delete={actions['delete']}",
                f"embeddings_flushed={flushed}  orphans_removed={payload_out['orphans_removed']}",
                "keep = reingest skipped the graph write because the content hash matched",
            ])
    return payload_out


@router.post("/jira/ingest")
async def ingest_jira(payload: LocalDataIngestRequest) -> dict:
    return await run_in_threadpool(_ingest_jira, payload)


def _ingest_bitbucket(payload: LocalDataIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    repository, prs, commits_by_pr = local_data.load_bitbucket()
    files = local_data.load_bitbucket_files(repository)
    connection_id = local_data.LOCAL_DATA_BITBUCKET_CONNECTION
    commits_by_hash = {
        commit.commit_hash: commit
        for commits in commits_by_pr.values()
        for commit in commits
        if commit.commit_hash
    }
    commit_count = len(commits_by_hash)
    log_box(logger, "INGEST bitbucket  deterministic pass", [
        f"graph={target.name}  repo={repository.full_name}  files={len(files)}  prs={len(prs)}  commits={commit_count}",
        "pass A writes Repository/SourceFile/PullRequest/Commit/Person and structural edges",
        "unchanged content hash => keep; changed hash => update and reset supported edges",
        "ticket keys in text become exact cross-source anchors, not LLM facts",
    ])
    _open_embedding_batch(target.qdrant_collection)
    actions = _actions()
    kept = written = files_written = commits_written = 0
    payload_out = None
    try:
        _count(actions, bp.write_repository(graph, ledger, repository, connection_id))
        present_paths = {file.path for file in files}
        for file in files:
            file_action = bp.write_file(
                graph, ledger, repository, file, connection_id,
                collection=target.qdrant_collection,
            )
            _count(actions, file_action)
            files_written += file_action != RecordAction.KEEP
        pr: BitbucketPullRequest
        for pr in prs:
            action = bp.write_pull_request(
                graph, ledger, repository, pr, connection_id,
                collection=target.qdrant_collection,
            )
            _count(actions, action)
            kept += action == RecordAction.KEEP
            written += action != RecordAction.KEEP
        for commit in commits_by_hash.values():
            commit_action = bp.write_commit(
                graph, ledger, repository, commit, connection_id,
                collection=target.qdrant_collection, present_paths=present_paths,
            )
            _count(actions, commit_action)
            commits_written += commit_action != RecordAction.KEEP
        orphans_removed = jp.delete_orphaned_shared_entities(graph)
        payload_out = {
            "provider": "bitbucket", "repository": repository.full_name,
            "pull_requests_fetched": len(prs), "records_kept": kept, "records_written": written,
            "files_fetched": len(files), "files_written": files_written,
            "commits_fetched": commit_count, "commits_written": commits_written,
            "orphans_removed": orphans_removed,
        }
    finally:
        flushed = close_batch()
        if payload_out is not None:
            log_box(logger, "INGEST bitbucket  done", [
                f"insert={actions['insert']}  update={actions['update']}  keep={actions['keep']}  delete={actions['delete']}",
                f"embeddings_flushed={flushed}  orphans_removed={payload_out['orphans_removed']}",
                "keep = reingest skipped the graph write because the content hash matched",
            ])
    return payload_out


@router.post("/bitbucket/ingest")
async def ingest_bitbucket(payload: LocalDataIngestRequest) -> dict:
    return await run_in_threadpool(_ingest_bitbucket, payload)


@router.post("/notion/ingest")
async def ingest_notion(payload: LocalDataIngestRequest) -> dict:
    target, graph, ledger = _target(payload.graph_name)
    workspace_id, workspace_name, pages = local_data.load_notion()
    log_box(logger, "INGEST notion  deterministic pass + semantic pass", [
        f"graph={target.name}  workspace={workspace_name}  pages={len(pages)}",
        "pass A writes Workspace/Document and parent edges from the page tree",
        "pass B is Notion-only here: pending chunks go to the semantic LLM",
        "assessments categorise addition|update|contradiction|architecture_change|review",
        "should_flag=true becomes a Finding; wisdom is a later review proposal, not this pass",
    ])
    _open_embedding_batch(target.qdrant_collection)
    actions = _actions()
    payload_out = None
    try:
        _count(actions, np.write_workspace(graph, ledger, workspace_id, workspace_name))
        kept = written = 0
        for page in pages:
            action = np.write_page(graph, ledger, page, workspace_id, workspace_name)
            _count(actions, action)
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
        payload_out = {
            "provider": "notion", "workspace": workspace_name,
            "pages_fetched": len(pages), "records_kept": kept, "records_written": written,
            "chunks_ingested": semantic.chunks_processed,
            "entities_written": semantic.entities_written, "facts_written": semantic.facts_written,
            "orphans_removed": orphans_removed, "findings_synced": findings_synced,
            **semantic.token_usage.as_dict("ingestion"),
        }
    finally:
        flushed = close_batch()
        if payload_out is not None:
            log_box(logger, "INGEST notion  done", [
                f"insert={actions['insert']}  update={actions['update']}  keep={actions['keep']}  delete={actions['delete']}",
                f"chunks={payload_out['chunks_ingested']}  entities={payload_out['entities_written']}  facts={payload_out['facts_written']}",
                f"findings_synced={payload_out['findings_synced']}  embeddings_flushed={flushed}",
                "keep = reingest skipped the page write; already-extracted chunks skip the LLM",
            ])
    return payload_out


@router.delete("/{provider}")
async def reset_provider(provider: str, graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME)) -> dict:
    connection_by_provider = {
        "jira": local_data.LOCAL_DATA_JIRA_CONNECTION,
        "bitbucket": local_data.LOCAL_DATA_BITBUCKET_CONNECTION,
        "notion": local_data.LOCAL_DATA_NOTION_CONNECTION,
    }
    if provider not in connection_by_provider:
        return {"deleted": False, "error": f"Unknown provider {provider!r}"}
    _, graph, ledger = _target(graph_name)
    result = _reset(graph, ledger, provider, connection_by_provider[provider])
    logger.info("local data %s reset: %s", provider, result)
    return {"deleted": True, **result}
