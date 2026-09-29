"""Aggregated Phase 6 health dashboard endpoint."""

from __future__ import annotations

import os
import re
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, Query

from connectors.core.ledger import ConnectorLedger
from graph.storage import multigraph
from graph.storage import vector_store as vector_store_module
from util.paths import DATA_DIR, ROOT

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
RESULTS_PATH = ROOT / "eval" / "results.md"


def _ledger(graph_name: str) -> ConnectorLedger:
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    return ConnectorLedger(target.ledger_path)


def _latest_eval(path: Path | None = None) -> dict | None:
    """Read the last measured markdown section without inventing metrics."""
    path = path or RESULTS_PATH
    if not path.is_file():
        return None
    text = path.read_text(errors="replace")
    sections = list(re.finditer(r"^## (.+)$", text, flags=re.MULTILINE))
    for index in range(len(sections) - 1, -1, -1):
        start = sections[index]
        end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
        body = text[start.end():end]
        metrics = {}
        for name, value in re.findall(r"^\| ([^|]+) \| ([^|]+) \|$", body, flags=re.MULTILINE):
            if name.strip().lower() == "metric":
                continue
            metrics[name.strip()] = value.strip()
        if metrics:
            return {"title": start.group(1).strip(), "metrics": metrics}
    return None


@router.get("")
async def dashboard(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    ledger = _ledger(graph_name)
    hygiene = ledger.latest_hygiene_snapshot(graph_name=graph_name)
    reviews = ledger.list_reviews(limit=1000)
    candidates = ledger.list_link_candidates(state=None, limit=1000)
    queue = Counter(row.type for row in reviews if row.state == "pending")
    queue["link_candidate"] += sum(row.state == "pending" for row in candidates)
    resolution = ledger.latest_resolution_stats()
    merges = ledger.list_merge_traces(limit=25)
    return {
        "graph_name": graph_name,
        "hygiene": [
            {
                "label": row.label,
                "isolated": row.isolated_count,
                "total": row.total_count,
                "ratio": row.isolated_count / row.total_count if row.total_count else 0.0,
                "run_id": row.run_id,
                "created_at": row.created_at,
            }
            for row in hygiene
        ],
        "queue": dict(sorted(queue.items())),
        "resolution": [
            {
                "run_id": row.run_id, "label": row.label,
                "resolved_by": row.resolved_by, "count": row.count,
                "created_at": row.created_at,
            }
            for row in resolution
        ],
        "merges": [
            {
                "id": row.id, "survivor_uid": row.survivor_uid,
                "absorbed_uid": row.absorbed_uid, "label": row.label,
                "merged_at": row.merged_at, "reason": row.reason,
            }
            for row in merges
        ],
        "latest_eval": _latest_eval(),
    }
