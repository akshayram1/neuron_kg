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
from graph import multigraph
from graph import vector_store
from graph.chat import run_chat_turn
from graph import adoption
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
GRAPH_REGISTRY = multigraph.GraphRegistry(DATA_DIR / "graphs.sqlite3")


def _resolve(graph_name: str) -> multigraph.GraphTarget:
    return multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store.COLLECTION,
    )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    providers: list[str] = Field(default_factory=lambda: ["jira", "github", "bitbucket", "notion"])
    at: str | None = None
    as_of: str | None = None
    graph_name: str = Field(default=multigraph.DEFAULT_GRAPH_NAME, max_length=40)


class GraphCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    display_name: str | None = Field(default=None, max_length=100)

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


@app.get("/api/graphs")
async def list_graphs() -> dict:
    return {"graphs": GRAPH_REGISTRY.list()}


@app.post("/api/graphs", status_code=201)
async def create_graph(payload: GraphCreateRequest) -> dict:
    """Register a new named graph and pre-create its FalkorDB indexes +
    Qdrant collection, so the very first sync into it isn't the thing that
    creates them (and the UI can show it as an existing, empty graph
    immediately, before any data has been synced)."""
    try:
        GRAPH_REGISTRY.create(payload.name, payload.display_name)
    except multigraph.InvalidGraphName as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    target = _resolve(payload.name)
    bootstrap_schema(get_graph(name=target.falkor_name))
    vector_store.ensure_collection(vector_store.client(), collection=target.qdrant_collection)
    return {"name": target.name}


