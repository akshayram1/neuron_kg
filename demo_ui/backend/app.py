"""FastAPI adapter for the React demo (plan.md §6a.1).

Ported from `graphiti_context_explorer/demo_ui/backend/app.py`. Scoped down
to what's actually built so far (Jira plus grounded graph chat):

  - `/api/chat` uses this repo's native hybrid BM25+vector+RRF search and
    returns citations plus the exact graph path used by the answer.
  - `github_router`/`notion_router`/`sharepoint_router`/`bitbucket_router`/
    `bridge_router` — not included; those connectors and the cross-source
    bridge are later phases (plan.md §8).
  - MCP block in `/api/config` — dropped; no MCP server in this project
    (plan.md §6a.2).

Run from the repository root:

    uv run uvicorn demo_ui.backend.app:app --reload --port 8000
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

from demo_ui.backend.github_routes import router as github_router
from demo_ui.backend.jira_routes import router as jira_router
from demo_ui.backend.notion_routes import router as notion_router
from graph.chat import run_chat_turn
from graph.falkor_client import get_graph
from graph.graph_view import fetch_graph, fetch_sources
from graph.schema import bootstrap_schema
from graph.skos_export import build_skos_turtle
from util import paths as _paths  # noqa: F401 — load the repo-root .env
from util.logging import configure_logging

configure_logging()

logger = logging.getLogger("uvicorn.error.neuron")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8_000)
    providers: list[str] = Field(default_factory=lambda: ["jira", "github", "notion"])

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
app.include_router(notion_router)


@app.middleware("http")
async def prevent_stale_frontend_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/index.html"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.on_event("startup")
async def _ensure_schema() -> None:
    bootstrap_schema(get_graph())


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "neuron"}


@app.get("/api/config")
async def config() -> dict:
    providers = ["jira", "github", "notion"]
    return {"providers": providers, "defaultProviders": providers}


@app.get("/api/graph")
async def graph(providers: list[str] | None = Query(default=None)) -> dict:
    try:
        view = fetch_graph(get_graph(), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    return {"groups": providers or ["jira", "github", "notion"], **view}


@app.get("/api/sources")
async def sources() -> dict:
    return {"sources": fetch_sources(get_graph())}


@app.get("/api/export/skos")
async def export_skos(providers: list[str] | None = Query(default=None)) -> PlainTextResponse:
    """Download the graph as SKOS RDF (Turtle) — plan.md §6b.1."""
    try:
        view = fetch_graph(get_graph(), providers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not load FalkorDB graph: {exc}") from exc
    turtle = build_skos_turtle(view["nodes"], view["edges"])
    filename = "context-graph-" + "-".join(providers or ["jira", "github", "notion"])[:60] + ".ttl"
    return PlainTextResponse(
        turtle, media_type="text/turtle", headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict:
    """Answer from the live graph and return the exact canvas path used."""
    try:
        result = await run_in_threadpool(
            run_chat_turn,
            get_graph(),
            OpenAI(api_key=os.environ.get("OPENAI_API_KEY")),
            request.message.strip(),
            model=os.getenv("LLM_MODEL", "gpt-5.6-sol"),
            providers=request.providers,
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
    }


# After `npm run build`, FastAPI can serve the complete app on one port.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
