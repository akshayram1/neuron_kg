"""Durable ledger for provider-independent record state.

State is committed only after the graph write succeeds, so a failed run
safely retries the same source version. This has no Graphiti dependency —
`primary_node_uid` and `record_edges` reference `graph.writer`'s uid/edge
identity scheme directly (plan.md §2.2, §7 Block 4).

Two things beyond simple hash-based KEEP/INSERT/UPDATE/DELETE live here:

- `semantic_status` — the LLM enrichment pass (plan.md §3 Pass B) is budgeted
  per run, so a record's deterministic pass can finish while its semantic pass
  stays 'pending' until a later run has budget. `source_chunks` tracks this at
  chunk granularity so a partially-processed record resumes instead of
  restarting.
- `record_edges` — a side-index of which fact edges a record supports, so
  deletion/invalidation can look up "what does this record_key support"
  directly instead of scanning the graph for a `record_key` inside every
  edge's `source_record_keys` array (plan.md §4.1 risk: unconfirmed whether
  the installed FalkorDB version indexes array-membership on relationships).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from connectors.core.actions import RecordAction, resolve_action


class SemanticStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    NOT_APPLICABLE = "not_applicable"  # record has no free text worth an LLM pass


@dataclass(frozen=True)
class LedgerEntry:
    record_key: str
    content_hash: str
    primary_node_uid: str | None
    semantic_status: str
    semantic_priority: int
    update_count: int
    updated_at: str


@dataclass(frozen=True)
class RecordEdgeRef:
    rel_type: str
    from_uid: str
    to_uid: str


@dataclass(frozen=True)
class PendingChunk:
    record_key: str
    chunk_id: str
    chunk_index: int
    text: str


class ConnectorLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_records (
                    record_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL,
                    primary_node_uid TEXT,
                    semantic_status TEXT NOT NULL DEFAULT 'pending',
                    semantic_priority INTEGER NOT NULL DEFAULT 100,
                    update_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(source_records)")
            }
            if "semantic_priority" not in columns:
                connection.execute(
                    "ALTER TABLE source_records ADD COLUMN semantic_priority INTEGER NOT NULL DEFAULT 100"
                )
            if "update_count" not in columns:
                connection.execute(
                    "ALTER TABLE source_records ADD COLUMN update_count INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_records_semantic_priority "
                "ON source_records(semantic_status, semantic_priority DESC, updated_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_records_semantic_status "
                "ON source_records(semantic_status, updated_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_chunks (
                    record_key TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY(record_key, chunk_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_chunks_status "
                "ON source_chunks(status, committed_at)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS record_edges (
                    record_key TEXT NOT NULL,
                    rel_type TEXT NOT NULL,
                    from_uid TEXT NOT NULL,
                    to_uid TEXT NOT NULL,
                    PRIMARY KEY(record_key, rel_type, from_uid, to_uid)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_record_edges_key ON record_edges(record_key)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    # ------------------------------------------------------------ hash / action

    def get(self, record_key: str) -> LedgerEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_records WHERE record_key = ?", (record_key,)
            ).fetchone()
        return LedgerEntry(**dict(row)) if row else None

    def plan(self, record_key: str, current_hash: str | None, *, deleted: bool = False) -> RecordAction:
        previous = self.get(record_key)
        return resolve_action(
            previous.content_hash if previous else None,
            current_hash,
            deleted=deleted,
        )

    def commit(
        self,
        record_key: str,
        content_hash: str,
        *,
        primary_node_uid: str | None = None,
        semantic_status: SemanticStatus | str = SemanticStatus.PENDING,
    ) -> None:
        """Record a successful deterministic write. The caller decides
        `semantic_status`: PENDING if this record has free text worth an LLM
        pass, NOT_APPLICABLE if it's purely structural (e.g. a container
        record with no prose), or DONE if the semantic pass ran in the same
        step (no budget contention)."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_records(
                    record_key, content_hash, primary_node_uid, semantic_status,
                    semantic_priority, update_count, updated_at
                )
                VALUES (?, ?, ?, ?, 100, 0, ?)
                ON CONFLICT(record_key) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    primary_node_uid = excluded.primary_node_uid,
                    semantic_status = excluded.semantic_status,
                    update_count = source_records.update_count + 1,
                    semantic_priority = min(300, 200 + source_records.update_count),
                    updated_at = excluded.updated_at
                """,
                (record_key, content_hash, primary_node_uid, str(semantic_status), datetime.now(UTC).isoformat()),
            )

    # ------------------------------------------------------------ semantic queue

    def set_semantic_status(self, record_key: str, status: SemanticStatus | str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_records SET semantic_status = ? WHERE record_key = ?",
                (str(status), record_key),
            )

    def pending_semantic_records(self, limit: int) -> list[str]:
        """Oldest-updated pending records first, so a persistent backlog
        doesn't starve any one record forever across budget-capped runs
        (plan.md §3 Pass B, §4 LLM budget)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_key FROM source_records "
                "WHERE semantic_status = ? "
                "ORDER BY semantic_priority DESC, updated_at ASC LIMIT ?",
                (str(SemanticStatus.PENDING), limit),
            ).fetchall()
        return [str(row["record_key"]) for row in rows]

    def chunk_status(self, record_key: str, chunk_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM source_chunks WHERE record_key = ? AND chunk_id = ?",
                (record_key, chunk_id),
            ).fetchone()
        return str(row["status"]) if row else None

    def save_chunks(self, record_key: str, chunks: list[tuple[str, int, str]]) -> None:
        """Persist chunk text as 'pending' so the semantic pass can resume
        across runs without re-fetching from the provider or re-chunking
        (plan.md §3 Pass B). Called by the deterministic pass right after it
        computes chunks for an INSERT/UPDATE record. chunks: (chunk_id,
        chunk_index, text) tuples. Replaces any prior chunk set for this
        record_key — an UPDATE's new content_hash means the old chunks are
        stale."""
        with self._connect() as connection:
            connection.execute("DELETE FROM source_chunks WHERE record_key = ?", (record_key,))
            if chunks:
                now = datetime.now(UTC).isoformat()
                connection.executemany(
                    """
                    INSERT INTO source_chunks(record_key, chunk_id, chunk_index, text, status, committed_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    [(record_key, chunk_id, index, text, now) for chunk_id, index, text in chunks],
                )

    def pending_chunks(self, limit: int, record_prefix: str | None = None) -> list[PendingChunk]:
        """Oldest-first, across all records — this is the actual LLM-call
        budget unit (plan.md §4 LLM_BUDGET_PER_RUN), not the record count,
        since one record can hold several chunks."""
        with self._connect() as connection:
            if record_prefix:
                escaped = record_prefix.replace("%", "\\%").replace("_", "\\_") + "%"
                rows = connection.execute(
                    "SELECT c.record_key, c.chunk_id, c.chunk_index, c.text "
                    "FROM source_chunks c JOIN source_records s ON s.record_key = c.record_key "
                    "WHERE c.status = 'pending' AND c.record_key LIKE ? ESCAPE '\\' "
                    "ORDER BY s.semantic_priority DESC, c.committed_at ASC, c.chunk_index ASC LIMIT ?",
                    (escaped, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT c.record_key, c.chunk_id, c.chunk_index, c.text "
                    "FROM source_chunks c JOIN source_records s ON s.record_key = c.record_key "
                    "WHERE c.status = 'pending' "
                    "ORDER BY s.semantic_priority DESC, c.committed_at ASC, c.chunk_index ASC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [PendingChunk(row["record_key"], row["chunk_id"], row["chunk_index"], row["text"]) for row in rows]

    def commit_chunk(self, record_key: str, chunk_id: str, status: SemanticStatus | str = SemanticStatus.DONE) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_chunks SET status = ?, committed_at = ? WHERE record_key = ? AND chunk_id = ?",
                (str(status), datetime.now(UTC).isoformat(), record_key, chunk_id),
            )

    def record_fully_processed(self, record_key: str) -> bool:
        """True once every chunk saved for this record has status='done' (or
        it has no chunks at all). Used to flip `semantic_status` to DONE only
        when nothing is left pending — a record with 3 chunks where the
        budget only covered 2 must stay PENDING."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM source_chunks WHERE record_key = ? AND status != 'done'",
                (record_key,),
            ).fetchone()
        return int(row["n"]) == 0

    # ------------------------------------------------------------ edge side-index

    def record_edge(self, record_key: str, rel_type: str, from_uid: str, to_uid: str) -> None:
        self.record_edges_batch(record_key, [RecordEdgeRef(rel_type, from_uid, to_uid)])

    def record_edges_batch(self, record_key: str, edges: list[RecordEdgeRef]) -> None:
        if not edges:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO record_edges(record_key, rel_type, from_uid, to_uid)
                VALUES (?, ?, ?, ?)
                """,
                [(record_key, e.rel_type, e.from_uid, e.to_uid) for e in edges],
            )

    def edges_for_record(self, record_key: str) -> list[RecordEdgeRef]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT rel_type, from_uid, to_uid FROM record_edges WHERE record_key = ?",
                (record_key,),
            ).fetchall()
        return [RecordEdgeRef(row["rel_type"], row["from_uid"], row["to_uid"]) for row in rows]

    def clear_edges(self, record_key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM record_edges WHERE record_key = ?", (record_key,))

    # ------------------------------------------------------------ deletion

    def record_keys_with_prefix(self, prefix: str) -> list[str]:
        """e.g. `record_keys_with_prefix('jira:<connection_id>:')` — every
        record a connection wrote, for bulk deletion when it's disconnected."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_key FROM source_records WHERE record_key LIKE ? ESCAPE '\\'",
                (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
        return [str(row["record_key"]) for row in rows]

    def commit_delete(self, record_key: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM source_chunks WHERE record_key = ?", (record_key,))
            connection.execute("DELETE FROM record_edges WHERE record_key = ?", (record_key,))
            connection.execute("DELETE FROM source_records WHERE record_key = ?", (record_key,))
