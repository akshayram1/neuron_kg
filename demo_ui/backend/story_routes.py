"""Local five-phase story demo backed by Postgres/pgvector + FalkorDB."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from connectors.story import StoryIngestor
from graph import multigraph, vector_store
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema
from storage.ledger import PostgresLedger
from storage.postgres import PostgresConfigurationError, PostgresStore
from util.paths import DATA_DIR

logger = logging.getLogger("uvicorn.error.story_demo")
router = APIRouter(prefix="/api/story-demo", tags=["story-demo"])
STORY_ROOT = Path(__file__).resolve().parents[2] / "story"
GRAPH_REGISTRY = multigraph.GraphRegistry(DATA_DIR / "graphs.sqlite3")
_tasks: set[asyncio.Task] = set()

_ORDER = ["baseline", "deprecation", "migration-claim", "code-catches-up", "v1-removal"]
_ROUTING_TOTAL_KEYS = (
    "pass1_records_linked", "pass1_links_written", "chunks_llm_skipped",
    "chunks_hybrid", "chunks_llm_only", "llm_calls",
    "ingestion_input_tokens", "ingestion_output_tokens", "ingestion_total_tokens",
)


class WisdomReviewRequest(BaseModel):
    decision: str


def _store() -> PostgresStore:
    try:
        store = PostgresStore()
        store.bootstrap()
        return store
    except PostgresConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _target(name: str) -> multigraph.GraphTarget:
    return multigraph.resolve(
        name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store.COLLECTION,
    )


def _graph_state(store: PostgresStore, name: str) -> tuple[dict, list[dict]]:
    graph = store.graph(name)
    if not graph:
        raise HTTPException(status_code=404, detail="Story graph not found")
    return graph, store.runs_for_graph(graph["id"])


def _schedule(run_id: str, graph_row: dict, phase: str) -> None:
    async def execute() -> None:
        store = PostgresStore()
        target = _target(graph_row["name"])

        def progress(value: dict) -> None:
            store.update_run(run_id, progress=value)

        try:
            graph = get_graph(name=target.falkor_name)
            bootstrap_schema(graph)
            vector_store.ensure_collection(vector_store.client(), collection=target.qdrant_collection)
            ledger = PostgresLedger(store, graph_row["id"])
            ingestor = StoryIngestor(
                root=STORY_ROOT, graph=graph, store=store, ledger=ledger,
                graph_id=graph_row["id"], collection=target.qdrant_collection,
            )
            run_llm = os.getenv("STORY_RUN_LLM", "true").lower() not in {"0", "false", "no"}
            result = await asyncio.to_thread(
                ingestor.ingest, phase, run_llm=run_llm, progress=progress,
            )
            findings = store.findings(graph_row["id"])
            store.update_run(run_id, status="completed", progress={
                **result.as_dict(), "phase": "done", "current": f"{phase} completed",
                "open_findings": sum(item["status"] == "open" for item in findings),
            })
        except Exception as exc:
            logger.exception("Story phase failed graph=%s phase=%s", graph_row["name"], phase)
            store.update_run(run_id, status="failed", error=str(exc)[:2000])

    task = asyncio.create_task(execute())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.post("/start", status_code=status.HTTP_202_ACCEPTED)
async def start_demo() -> dict:
    store = _store()
    suffix = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    graph_name = f"story-{suffix}"
    target = _target(graph_name)
    graph_row = store.create_graph(
        graph_name, "CTO Story Demo", target.falkor_name, target.qdrant_collection,
    )
    try:
        GRAPH_REGISTRY.create(graph_name, "CTO Story Demo")
        bootstrap_schema(get_graph(name=target.falkor_name))
        vector_store.ensure_collection(vector_store.client(), collection=target.qdrant_collection)
    except Exception:
        store.delete_graph(graph_name)
        if GRAPH_REGISTRY.exists(graph_name):
            GRAPH_REGISTRY.delete(graph_name)
        raise
    run_id = store.start_run("baseline", graph_row["id"])
    _schedule(run_id, graph_row, "baseline")
    return {"run_id": run_id, "graph_name": graph_name, "phase": "baseline", "status": "running"}


@router.post("/apply/{phase}", status_code=status.HTTP_202_ACCEPTED)
async def apply_phase(phase: str, graph_name: str = Query(min_length=1, max_length=40)) -> dict:
    if phase not in _ORDER[1:]:
        raise HTTPException(status_code=422, detail=f"Unknown phase {phase!r}")
    store = _store()
    graph_row, runs = _graph_state(store, graph_name)
    if any(run["status"] == "running" for run in runs):
        raise HTTPException(status_code=409, detail="Another story phase is still running")
    completed = {run["phase"] for run in runs if run["status"] == "completed"}
    required = _ORDER[_ORDER.index(phase) - 1]
    if required not in completed:
        raise HTTPException(status_code=409, detail=f"Complete {required!r} before {phase!r}")
    if phase in completed:
        raise HTTPException(status_code=409, detail=f"Phase {phase!r} is already complete")
    run_id = store.start_run(phase, graph_row["id"])
    _schedule(run_id, graph_row, phase)
    return {"run_id": run_id, "graph_name": graph_name, "phase": phase, "status": "running"}


@router.get("/runs/{run_id}")
async def run_status(run_id: str) -> dict:
    run = _store().run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Story run not found")
    return run


@router.get("/state")
async def story_state(graph_name: str = Query(min_length=1, max_length=40)) -> dict:
    store = _store()
    graph_row, runs = _graph_state(store, graph_name)
    completed = [phase for phase in _ORDER if any(
        run["phase"] == phase and run["status"] == "completed" for run in runs
    )]
    running = next((run for run in runs if run["status"] == "running"), None)
    last_completed = next((run for run in runs if run["status"] == "completed"), None)
    routing_totals = {
        key: sum(
            int(run["progress"].get(key) or 0)
            for run in runs if run["status"] == "completed"
        )
        for key in _ROUTING_TOTAL_KEYS
    }
    routing_totals["routing_runs_measured"] = sum(
        run["status"] == "completed" and "pass1_links_written" in run["progress"]
        for run in runs
    )
    return {
        "graph": graph_row, "completedPhases": completed, "running": running,
        "lastRun": last_completed, "routingTotals": routing_totals,
        "findings": store.findings(graph_row["id"]),
        "wisdom": store.wisdom_proposals(graph_row["id"]),
    }


@router.post("/wisdom/{proposal_id}/review")
async def review_wisdom(
    proposal_id: str, payload: WisdomReviewRequest,
    graph_name: str = Query(min_length=1, max_length=40),
) -> dict:
    decision = {"approve": "active", "reject": "rejected"}.get(payload.decision)
    if decision is None:
        raise HTTPException(status_code=422, detail="Decision must be 'approve' or 'reject'")
    store = _store()
    graph_row, _runs = _graph_state(store, graph_name)
    if not store.review_wisdom_proposal(
        graph_row["id"], proposal_id, decision=decision,
    ):
        raise HTTPException(status_code=409, detail="Wisdom proposal is not awaiting review")
    target = _target(graph_name)
    graph = get_graph(name=target.falkor_name)
    ingestor = StoryIngestor(
        root=STORY_ROOT, graph=graph, store=store,
        ledger=PostgresLedger(store, graph_row["id"]), graph_id=graph_row["id"],
        collection=target.qdrant_collection,
    )
    ingestor.project_wisdom()
    proposal = next(
        item for item in store.wisdom_proposals(graph_row["id"])
        if item["id"] == proposal_id
    )
    return {"wisdom": proposal}


@router.delete("/reset")
async def reset_demo(graph_name: str = Query(min_length=1, max_length=40)) -> dict:
    store = _store()
    graph_row, runs = _graph_state(store, graph_name)
    if any(run["status"] == "running" for run in runs):
        raise HTTPException(status_code=409, detail="Wait for the active phase before resetting")
    target = _target(graph_name)
    graph = get_graph(name=target.falkor_name)
    before_rows = graph.query("MATCH (n) RETURN count(n)").result_set
    before = int(before_rows[0][0]) if before_rows else 0
    graph.delete()
    client = vector_store.client()
    if client.collection_exists(target.qdrant_collection):
        client.delete_collection(target.qdrant_collection)
    store.delete_graph(graph_name)
    if GRAPH_REGISTRY.exists(graph_name):
        GRAPH_REGISTRY.delete(graph_name)
    return {"reset": True, "graph_name": graph_name, "nodes_removed": before}
