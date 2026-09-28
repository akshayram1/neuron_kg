"""Delete a source's full data footprint: its FalkorDB graph and whatever
the bridge resolver recorded about it. Each connector's own ledger tables
are deleted separately (their schemas differ per provider); this module only
owns the two things every provider shares.
"""

from __future__ import annotations

import os
from pathlib import Path

from falkordb import FalkorDB

from storage import sql_backend


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
    # Imported lazily: graph.bridge.resolver no longer exists in this tree, and
    # a module-level import made purge_ledger_prefix unimportable too.
    from graph.bridge.resolver import get_default_store

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
    'jira:<connection_id>:'. Returns the number of source_records removed.

    Matching is ASCII case-insensitive on both backends (SQLite's LIKE
    semantics), with ``%``/``_`` in the prefix escaped so the prefix is always
    literal -- the delete never reaches beyond that one prefix."""
    if not record_key_prefix:
        raise ValueError("record_key_prefix must be non-empty")
    path = Path(ledger_path)
    if sql_backend.backend() == "sqlite" and not path.exists():
        return 0
    pattern = (
        record_key_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    ).lower()
    db = sql_backend.connect(path)
    try:
        if not sql_backend.table_columns(db, "source_records"):
            return 0
        with db:
            if sql_backend.table_columns(db, "source_chunks"):
                db.execute(
                    "DELETE FROM source_chunks WHERE LOWER(record_key) LIKE ? ESCAPE '\\'",
                    (pattern,),
                )
            removed = db.execute(
                "DELETE FROM source_records WHERE LOWER(record_key) LIKE ? ESCAPE '\\'",
                (pattern,),
            ).rowcount
    finally:
        db.close()
    return removed
