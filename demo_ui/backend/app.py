"""FastAPI adapter for the React demo (plan.md §6a.1).

Ported from `graphiti_context_explorer/demo_ui/backend/app.py`, then adapted
to the single unified Jira + GitHub + Notion graph:

  - `/api/chat` uses this repo's native hybrid BM25+vector+RRF search and
    returns citations plus the exact graph path used by the answer.
  - Jira, GitHub and Notion connector routes feed the same physical graph.
  - Exact cross-source anchors are resolved in the shared write layer; there
    is no per-source graph or Graphiti runtime dependency.
  - MCP block in `/api/config` — dropped; no MCP server in this project
    (plan.md §6a.2).

Run from the repository root:

    uv run uvicorn demo_ui.backend.app:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

from connectors.core.ledger import ConnectorLedger
from demo_ui.backend.bitbucket_routes import router as bitbucket_router
from demo_ui.backend.github_routes import router as github_router
from demo_ui.backend.jira_routes import router as jira_router
from demo_ui.backend.notion_routes import router as notion_router
from demo_ui.backend.access import access_scope_for_request
from demo_ui.backend.job_worker import run_worker
from graph import vector_store
from graph.chat import run_chat_turn
from graph.entity import fetch_entity_detail
from graph.falkor_client import get_graph
from graph.graph_view import fetch_graph, fetch_sources
from graph.history import fetch_fact_history
from graph.schema import bootstrap_schema
from graph.skos_export import build_skos_turtle
from util import paths as _paths  # noqa: F401 — load the repo-root .env
from util.logging import configure_logging
from util.paths import DATA_DIR

configure_logging()

logger = logging.getLogger("uvicorn.error.neuron")
_worker_stop: asyncio.Event | None = None
_worker_task: asyncio.Task | None = None
LEDGER_PATH = DATA_DIR / "connector_ledger.sqlite3"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    providers: list[str] = Field(default_factory=lambda: ["jira", "github", "bitbucket", "notion"])
    at: str | None = None
    as_of: str | None = None

app = FastAPI(
    title="Neuron context graph API",
    version="0.1.0",
    description="Structured browser adapter over the shared FalkorDB context graph.",
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("DEMO_UI_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)
app.include_router(jira_router)
app.include_router(github_router)
app.include_router(bitbucket_router)
app.include_router(notion_router)


@app.middleware("http")
async def prevent_stale_frontend_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/index.html"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.on_event("startup")
async def _ensure_schema() -> None:
    global _worker_stop, _worker_task
    bootstrap_schema(get_graph())
    _worker_stop = asyncio.Event()
    _worker_task = asyncio.create_task(run_worker(_worker_stop))


@app.on_event("shutdown")
async def _stop_worker() -> None:
    if _worker_stop is not None:
        _worker_stop.set()
    if _worker_task is not None:
        _worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await _worker_task


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "neuron"}


@app.get("/api/config")
async def config() -> dict:
    providers = ["jira", "github", "bitbucket", "notion"]
    return {"providers": providers, "defaultProviders": providers}


@app.get("/api/graph")
async def graph(request: Request, providers: list[str] | None = Query(default=None)) -> dict:
    try:
        view = fetch_graph(get_graph(), access_scope_for_request(request), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    return {"groups": providers or ["jira", "github", "bitbucket", "notion"], **view}


@app.get("/api/sources")
async def sources(request: Request) -> dict:
    return {"sources": fetch_sources(get_graph(), access_scope_for_request(request))}


@app.get("/api/entities/{uid}")
async def entity_detail(
    uid: str, request: Request,
    at: str | None = Query(default=None),
    as_of: str | None = Query(default=None),
    providers: list[str] | None = Query(default=None),
) -> dict:
    """Relations (current + past), record-axis history, and derived proofs."""
    try:
        detail = fetch_entity_detail(
            get_graph(), access_scope_for_request(request), uid,
            at=at, as_of=as_of, providers=providers,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="at and as_of must be ISO-8601 timestamps") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Entity not found or not visible")
    return detail


@app.get("/api/facts/{fact_uid}/history")
async def fact_history(
    fact_uid: str, request: Request,
    valid_at: str | None = Query(default=None),
    observed_at: str | None = Query(default=None),
) -> dict:
    """Audit a fact or select business-time / transaction-time state."""
    try:
        intervals = fetch_fact_history(
            get_graph(), access_scope_for_request(request), fact_uid,
            valid_at=valid_at, observed_at=observed_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="valid_at and observed_at must be ISO-8601 timestamps") from exc
    return {"factUid": fact_uid, "intervals": intervals}


@app.get("/api/export/skos")
async def export_skos(request: Request, providers: list[str] | None = Query(default=None)) -> PlainTextResponse:
    """Download the graph as SKOS RDF (Turtle) — plan.md §6b.1."""
    try:
        view = fetch_graph(get_graph(), access_scope_for_request(request), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    turtle = build_skos_turtle(view["nodes"], view["edges"])
    filename = "context-graph-" + "-".join(providers or ["jira", "github", "bitbucket", "notion"])[:60] + ".ttl"
    return PlainTextResponse(
        turtle, media_type="text/turtle", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/admin/clear-graph")
async def clear_graph() -> dict:
    """Wipe the knowledge graph and its vector projection for a fresh sync.

    Deliberately scoped to exactly the `neuron` FalkorDB graph -- other
    graphs sharing this Redis instance (a `GRAPH.COPY` backup, or the old
    graphiti_context_explorer project's graphs) are untouched. Also clears
    `connector_ledger.sqlite3`: without that, every connector would see an
    unchanged content hash on the next sync and skip writing anything back
    into the now-empty graph. OAuth connections (Jira/GitHub/Notion/Bitbucket)
    are never touched here -- reconnecting Bitbucket in particular required
    an admin round-trip, so nothing in this endpoint may force that again.
    """
    graph = get_graph()
    before = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    graph.delete()
    bootstrap_schema(get_graph())

    client = vector_store.client()
    if client.collection_exists(vector_store.COLLECTION):
        client.delete_collection(vector_store.COLLECTION)
    vector_store.ensure_collection(client)

    ledger = ConnectorLedger(LEDGER_PATH)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM source_records")
        db.execute("DELETE FROM source_chunks")
        db.execute("DELETE FROM record_edges")

    logger.info("cleared neuron graph: %s nodes removed, ledger and vector store reset", before)
    return {"cleared": True, "nodes_removed": before}


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request) -> dict:
    """Answer from the live graph and return the exact canvas path used.

    Takes both the parsed body (`payload`) and the raw `Request`: the ACL
    scope is derived from session cookies, which only exist on the latter.
    Naming the body `request` shadowed it and made `access_scope_for_request`
    read `.cookies` off a Pydantic model.
    """
    try:
        result = await run_in_threadpool(
            run_chat_turn,
            get_graph(),
            OpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
            payload.message.strip(),
            # Not $LLM_MODEL — that's extraction's model, chat wants its own
            # faster default. See graph/chat.py's run_chat_turn docstring.
            model=os.getenv("CHAT_MODEL"),
            providers=payload.providers,
            scope=access_scope_for_request(request),
            at=payload.at, as_of=payload.as_of,
        )
    except Exception as exc:
        logger.exception("Grounded chat failed")
        raise HTTPException(status_code=503, detail=f"Chat turn failed: {exc}") from exc

    return {
        "answer": result.answer,
        "citations": [
            {"recordKey": item.record_key, "name": item.name, "url": item.url}
            for item in result.citations
        ],
        "highlight": {
            "nodes": sorted(set(result.highlighted_nodes)),
            "edges": sorted(set(result.highlighted_edges)),
        },
        "readOnly": True,
        "tokenUsage": {
            "input": result.token_usage.input_tokens,
            "output": result.token_usage.output_tokens,
            "total": result.token_usage.total_tokens,
        },
    }


# After `npm run build`, FastAPI can serve the complete app on one port.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