@app.get("/api/graph")
async def graph(
    request: Request, providers: list[str] | None = Query(default=None),
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    target = _resolve(graph_name)
    try:
        view = fetch_graph(get_graph(name=target.falkor_name), access_scope_for_request(request), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    return {"groups": providers or ["jira", "github", "bitbucket", "notion"], **view}


@app.get("/api/sources")
async def sources(
    request: Request, graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    target = _resolve(graph_name)
    return {"sources": fetch_sources(get_graph(name=target.falkor_name), access_scope_for_request(request))}


@app.get("/api/entities/{uid}")
async def entity_detail(
    uid: str, request: Request,
    at: str | None = Query(default=None),
    at_end: str | None = Query(default=None),
    as_of: str | None = Query(default=None),
    providers: list[str] | None = Query(default=None),
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    """Relations (current + past), record-axis history, and derived proofs."""
    target = _resolve(graph_name)
    try:
        detail = fetch_entity_detail(
            get_graph(name=target.falkor_name), access_scope_for_request(request), uid,
            at=at, at_end=at_end, as_of=as_of, providers=providers,
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
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    """Audit a fact or select business-time / transaction-time state."""
    target = _resolve(graph_name)
    try:
        intervals = fetch_fact_history(
            get_graph(name=target.falkor_name), access_scope_for_request(request), fact_uid,
            valid_at=valid_at, observed_at=observed_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="valid_at and observed_at must be ISO-8601 timestamps") from exc
    return {"factUid": fact_uid, "intervals": intervals}


@app.get("/api/export/skos")
async def export_skos(
    request: Request, providers: list[str] | None = Query(default=None),
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> PlainTextResponse:
    """Download the graph as SKOS RDF (Turtle) — plan.md §6b.1."""
    target = _resolve(graph_name)
    try:
        view = fetch_graph(get_graph(name=target.falkor_name), access_scope_for_request(request), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    turtle = build_skos_turtle(view["nodes"], view["edges"])
    filename = "context-graph-" + "-".join(providers or ["jira", "github", "bitbucket", "notion"])[:60] + ".ttl"
    return PlainTextResponse(
        turtle, media_type="text/turtle", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/admin/clear-graph")
async def clear_graph(graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME)) -> dict:
    """Wipe ONE named graph's knowledge graph and vector projection for a
    fresh sync.

    Deliberately scoped to exactly the requested graph_name's FalkorDB graph
    and Qdrant collection -- every other graph (and other graphs sharing this
    Redis instance, like a `GRAPH.COPY` backup or the old
    graphiti_context_explorer project's graphs) is untouched. This used to be
    an unconditional global wipe; it was scoped per-graph specifically
    because a single-provider cleanup once destroyed unrelated, already-
    completed work in a different provider's data -- see the
    `feedback_scoped_cleanup` project memory. Also clears that graph's own
    ledger file: without that, every connector would see an unchanged
    content hash on the next sync and skip writing anything back into the
    now-empty graph. OAuth connections (Jira/GitHub/Notion/Bitbucket) are
    never touched here -- they are shared across every graph, not owned by
    any one of them.
    """
    target = _resolve(graph_name)
    graph = get_graph(name=target.falkor_name)
    before = graph.query("MATCH (n) RETURN count(n)").result_set[0][0]
    graph.delete()
    bootstrap_schema(get_graph(name=target.falkor_name))

    client = vector_store.client()
    if client.collection_exists(target.qdrant_collection):
        client.delete_collection(target.qdrant_collection)
    vector_store.ensure_collection(client, collection=target.qdrant_collection)

    ledger = ConnectorLedger(target.ledger_path)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DELETE FROM source_records")
        db.execute("DELETE FROM source_chunks")
        db.execute("DELETE FROM record_edges")

    logger.info(
        "cleared graph %r: %s nodes removed, its ledger and vector collection reset",
        target.name, before,
    )
    return {"cleared": True, "nodes_removed": before, "graph": target.name}



# --------------------------------------------------------- ontology adoption
#
# After a sync, the refused facts are the one thing the user cannot see: the
# graph looks complete because what is missing was never written. These
# endpoints surface that, and make widening the vocabulary a decision rather
# than a SQL statement.


class AdoptRequest(BaseModel):
    graph_name: str = Field(default=multigraph.DEFAULT_GRAPH_NAME, max_length=40)
    min_docs: int = Field(default=adoption.MIN_DOCS, ge=1, le=50)
    min_facts: int = Field(default=adoption.MIN_FACTS, ge=1, le=1000)


class AutoExtendRequest(BaseModel):
    graph_name: str = Field(default=multigraph.DEFAULT_GRAPH_NAME, max_length=40)
    enabled: bool


@app.get("/api/ontology/pending")
async def ontology_pending(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
    min_docs: int = Query(default=adoption.MIN_DOCS, ge=1, le=50),
    min_facts: int = Query(default=adoption.MIN_FACTS, ge=1, le=1000),
) -> dict:
    """What this sync left out of the graph, and what adopting would recover."""
    ledger = ConnectorLedger(_resolve(graph_name).ledger_path)
    return adoption.pending_report(ledger, min_docs=min_docs, min_facts=min_facts)


@app.get("/api/ontology/adoptions")
async def ontology_adoptions(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
    include_undone: bool = Query(default=False),
) -> dict:
    target = _resolve(graph_name)
    return {"adoptions": adoption.adoptions_with_edges(
        get_graph(name=target.falkor_name), ConnectorLedger(target.ledger_path),
        include_undone=include_undone,
    )}


@app.post("/api/ontology/adopt")
async def ontology_adopt(payload: AdoptRequest) -> dict:
    """Adopt every eligible shape as one revertible batch. Explicit, so it
    runs whatever the switch says."""
    ledger = ConnectorLedger(_resolve(payload.graph_name).ledger_path)
    result = adoption.adopt(
        ledger, min_docs=payload.min_docs, min_facts=payload.min_facts, force=True,
    )
    return {
        "adopted": result.adopted, "batch_id": result.batch_id,
        "shapes": result.shapes, "facts_expected": result.facts_expected,
        "chunks_requeued": result.chunks_requeued,
        "skipped_reason": result.skipped_reason,
        # The chunks are queued, not extracted. Saying so here stops the UI
        # from reporting a recovery that has not happened yet.
        "note": "re-run the sync to extract the requeued chunks",
    }


@app.post("/api/ontology/unadopt/{batch_id}")
async def ontology_unadopt(
    batch_id: str,
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    target = _resolve(graph_name)
    ledger = ConnectorLedger(target.ledger_path)
    return adoption.unadopt(get_graph(name=target.falkor_name), ledger, batch_id)


@app.post("/api/ontology/auto-extend")
async def ontology_auto_extend(payload: AutoExtendRequest) -> dict:
    ledger = ConnectorLedger(_resolve(payload.graph_name).ledger_path)
    adoption.set_auto_extend(ledger, payload.enabled)
    return {"auto_extend": adoption.auto_extend_enabled(ledger)}


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request) -> dict:
    """Answer from the live graph and return the exact canvas path used.

    Takes both the parsed body (`payload`) and the raw `Request`: the ACL
    scope is derived from session cookies, which only exist on the latter.
    Naming the body `request` shadowed it and made `access_scope_for_request`
    read `.cookies` off a Pydantic model.
    """
    target = _resolve(payload.graph_name)
    try:
        result = await run_in_threadpool(
            run_chat_turn,
            get_graph(name=target.falkor_name),
            OpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
            payload.message.strip(),
            # Not $LLM_MODEL — that's extraction's model, chat wants its own
            # faster default. See graph/chat.py's run_chat_turn docstring.
            model=os.getenv("CHAT_MODEL"),
            providers=payload.providers,
            scope=access_scope_for_request(request),
            at=payload.at, as_of=payload.as_of,
            collection=target.qdrant_collection,
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
