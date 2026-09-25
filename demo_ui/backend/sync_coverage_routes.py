"""Sync coverage read endpoint (25-plan.md Phase 0 §"Sync coverage report").

Every connector sync orchestrator (jira_routes.py, bitbucket_routes.py,
github_routes.py, notion_routes.py) logs one `sync_coverage` row to the
connector ledger at the end of a completed run via
`ConnectorLedger.record_sync_coverage` -- provider-reported total (when the
API gives one), fetched count, ledger count (re-read from the ledger, not
just trusted from the in-memory loop), and skipped-by-rule count. This
module is the read side: one small endpoint per graph, scoped the same way
every other connector `/status` endpoint is.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Query

from connectors.core.ledger import ConnectorLedger, SyncCoverage
from graph import multigraph
from graph import vector_store as vector_store_module
from util.paths import DATA_DIR

router = APIRouter(prefix="/api/sync-coverage", tags=["sync-coverage"])


def _ledger(graph_name: str) -> ConnectorLedger:
    target = multigraph.resolve(
        graph_name, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=vector_store_module.COLLECTION,
    )
    return ConnectorLedger(target.ledger_path)


def _as_dict(row: SyncCoverage) -> dict:
    return {
        "run_id": row.run_id,
        "provider": row.provider,
        "connection_id": row.connection_id,
        "provider_reported_total": row.provider_reported_total,
        "fetched_count": row.fetched_count,
        "ledger_count": row.ledger_count,
        "skipped_by_rule_count": row.skipped_by_rule_count,
        "created_at": row.created_at,
    }


@router.get("")
async def latest_coverage(
    graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
    provider: str | None = Query(default=None, max_length=50),
) -> dict:
    """Latest coverage row per provider for this graph (or just `provider`,
    if given). This is the endpoint `SyncProgress.tsx` reads once a sync is
    no longer actively running, since the live `progress` prop only carries
    the numbers for the run currently in flight."""
    rows = _ledger(graph_name).latest_sync_coverage(provider)
    return {"coverage": [_as_dict(row) for row in rows]}


@router.get("/{run_id}")
async def coverage_for_run(
    run_id: str, graph_name: str = Query(default=multigraph.DEFAULT_GRAPH_NAME),
) -> dict:
    row = _ledger(graph_name).sync_coverage_for_run(run_id)
    return {"coverage": _as_dict(row) if row else None}
