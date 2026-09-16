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

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

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


class DropReason(StrEnum):
    """Why one extracted item did not become a fact. `DIRECTION_CORRECTED` is
    deliberately in this vocabulary while NOT being a loss — the fact was
    written, with its endpoints swapped to match the ontology. It is recorded
    here so that correction can never happen silently."""

    RELATION_NOT_ALLOWED = "relation_not_allowed"
    EVIDENCE_NOT_IN_CHUNK = "evidence_not_in_chunk"
    ENDPOINT_UNRESOLVED = "endpoint_unresolved"
    ENTITY_NO_CONNECTING_FACT = "entity_no_connecting_fact"
    DIRECTION_CORRECTED = "direction_corrected"


@dataclass(frozen=True)
class ExtractionDrop:
    reason: str
    subject_kind: str | None = None
    subject_name: str | None = None
    relation: str | None = None
    object_kind: str | None = None
    object_name: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ChunkDiff:
    """What one `save_chunks` call actually changed. `reused_done` is the
    number of chunks that kept a completed extraction — i.e. the LLM calls
    this re-ingestion did NOT have to make."""

    kept: int
    added: int
    superseded: int
    reused_done: int


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
            chunk_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(source_chunks)")
            }
            if "superseded_at" not in chunk_columns:
                # Soft supersession rather than DELETE: a chunk that vanished
                # from the current version is still the evidence a fact was
                # extracted from, and deleting it makes that fact
                # unexplainable rather than merely stale.
                connection.execute("ALTER TABLE source_chunks ADD COLUMN superseded_at TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_chunks_status "
                "ON source_chunks(status, committed_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_chunks_live "
                "ON source_chunks(record_key) WHERE superseded_at IS NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS record_versions (
                    record_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY(record_key, version)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_record_versions_hash "
                "ON record_versions(content_hash)"
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS extraction_drops (
                    record_key TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    subject_kind TEXT,
                    subject_name TEXT,
                    relation TEXT,
                    object_kind TEXT,
                    object_name TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_extraction_drops_reason "
                "ON extraction_drops(reason, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_extraction_drops_chunk "
                "ON extraction_drops(record_key, chunk_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS relation_axioms (
                    relation TEXT NOT NULL,
                    subject_kind TEXT NOT NULL,
                    object_kind TEXT NOT NULL,
                    extractable INTEGER NOT NULL DEFAULT 0,
                    functional INTEGER NOT NULL DEFAULT 0,
                    is_transitive INTEGER NOT NULL DEFAULT 0,
                    is_symmetric INTEGER NOT NULL DEFAULT 0,
                    is_asymmetric INTEGER NOT NULL DEFAULT 0,
                    inverse_of TEXT,
                    sub_property_of TEXT,
                    temporal TEXT NOT NULL DEFAULT 'state',
                    PRIMARY KEY(relation, subject_kind, object_kind)
                )
                """
            )
            axiom_columns = {
                str(row["name"]) for row in
                connection.execute("PRAGMA table_info(relation_axioms)").fetchall()
            }
            if "adopted_batch" not in axiom_columns:
                connection.execute("ALTER TABLE relation_axioms ADD COLUMN adopted_batch TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS axiom_adoptions (
                    batch_id       TEXT PRIMARY KEY,
                    adopted_at     TEXT NOT NULL,
                    shapes         TEXT NOT NULL,   -- JSON list of adopted triples
                    facts_expected INTEGER NOT NULL DEFAULT 0,
                    min_docs       INTEGER NOT NULL DEFAULT 0,
                    undone_at      TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS graph_settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ontology_misses (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    example TEXT,
                    count INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    dismissed_at TEXT,
                    PRIMARY KEY(kind, key)
                )
                """
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
        # Append-only version history. Keyed on (record_key, version) with
        # INSERT OR IGNORE, so re-committing the same content is a no-op
        # rather than an invented version.
        self.record_version(record_key, content_hash)

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

    def save_chunks(
        self, record_key: str, chunks: list[tuple[str, int, str]],
        adopt_from: str | None = None,
    ) -> ChunkDiff:
        """Persist this version's chunks, reusing every chunk whose text did
        not change (plan.md §3 Pass B).

        This used to be `DELETE FROM source_chunks WHERE record_key = ?`
        followed by re-inserting everything as 'pending', which meant one new
        comment on a Jira ticket or one edited paragraph in a Notion page
        re-extracted the WHOLE record through the LLM. `chunk_id` is already
        content-addressed -- `uuid5(record_key:sha256(text):occurrence)` in
        `connectors/core/chunking/router.py` -- so an unchanged chunk keeps
        its id across versions, and "did this text change" is a set
        comparison, not a diff algorithm.

        So: ids present in both versions are left exactly as they are
        (a 'done' chunk stays done and is never re-extracted), genuinely new
        ids are inserted as 'pending', and ids that vanished are marked
        `superseded_at` rather than deleted, because a fact extracted from
        them still points at them as its evidence.

        `adopt_from` handles a rename. `chunk_id` is namespaced by
        `record_key`, so moving a file changes every chunk id even when not
        one character of the text changed -- which would send a whole
        renamed document back through the LLM. Given the predecessor's key
        (see `find_moved_from`), chunks whose TEXT matches an
        already-extracted chunk there are inserted as 'done'.
        """
        now = datetime.now(UTC).isoformat()
        incoming = {chunk_id: (index, text) for chunk_id, index, text in chunks}
        with self._connect() as connection:
            existing = {
                str(row["chunk_id"]): str(row["status"])
                for row in connection.execute(
                    "SELECT chunk_id, status FROM source_chunks "
                    "WHERE record_key = ? AND superseded_at IS NULL",
                    (record_key,),
                )
            }
            adopted_by_text: dict[str, str] = {}
            if adopt_from:
                adopted_by_text = {
                    str(row["text"]): str(row["status"])
                    for row in connection.execute(
                        "SELECT text, status FROM source_chunks "
                        "WHERE record_key = ? AND superseded_at IS NULL AND status = 'done'",
                        (adopt_from,),
                    )
                }
            kept = sorted(set(existing) & set(incoming))
            added = sorted(set(incoming) - set(existing))
            superseded = sorted(set(existing) - set(incoming))

            # Position can shift even when text does not (a paragraph inserted
            # above it), and `chunk_index` only drives ordering, never identity.
            connection.executemany(
                "UPDATE source_chunks SET chunk_index = ? WHERE record_key = ? AND chunk_id = ?",
                [(incoming[chunk_id][0], record_key, chunk_id) for chunk_id in kept],
            )
            connection.executemany(
                "INSERT INTO source_chunks(record_key, chunk_id, chunk_index, text, status, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (record_key, chunk_id, incoming[chunk_id][0], incoming[chunk_id][1],
                     adopted_by_text.get(incoming[chunk_id][1], "pending"), now)
                    for chunk_id in added
                ],
            )
            connection.executemany(
                "UPDATE source_chunks SET superseded_at = ? WHERE record_key = ? AND chunk_id = ?",
                [(now, record_key, chunk_id) for chunk_id in superseded],
            )
        return ChunkDiff(
            kept=len(kept), added=len(added), superseded=len(superseded),
            reused_done=(
                sum(1 for chunk_id in kept if existing[chunk_id] == str(SemanticStatus.DONE))
                + sum(1 for chunk_id in added if incoming[chunk_id][1] in adopted_by_text)
            ),
        )

    def record_version(self, record_key: str, content_hash: str) -> int:
        """Append one row per ingested version and return its number.

        The ledger previously kept only `update_count` -- it could say a
        record had changed five times but not what any earlier version was,
        so "when did this text arrive" had no answer.

        Idempotent on CONTENT, not just on the primary key: re-committing
        identical bytes returns the existing version rather than inventing a
        new one, so the version number counts real changes."""
        with self._connect() as connection:
            latest = connection.execute(
                "SELECT version, content_hash FROM record_versions "
                "WHERE record_key = ? ORDER BY version DESC LIMIT 1",
                (record_key,),
            ).fetchone()
            if latest and str(latest["content_hash"]) == content_hash:
                return int(latest["version"])
            version = int(latest["version"]) + 1 if latest else 1
            connection.execute(
                "INSERT OR IGNORE INTO record_versions(record_key, version, content_hash, ingested_at) "
                "VALUES (?, ?, ?, ?)",
                (record_key, version, content_hash, datetime.now(UTC).isoformat()),
            )
        return version

    def versions(self, record_key: str) -> list[tuple[int, str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version, content_hash, ingested_at FROM record_versions "
                "WHERE record_key = ? ORDER BY version",
                (record_key,),
            ).fetchall()
        return [(int(r["version"]), str(r["content_hash"]), str(r["ingested_at"])) for r in rows]

    def find_moved_from(self, record_key: str, content_hash: str) -> LedgerEntry | None:
        """A record with this exact content already ingested under a DIFFERENT
        key in the same provider+connection -- i.e. a rename.

        Neuron's `record_key` embeds the path (`…:source_file:{repo}:{path}`),
        so renaming a file produces an INSERT plus a DELETE and every derived
        artifact is recomputed from scratch. Identifying the predecessor lets
        the caller carry over what does not depend on the path (its
        embedding) instead of paying to regenerate identical bytes.
        """
        prefix = ":".join(record_key.split(":", 2)[:2]) + ":"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_records WHERE content_hash = ? AND record_key != ? "
                "AND record_key LIKE ? ESCAPE '\\' LIMIT 1",
                (content_hash, record_key,
                 prefix.replace("%", "\\%").replace("_", "\\_") + "%"),
            ).fetchone()
        return LedgerEntry(**dict(row)) if row else None

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
                    "WHERE c.status = 'pending' AND c.superseded_at IS NULL AND c.record_key LIKE ? ESCAPE '\\' "
                    "ORDER BY s.semantic_priority DESC, c.committed_at ASC, c.chunk_index ASC LIMIT ?",
                    (escaped, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT c.record_key, c.chunk_id, c.chunk_index, c.text "
                    "FROM source_chunks c JOIN source_records s ON s.record_key = c.record_key "
                    "WHERE c.status = 'pending' AND c.superseded_at IS NULL "
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
                "SELECT COUNT(*) AS n FROM source_chunks "
                "WHERE record_key = ? AND status != 'done' AND superseded_at IS NULL",
                (record_key,),
            ).fetchone()
        return int(row["n"]) == 0

    # ------------------------------------------------------------ extraction drops

    def record_drops(self, record_key: str, chunk_id: str, drops: list[ExtractionDrop]) -> None:
        """Persist what an extraction threw away, and why.

        The semantic pass had five paths that discarded an extracted item;
        three bumped a counter and two were entirely silent, and none of them
        survived the log line they were written to. A count with no reason
        cannot answer "is the ontology too narrow, or is the model wrong",
        which is the only question worth asking about a drop.

        Rewritten per (record_key, chunk_id) rather than appended, so the
        table stays a current picture of one extraction rather than an
        ever-growing log that double-counts every re-run.
        """
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM extraction_drops WHERE record_key = ? AND chunk_id = ?",
                (record_key, chunk_id),
            )
            connection.executemany(
                "INSERT INTO extraction_drops(record_key, chunk_id, reason, subject_kind, "
                "subject_name, relation, object_kind, object_name, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (record_key, chunk_id, drop.reason, drop.subject_kind, drop.subject_name,
                     drop.relation, drop.object_kind, drop.object_name, drop.detail, now)
                    for drop in drops
                ],
            )

    def drop_counts(self, record_prefix: str | None = None) -> dict[str, int]:
        query = "SELECT reason, COUNT(*) AS n FROM extraction_drops"
        params: tuple = ()
        if record_prefix:
            escaped = record_prefix.replace("%", "\\%").replace("_", "\\_") + "%"
            query += " WHERE record_key LIKE ? ESCAPE '\\'"
            params = (escaped,)
        query += " GROUP BY reason ORDER BY n DESC"
        with self._connect() as connection:
            return {str(row["reason"]): int(row["n"]) for row in connection.execute(query, params)}

    def drops(self, reason: str | None = None, limit: int = 100) -> list[ExtractionDrop]:
        query = (
            "SELECT reason, subject_kind, subject_name, relation, object_kind, object_name, detail "
            "FROM extraction_drops"
        )
        params: tuple = ()
        if reason:
            query += " WHERE reason = ?"
            params = (reason,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [
            ExtractionDrop(
                reason=str(r["reason"]), subject_kind=r["subject_kind"], subject_name=r["subject_name"],
                relation=r["relation"], object_kind=r["object_kind"], object_name=r["object_name"],
                detail=r["detail"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------ ontology

    def seed_axioms_if_empty(self, rows: list[dict]) -> int:
        """Populate the vocabulary once, from code, then leave it alone.

        `INSERT OR IGNORE` rather than a wipe-and-reseed: once the table
        exists it is editable data, and a redeploy silently reverting a
        deliberate edit is exactly the failure this table was created to
        stop. New relations added in code still land; existing rows win.
        """
        if not rows:
            return 0
        with self._connect() as connection:
            before = connection.execute("SELECT COUNT(*) AS n FROM relation_axioms").fetchone()["n"]
            connection.executemany(
                "INSERT OR IGNORE INTO relation_axioms(relation, subject_kind, object_kind, "
                "extractable, functional, is_transitive, is_symmetric, is_asymmetric, "
                "inverse_of, sub_property_of, temporal) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (r["relation"], r["subject_kind"], r["object_kind"],
                     int(r["extractable"]), int(r["functional"]), int(r["is_transitive"]),
                     int(r["is_symmetric"]), int(r["is_asymmetric"]),
                     r["inverse_of"], r["sub_property_of"], r["temporal"])
                    for r in rows
                ],
            )
            after = connection.execute("SELECT COUNT(*) AS n FROM relation_axioms").fetchone()["n"]
        return int(after) - int(before)

    def axiom_rows(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT relation, subject_kind, object_kind, extractable, functional, "
                "is_transitive, is_symmetric, is_asymmetric, inverse_of, sub_property_of, "
                "temporal, adopted_batch FROM relation_axioms"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_miss(self, kind: str, key: str, example: str | None = None) -> None:
        """Count a term the ontology does not have, instead of discarding it.

        `dismissed_at` is a flag and never a DELETE: Utopia shipped the delete
        first and found that the next extraction simply re-inserted the term,
        so "the user's no did not survive one round of extraction"
        (`utopia/migrations/0004_ontology.sql:15-18`).
        """
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ontology_misses(kind, key, example, count, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(kind, key) DO UPDATE SET "
                "  count = ontology_misses.count + 1, "
                "  last_seen_at = excluded.last_seen_at, "
                "  example = COALESCE(ontology_misses.example, excluded.example)",
                (kind, key, example, now, now),
            )

    def misses(self, include_dismissed: bool = False) -> list[tuple[str, str, int, str | None]]:
        query = "SELECT kind, key, count, example FROM ontology_misses"
        if not include_dismissed:
            query += " WHERE dismissed_at IS NULL"
        query += " ORDER BY count DESC, key"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [(str(r["kind"]), str(r["key"]), int(r["count"]), r["example"]) for r in rows]

    def dismiss_miss(self, kind: str, key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE ontology_misses SET dismissed_at = ? WHERE kind = ? AND key = ?",
                (datetime.now(UTC).isoformat(), kind, key),
            )

    # --------------------------------------------------- ontology adoption

    def adoption_candidates(
        self, *, min_docs: int = 2, min_facts: int = 1,
    ) -> list[dict]:
        """Refused shapes that clear the bar, widest evidence first.

        `min_docs` counts DISTINCT source records, never a sum. Utopia's note
        on the same rule: one document may use two wordings, and summing lets
        a single document push a shape past a "seen in >= 2 documents" bar.
        Their reason for the bar at all: **a statement that appears in only
        one document is that document's wording, not the organization's
        vocabulary** -- and the ontology feeds back into the extraction
        prompt, so one accident becomes a standing instruction.

        Tautologies are excluded outright. In this graph, `Document -DEFINES->
        System` is the widest candidate (33 documents) and 19 of its 84 facts
        say a thing defines itself ("AWS-backed DataOS Lakehouse defines
        AWS-backed DataOS Lakehouse"). Counting cannot tell that from a real
        claim, so the shape-level threshold alone would adopt it.

        A dismissed miss is never a candidate, no matter how often it recurs:
        with adoption running unattended, re-proposing a dismissed shape is
        the system overruling an explicit human decision.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.subject_kind, d.relation, d.object_kind,
                       COUNT(*)                   AS facts,
                       COUNT(DISTINCT d.record_key) AS docs,
                       MIN(d.subject_name || ' -> ' || d.object_name) AS example
                FROM extraction_drops d
                WHERE d.reason = ?
                  AND d.relation != ''
                  AND LOWER(d.subject_name) != LOWER(d.object_name)
                  AND NOT EXISTS (
                        SELECT 1 FROM ontology_misses m
                        WHERE m.kind = 'relation_type'
                          AND m.key = d.subject_kind || ' -' || d.relation || '-> ' || d.object_kind
                          AND m.dismissed_at IS NOT NULL)
                  AND NOT EXISTS (
                        SELECT 1 FROM relation_axioms a
                        WHERE a.relation = d.relation
                          AND a.subject_kind = d.subject_kind
                          AND a.object_kind = d.object_kind)
                GROUP BY d.subject_kind, d.relation, d.object_kind
                HAVING docs >= ? AND facts >= ?
                ORDER BY docs DESC, facts DESC
                """,
                (DropReason.RELATION_NOT_ALLOWED, min_docs, min_facts),
            ).fetchall()
        return [dict(row) for row in rows]

    def adopt_shapes(self, shapes: list[dict], *, min_docs: int) -> str | None:
        """Write the shapes as extractable axioms under one revertible batch.

        Only the allow-list is widened. Every axiom column stays at its
        default -- `functional`, `is_transitive`, `is_symmetric` are NOT
        guessed from counts. Utopia's carve-out is the sharp end of this:
        `functional` drives the temporal engine to auto-close facts, and by
        the time a wrong one is noticed those closures are already a chain of
        supersedes. Counting says a shape is common; it says nothing about
        whether it holds one value at a time.
        """
        if not shapes:
            return None
        batch_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO relation_axioms(relation, subject_kind, object_kind, "
                "extractable, temporal, adopted_batch) VALUES (?,?,?,1,'state',?)",
                [(s["relation"], s["subject_kind"], s["object_kind"], batch_id) for s in shapes],
            )
            connection.execute(
                "INSERT INTO axiom_adoptions(batch_id, adopted_at, shapes, facts_expected, min_docs) "
                "VALUES (?,?,?,?,?)",
                (batch_id, now, json.dumps(shapes), sum(int(s["facts"]) for s in shapes), min_docs),
            )
        return batch_id

    def unadopt(self, batch_id: str) -> list[dict]:
        """Remove a batch's axioms and report the shapes, so the caller can
        delete the edges they produced.

        This is the precondition for adopting at all, not a nicety. Utopia:
        *"the precondition for daring to do this is that adoption is
        revertible -- if wrong, one click goes back. So the axis is not how
        confident we are, but how expensive it is if wrong."*
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT shapes FROM axiom_adoptions WHERE batch_id = ? AND undone_at IS NULL",
                (batch_id,),
            ).fetchone()
            if rows is None:
                return []
            shapes = json.loads(str(rows["shapes"]))
            connection.execute("DELETE FROM relation_axioms WHERE adopted_batch = ?", (batch_id,))
            connection.execute(
                "UPDATE axiom_adoptions SET undone_at = ? WHERE batch_id = ?",
                (datetime.now(UTC).isoformat(), batch_id),
            )
        return shapes

    def adoptions(self, include_undone: bool = False) -> list[dict]:
        query = ("SELECT batch_id, adopted_at, shapes, facts_expected, min_docs, undone_at "
                 "FROM axiom_adoptions")
        if not include_undone:
            query += " WHERE undone_at IS NULL"
        query += " ORDER BY adopted_at DESC"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [{**dict(r), "shapes": json.loads(str(r["shapes"]))} for r in rows]

    def requeue_chunks_for_shapes(self, shapes: list[dict]) -> int:
        """Re-open every chunk whose refusal these shapes would now allow.

        Adoption alone changes nothing: the facts were refused at extraction
        time and the graph has never seen them. Only the chunks that actually
        hit one of these shapes are re-opened -- the whole point of the chunk
        diff is not to re-extract text that has nothing to gain.
        """
        if not shapes:
            return 0
        clauses = " OR ".join(
            ["(subject_kind = ? AND relation = ? AND object_kind = ?)"] * len(shapes)
        )
        params: list[str] = []
        for shape in shapes:
            params += [shape["subject_kind"], shape["relation"], shape["object_kind"]]
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE source_chunks SET status = 'pending'
                WHERE superseded_at IS NULL
                  AND (record_key, chunk_id) IN (
                        SELECT record_key, chunk_id FROM extraction_drops
                        WHERE reason = ? AND ({clauses}))
                """,
                (DropReason.RELATION_NOT_ALLOWED, *params),
            )
            return int(cursor.rowcount)

    # ------------------------------------------------------------- settings

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM graph_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO graph_settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

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
