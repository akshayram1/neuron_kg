"""Delete a source's full data footprint: its FalkorDB graph and whatever
the bridge resolver recorded about it. Each connector's own ledger tables
are deleted separately (their schemas differ per provider); this module only
owns the two things every provider shares.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from falkordb import FalkorDB

from graph.bridge.resolver import get_default_store


def delete_falkordb_group(group_id: str) -> bool:
    client = FalkorDB(
        host=os.getenv("FALKOR_HOST", "localhost"),
        port=int(os.getenv("FALKOR_PORT", "6379")),
    )
    if group_id not in client.list_graphs():
        return False
    client.select_graph(group_id).delete()
    return True


def purge_groups(group_ids: list[str]) -> dict[str, object]:
    """Delete each group's FalkorDB graph and bridge footprint. Safe to call
    with group_ids that were never actually synced (e.g. a connection with
    no sources yet) -- each step is a no-op when there is nothing to delete.
    """
    deleted_graphs: list[str] = []
    links_removed = records_removed = 0
    store = get_default_store()
    for group_id in group_ids:
        if delete_falkordb_group(group_id):
            deleted_graphs.append(group_id)
        links, records = store.purge_group(group_id)
        links_removed += links
        records_removed += records
    return {
        "falkordb_graphs_deleted": deleted_graphs,
        "bridge_links_removed": links_removed,
        "bridge_records_removed": records_removed,
    }


def purge_ledger_prefix(ledger_path: str | Path, record_key_prefix: str) -> int:
    """For the shared connectors.core.ledger.ConnectorLedger (Jira/Bitbucket):
    delete every row whose record_key starts with the given prefix, e.g.
    'jira:<connection_id>:'. Returns the number of source_records removed."""
    path = Path(ledger_path)
    if not path.exists():
        return 0
    with sqlite3.connect(path) as db:
        db.execute(
            "DELETE FROM source_chunks WHERE record_key LIKE ? ESCAPE '\\'",
            (record_key_prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
        )
        removed = db.execute(
            "DELETE FROM source_records WHERE record_key LIKE ? ESCAPE '\\'",
            (record_key_prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
        ).rowcount
        db.commit()
    return removed
