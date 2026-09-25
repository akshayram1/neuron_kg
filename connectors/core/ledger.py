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


@dataclass(frozen=True)
class SyncCoverage:
    """One sync's accounting (plan.md Phase 0.4): what the provider says
    exists (when its API exposes a total), what we actually fetched, what
    landed in the ledger, and what a deliberate rule dropped before it ever
    reached the ledger (e.g. Bitbucket/GitHub non-.py/.md files, a commit
    cap). `provider_reported_total` is `None` -- not 0 or a guess -- for any
    provider/endpoint whose API does not hand back a total count."""

    run_id: str
    provider: str
    connection_id: str | None
    provider_reported_total: int | None
    fetched_count: int
    ledger_count: int
    skipped_by_rule_count: int
    created_at: str


class ReviewState(StrEnum):
    """State of one row in `reviews` (plan.md Phase 3 §3.0 "Minimal review
    queue"). A review starts PENDING and is decided exactly once, into
    APPROVED or REJECTED — `approve_review`/`reject_review` only act on a
    row that is still PENDING, so a decided review cannot be re-decided out
    from under whoever already acted on it."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Review:
    """One human-review proposal: `possibly_same_as`, `fact_update`, and
    later link/duplicate candidates (plan.md Phase 3 §3.0). `type` is
    deliberately free text, not a StrEnum -- new proposal types will be
    added by later phases this module does not know about yet.

    `payload` is caller-defined proposal data, returned here already
    `json.loads`'d (the DB column stores the JSON text; see
    `axiom_adoptions.shapes` for the same round-trip convention elsewhere in
    this file). This module never inspects `payload`'s keys.

    `identity` is an optional, caller-supplied stable key for the proposal
    -- e.g. `f"{type}:{subject_uid}:{object_uid}"` for a merge/update
    candidate -- used only to dedupe against previously-rejected proposals
    (see `create_review` and `review_rejections`). It is intentionally
    opaque to this module: the shape is the caller's choice, not something
    parsed out of `payload`.
    """

    id: int
    type: str
    payload: dict
    identity: str | None
    state: str
    decided_by: str | None
    decided_at: str | None
    created_at: str


@dataclass(frozen=True)
class EntityAlias:
    """One row of `entity_aliases` (plan.md Phase 4 §4.8): a normalized
    alias that resolves to an existing entity's canonical uid. Filled by
    approved `POSSIBLY_SAME_AS` reviews, approved Phase 6.4 pairwise merges,
    and manual entries. Step 3 of the resolution ladder (§4.2) reads this
    table by `(label, namespace_uid, alias_norm)` before falling through to
    vector search.

    `namespace_uid` is `''`, never `NULL`, for labels whose identity is not
    namespace-scoped (e.g. Decision, per §4.0) -- SQLite treats every `NULL`
    as distinct from every other `NULL` inside a UNIQUE constraint, which
    would silently break idempotent re-adds and the uniqueness this table
    depends on for `(label, namespace_uid, alias_norm)` to be a reliable key.
    """

    id: int
    label: str
    namespace_uid: str
    alias_norm: str
    uid: str
    source: str
    created_at: str


@dataclass(frozen=True)
class StoplistTerm:
    """One row of `mention_stoplist` (plan.md Phase 4 §4.5): a generic term
    that should never, by itself, mint a Term/System node. `label` is `''`
    for an entry that applies globally across both labels the mention filter
    covers (§4.5 is scoped to `Term`/`System` only, and the seed terms —
    "data", "pipeline", "api", … — are generic across both, so the seed set
    is global rather than per-label); a non-empty `label` scopes one entry to
    just that label."""

    term_norm: str
    label: str
    reason: str | None
    created_at: str


@dataclass(frozen=True)
class ResolutionStat:
    """One aggregated row of `resolution_stats` (plan.md Phase 4 §4.6): how
    many `label` entities resolved via `resolved_by` during `run_id`.
    `resolved_by` is free text matching the resolution ladder's own
    vocabulary (§4.2) -- currently `scoped_exact`, `alias`, `vector`,
    `review_required`, `laya_suggest`, `laya`, `new` -- but this table does
    not enforce that list as an enum, since the ladder may grow new
    resolution paths later without a schema change."""

    run_id: str
    label: str
    resolved_by: str
    count: int
    created_at: str


@dataclass(frozen=True)
class HygieneCount:
    """One label's isolated/total node count for a single hygiene run
    (plan.md Phase 6 §6.1) -- the caller's INPUT to
    `record_hygiene_counts`, not a stored row (see `HygieneRun` for that).
    `label` is the node label being measured -- e.g. `"Document"`,
    `"Decision"`, `"Commit"`, `"Term"`, `"System"` per §6.1's isolation
    rules -- free text, not a StrEnum, since this module does not know or
    enforce which labels the hygiene job chooses to measure."""

    label: str
    isolated_count: int
    total_count: int


@dataclass(frozen=True)
class HygieneRun:
    """One stored row of `hygiene_runs` (plan.md Phase 6 §6.1): one label's
    isolated/total counts from one hygiene run, on one graph. One row per
    `(run_id, label)` so trend queries can filter by label without
    unpacking a wider per-run blob."""

    id: int
    run_id: str
    graph_name: str
    label: str
    isolated_count: int
    total_count: int
    created_at: str


@dataclass(frozen=True)
class LinkCandidate:
    """One row of `link_candidates` (plan.md Phase 6 §6.2): a proposed
    derived edge between two nodes, not yet a real graph edge.
    `derived_rule` is `"two_hop"` (DICE two-hop co-occurrence) or
    `"semantic_candidate"` (embedding similarity) per §6.2 -- free text,
    not a StrEnum, matching `Review.type`'s reasoning: new candidate
    sources may be added later without a schema change.

    `state` reuses `ReviewState` (pending/approved/rejected) rather than a
    parallel enum with identical values -- this table's lifecycle is the
    same propose-then-decide-once shape `reviews` already has, just
    carrying the edge-specific columns (`from_uid`, `to_uid`, `relation`,
    `confidence`, `derived_rule`) that `reviews`' generic `payload` blob
    doesn't structure.
    """

    id: int
    from_uid: str
    to_uid: str
    relation: str
    confidence: float
    derived_rule: str
    state: str
    created_at: str
    decided_at: str | None


@dataclass(frozen=True)
class MergeTrace:
    """One row of `merge_trace` (plan.md Phase 6 §6.4): a durable audit
    record of one EXECUTED pairwise merge -- written once a merge actually
    happens (survivor absorbs the other node's edges and
    `source_record_keys`), never when a merge is merely proposed (that
    proposal goes through the generic `reviews` table with
    `type="duplicate_pair"`, built elsewhere, out of this module's scope).
    `survivor_uid` absorbed `absorbed_uid`; `label` is their shared node
    label (`Decision` or `Term` per §6.4)."""

    id: int
    survivor_uid: str
    absorbed_uid: str
    label: str
    merged_at: str
    reason: str | None


class DropReason(StrEnum):
    """Why one extracted item did not become a fact. `DIRECTION_CORRECTED` is
    deliberately in this vocabulary while NOT being a loss — the fact was
    written, with its endpoints swapped to match the ontology. It is recorded
    here so that correction can never happen silently. `GENERIC_MENTION`
    (plan.md Phase 4 §4.5) is a mention-filter drop, not an extraction drop
    -- e.g. "data" or "pipeline" standing alone never becomes a Term/System
    node -- but it is recorded and counted through the same
    `record_drops`/`drop_counts`/`drops` mechanism as every other reason
    here, since that mechanism already treats `reason` as free text."""

    RELATION_NOT_ALLOWED = "relation_not_allowed"
    EVIDENCE_NOT_IN_CHUNK = "evidence_not_in_chunk"
    ENDPOINT_UNRESOLVED = "endpoint_unresolved"
    ENTITY_NO_CONNECTING_FACT = "entity_no_connecting_fact"
    DIRECTION_CORRECTED = "direction_corrected"
    GENERIC_MENTION = "generic_mention"


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
    # ``text`` is the selectively unresolved evidence sent to the LLM.
    # ``source_text`` keeps the complete canonical chunk for provenance.
    source_text: str | None = None


@dataclass(frozen=True)
class ChunkWrite:
    """One canonical chunk plus its deterministic-pass disposition."""

    chunk_id: str
    chunk_index: int
    text: str
    status: SemanticStatus | str = SemanticStatus.PENDING
    llm_text: str | None = None
    resolution_status: str = "unresolved"
    resolution_reason: str | None = None


def normalize_chunk_write(value: ChunkWrite | tuple) -> ChunkWrite:
    """Keep the long-standing 3-tuple ledger API backward compatible."""
    if isinstance(value, ChunkWrite):
        return value
    if len(value) == 3:
        chunk_id, chunk_index, text = value
        return ChunkWrite(chunk_id, chunk_index, text, llm_text=text)
    raise ValueError("chunks must be ChunkWrite instances or (id, index, text) tuples")


# Seed terms for `mention_stoplist` (plan.md Phase 4 §4.5): generic nouns
# that name the ingestion/data-pipeline machinery itself, not a real Term or
# System entity a document is actually about. Seeded once via `INSERT OR
# IGNORE` when the table is first created -- editable afterwards without a
# redeploy, the same "seed from code, then leave the data alone" convention
# `graph/axioms.py`'s `seed_axioms()` uses for `relation_axioms` (axioms are
# genuinely ledger-backed today: `relation_axioms` + `seed_axioms_if_empty`
# below, read back by `graph.axioms.load_axioms`). Axioms seed lazily, only
# when `load_axioms(ledger)` is first called, because they need
# `graph/ontology.py`'s `RELATION_TYPE_MAP`, which this module cannot import
# without an upward dependency. The stoplist has no such constraint -- it is
# a flat literal list -- so it seeds eagerly here in `__init__`, right when
# the table is created.
_STOPLIST_SEED_TERMS = (
    "data", "pipeline", "source", "table", "service", "api", "system",
    "config", "batch source", "source_table",
)


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
            if "llm_text" not in chunk_columns:
                connection.execute("ALTER TABLE source_chunks ADD COLUMN llm_text TEXT")
            if "resolution_status" not in chunk_columns:
                connection.execute(
                    "ALTER TABLE source_chunks ADD COLUMN resolution_status TEXT NOT NULL DEFAULT 'unresolved'"
                )
            if "resolution_reason" not in chunk_columns:
                connection.execute("ALTER TABLE source_chunks ADD COLUMN resolution_reason TEXT")
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_coverage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    connection_id TEXT,
                    provider_reported_total INTEGER,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    ledger_count INTEGER NOT NULL DEFAULT 0,
                    skipped_by_rule_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_coverage_provider "
                "ON sync_coverage(provider, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_coverage_run ON sync_coverage(run_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    identity TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    decided_by TEXT,
                    decided_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reviews_state ON reviews(state, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reviews_type ON reviews(type, state)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_reviews_identity ON reviews(identity) "
                "WHERE identity IS NOT NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_rejections (
                    identity TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    review_id INTEGER,
                    rejected_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL,
                    namespace_uid TEXT NOT NULL DEFAULT '',
                    alias_norm TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(label, namespace_uid, alias_norm)
                )
                """
            )
            # The UNIQUE constraint above already creates a covering index in
            # this exact column order, serving step 3 of the ladder's lookup
            # shape (`WHERE label = ? AND namespace_uid = ? AND alias_norm =
            # ?`) directly. A second index serves the other read direction:
            # every alias for a given canonical uid (diagnostics/debugging).
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_entity_aliases_uid ON entity_aliases(uid)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mention_stoplist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    term_norm TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(term_norm, label)
                )
                """
            )
            # Same reasoning as entity_aliases: the UNIQUE constraint already
            # covers the hot lookup shape (`term_norm = ? AND label IN (?,
            # '')`) since both columns are its leading prefix.
            connection.executemany(
                "INSERT OR IGNORE INTO mention_stoplist(term_norm, label, reason, created_at) "
                "VALUES (?, '', 'seed', ?)",
                [(term, datetime.now(UTC).isoformat()) for term in _STOPLIST_SEED_TERMS],
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resolution_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    resolved_by TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, label, resolved_by)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_resolution_stats_run ON resolution_stats(run_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hygiene_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    graph_name TEXT NOT NULL DEFAULT 'default',
                    label TEXT NOT NULL,
                    isolated_count INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Covers `hygiene_trend`'s read shape (`WHERE graph_name = ? AND
            # label = ? ORDER BY created_at DESC`) directly.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_hygiene_runs_trend "
                "ON hygiene_runs(graph_name, label, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_hygiene_runs_run ON hygiene_runs(run_id)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS link_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_uid TEXT NOT NULL,
                    to_uid TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    derived_rule TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    UNIQUE(from_uid, to_uid, relation)
                )
                """
            )
            # The UNIQUE constraint is `create_link_candidate`'s idempotency
            # key (plan.md §6.2): the same triple proposed twice resolves to
            # the same row instead of duplicating.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_link_candidates_state "
                "ON link_candidates(state, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_link_candidates_rule "
                "ON link_candidates(derived_rule, state)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS merge_trace (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    survivor_uid TEXT NOT NULL,
                    absorbed_uid TEXT NOT NULL,
                    label TEXT NOT NULL,
                    merged_at TEXT NOT NULL,
                    reason TEXT
                )
                """
            )
            # `merged_into` is the hot lookup (by absorbed_uid); the
            # survivor-side index exists for the same diagnostics purpose as
            # `idx_entity_aliases_uid`.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_merge_trace_absorbed "
                "ON merge_trace(absorbed_uid)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_merge_trace_survivor "
                "ON merge_trace(survivor_uid)"
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
        self, record_key: str, chunks: list[tuple[str, int, str] | ChunkWrite],
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
        writes = [normalize_chunk_write(item) for item in chunks]
        incoming = {item.chunk_id: item for item in writes}
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
                [(incoming[chunk_id].chunk_index, record_key, chunk_id) for chunk_id in kept],
            )
            connection.executemany(
                "UPDATE source_chunks SET status = ?, llm_text = ?, resolution_status = ?, resolution_reason = ? "
                "WHERE record_key = ? AND chunk_id = ?",
                [(
                    str(SemanticStatus.DONE) if (
                        existing[chunk_id] == str(SemanticStatus.DONE)
                        or str(incoming[chunk_id].status) == str(SemanticStatus.DONE)
                    ) else str(SemanticStatus.PENDING),
                    incoming[chunk_id].llm_text,
                    incoming[chunk_id].resolution_status,
                    incoming[chunk_id].resolution_reason,
                    record_key, chunk_id,
                ) for chunk_id in kept],
            )
            connection.executemany(
                "INSERT INTO source_chunks(record_key, chunk_id, chunk_index, text, status, llm_text, "
                "resolution_status, resolution_reason, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (record_key, chunk_id, incoming[chunk_id].chunk_index, incoming[chunk_id].text,
                     adopted_by_text.get(incoming[chunk_id].text, str(incoming[chunk_id].status)),
                     incoming[chunk_id].llm_text, incoming[chunk_id].resolution_status,
                     incoming[chunk_id].resolution_reason, now)
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
                + sum(1 for chunk_id in added if incoming[chunk_id].text in adopted_by_text)
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
                    "SELECT c.record_key, c.chunk_id, c.chunk_index, coalesce(c.llm_text, c.text), c.text "
                    "FROM source_chunks c JOIN source_records s ON s.record_key = c.record_key "
                    "WHERE c.status = 'pending' AND c.superseded_at IS NULL AND c.record_key LIKE ? ESCAPE '\\' "
                    "ORDER BY s.semantic_priority DESC, c.committed_at ASC, c.chunk_index ASC LIMIT ?",
                    (escaped, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT c.record_key, c.chunk_id, c.chunk_index, coalesce(c.llm_text, c.text), c.text "
                    "FROM source_chunks c JOIN source_records s ON s.record_key = c.record_key "
                    "WHERE c.status = 'pending' AND c.superseded_at IS NULL "
                    "ORDER BY s.semantic_priority DESC, c.committed_at ASC, c.chunk_index ASC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [PendingChunk(row[0], row[1], row[2], row[3], row[4]) for row in rows]

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

    def count_present(self, record_keys: list[str]) -> int:
        """How many of these exact keys are in the ledger right now.

        Used for sync coverage's `ledger_count` (plan.md Phase 0.4): a
        `fetched == written` tally only proves the write call was invoked,
        not that it committed -- a mid-batch crash or a record that was
        deleted and never recommitted would still look complete without
        actually re-reading the ledger.
        """
        if not record_keys:
            return 0
        total = 0
        with self._connect() as connection:
            # SQLite's default bound-parameter limit is 999 -- chunk the IN clause.
            for start in range(0, len(record_keys), 500):
                batch = record_keys[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                row = connection.execute(
                    f"SELECT COUNT(*) AS n FROM source_records WHERE record_key IN ({placeholders})",
                    batch,
                ).fetchone()
                total += int(row["n"])
        return total

    # ------------------------------------------------------------ sync coverage

    def record_sync_coverage(
        self,
        run_id: str,
        provider: str,
        *,
        connection_id: str | None = None,
        provider_reported_total: int | None = None,
        fetched_count: int = 0,
        ledger_count: int = 0,
        skipped_by_rule_count: int = 0,
    ) -> None:
        """Log one sync's coverage numbers (plan.md Phase 0.4).

        Append-only, one row per run -- coverage is a measurement over time,
        not a single mutable "latest" cell, so a regression shows up as a
        row-to-row comparison instead of overwriting the evidence of a
        previous, better run.
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sync_coverage(run_id, provider, connection_id, "
                "provider_reported_total, fetched_count, ledger_count, "
                "skipped_by_rule_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, provider, connection_id, provider_reported_total,
                    fetched_count, ledger_count, skipped_by_rule_count,
                    datetime.now(UTC).isoformat(),
                ),
            )

    @staticmethod
    def _sync_coverage_row(row: sqlite3.Row) -> SyncCoverage:
        return SyncCoverage(
            run_id=str(row["run_id"]), provider=str(row["provider"]),
            connection_id=row["connection_id"],
            provider_reported_total=row["provider_reported_total"],
            fetched_count=int(row["fetched_count"]), ledger_count=int(row["ledger_count"]),
            skipped_by_rule_count=int(row["skipped_by_rule_count"]),
            created_at=str(row["created_at"]),
        )

    def latest_sync_coverage(self, provider: str | None = None) -> list[SyncCoverage]:
        """The most recent coverage row per provider, newest sync only.

        With `provider` given, just that provider's latest row (0 or 1
        results); otherwise one row per provider that has ever synced.
        """
        with self._connect() as connection:
            if provider:
                rows = connection.execute(
                    "SELECT * FROM sync_coverage WHERE provider = ? ORDER BY id DESC LIMIT 1",
                    (provider,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT s.* FROM sync_coverage s
                    INNER JOIN (
                        SELECT provider, MAX(id) AS max_id FROM sync_coverage GROUP BY provider
                    ) latest ON s.provider = latest.provider AND s.id = latest.max_id
                    ORDER BY s.provider
                    """
                ).fetchall()
        return [self._sync_coverage_row(row) for row in rows]

    def sync_coverage_for_run(self, run_id: str) -> SyncCoverage | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sync_coverage WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._sync_coverage_row(row) if row else None

    # ------------------------------------------------------------ review queue

    @staticmethod
    def _review_row(row: sqlite3.Row) -> Review:
        return Review(
            id=int(row["id"]), type=str(row["type"]), payload=json.loads(str(row["payload"])),
            identity=row["identity"], state=str(row["state"]),
            decided_by=row["decided_by"], decided_at=row["decided_at"],
            created_at=str(row["created_at"]),
        )

    def is_identity_rejected(self, identity: str) -> bool:
        """True if a proposal with this identity was already rejected --
        the check `create_review` makes before inserting a new pending row,
        also usable standalone by a caller deciding whether to propose at
        all (plan.md Phase 3 §3.0 "Cache rejection identities so the same
        proposal is not recreated")."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM review_rejections WHERE identity = ?", (identity,)
            ).fetchone()
        return row is not None

    def create_review(
        self, review_type: str, payload: dict, *, identity: str | None = None,
    ) -> int | None:
        """Create a pending review for `payload` under `review_type`.

        `identity` is the caller's stable key for this proposal (see
        `Review`'s docstring for the shape convention). When given and a
        proposal with that identity was already rejected, no new row is
        created and `None` is returned -- the human already declined this,
        and re-surfacing it on every run would make the queue impossible to
        clear. Otherwise inserts a new PENDING row and returns its id.
        """
        with self._connect() as connection:
            if identity is not None:
                already_rejected = connection.execute(
                    "SELECT 1 FROM review_rejections WHERE identity = ?", (identity,)
                ).fetchone()
                if already_rejected:
                    return None
            cursor = connection.execute(
                "INSERT INTO reviews(type, payload, identity, state, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    review_type, json.dumps(payload), identity, str(ReviewState.PENDING),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def get_review(self, review_id: int) -> Review | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return self._review_row(row) if row else None

    def list_reviews(
        self, *, state: ReviewState | str | None = None, type: str | None = None,
        limit: int = 200,
    ) -> list[Review]:
        """Newest first, optionally filtered by `state` and/or `type`."""
        query = "SELECT * FROM reviews"
        clauses: list[str] = []
        params: list = []
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state))
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._review_row(row) for row in rows]

    def approve_review(self, review_id: int, decided_by: str) -> Review | None:
        """Approve a PENDING review. Returns the decided row, or `None` if
        `review_id` does not exist or is no longer PENDING (already decided
        by someone else -- this never re-decides a review out from under a
        previous decision)."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reviews SET state = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND state = ?",
                (str(ReviewState.APPROVED), decided_by, now, review_id, str(ReviewState.PENDING)),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return self._review_row(row) if row else None

    def reject_review(self, review_id: int, decided_by: str) -> Review | None:
        """Reject a PENDING review and, when it carries an `identity`, cache
        that identity so the same proposal is not recreated (plan.md Phase 3
        §3.0). Returns the decided row, or `None` if `review_id` does not
        exist or is no longer PENDING."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reviews SET state = ?, decided_by = ?, decided_at = ? "
                "WHERE id = ? AND state = ?",
                (str(ReviewState.REJECTED), decided_by, now, review_id, str(ReviewState.PENDING)),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            review = self._review_row(row)
            if review.identity is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO review_rejections(identity, type, review_id, rejected_at) "
                    "VALUES (?, ?, ?, ?)",
                    (review.identity, review.type, review.id, now),
                )
        return review

    # ------------------------------------------------------------ entity aliases

    @staticmethod
    def _entity_alias_row(row: sqlite3.Row) -> EntityAlias:
        return EntityAlias(
            id=int(row["id"]), label=str(row["label"]), namespace_uid=str(row["namespace_uid"]),
            alias_norm=str(row["alias_norm"]), uid=str(row["uid"]), source=str(row["source"]),
            created_at=str(row["created_at"]),
        )

    def add_entity_alias(
        self, label: str, namespace_uid: str | None, alias_norm: str, uid: str, source: str,
    ) -> None:
        """Add or update one alias (plan.md Phase 4 §4.8): `source` is free
        text describing where it came from, e.g. `"review:<review_id>"`,
        `"manual"`, or `"phase6_merge:<id>"`.

        Idempotent on `(label, namespace_uid, alias_norm)` -- this is a
        lookup table, not an append-only log, so re-adding the same alias
        updates which uid it resolves to (and who last said so) rather than
        duplicating the row. `namespace_uid=None` is normalized to `''`,
        matching the schema's default for labels without namespace-scoped
        identity (e.g. Decision, per §4.0).
        """
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entity_aliases(label, namespace_uid, alias_norm, uid, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(label, namespace_uid, alias_norm) DO UPDATE SET "
                "  uid = excluded.uid, source = excluded.source, created_at = excluded.created_at",
                (label, namespace_uid or "", alias_norm, uid, source, datetime.now(UTC).isoformat()),
            )

    def lookup_alias(self, label: str, namespace_uid: str | None, alias_norm: str) -> str | None:
        """Step 3 of the resolution ladder (plan.md §4.2): the canonical uid
        this alias resolves to, or `None` on a miss."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT uid FROM entity_aliases WHERE label = ? AND namespace_uid = ? AND alias_norm = ?",
                (label, namespace_uid or "", alias_norm),
            ).fetchone()
        return str(row["uid"]) if row else None

    def aliases_for_uid(self, uid: str) -> list[EntityAlias]:
        """Every alias that resolves to this uid, for diagnostics/debugging."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_aliases WHERE uid = ? ORDER BY created_at", (uid,),
            ).fetchall()
        return [self._entity_alias_row(row) for row in rows]

    # ------------------------------------------------------------ mention stoplist

    @staticmethod
    def _normalize_stoplist_term(term: str) -> str:
        """"Normalized" here means lowercased and stripped of leading/
        trailing whitespace -- the same transform every stoplist accessor
        applies to its input, so a caller never has to pre-normalize."""
        return term.strip().lower()

    def is_stoplisted(self, term: str, label: str | None = None) -> bool:
        """True if `term` is on the mention stoplist (plan.md §4.5), either
        as a global entry (seeded terms all are) or scoped to `label`."""
        term_norm = self._normalize_stoplist_term(term)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM mention_stoplist WHERE term_norm = ? AND label IN (?, '') LIMIT 1",
                (term_norm, label or ""),
            ).fetchone()
        return row is not None

    def add_stoplist_term(self, term: str, label: str | None = None, reason: str | None = None) -> None:
        """Add or update a stoplist entry -- "editable without deploy" per
        §4.5, for whatever admin surface later calls this. Idempotent on
        `(term_norm, label)`; `label=None`/`''` means global."""
        term_norm = self._normalize_stoplist_term(term)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO mention_stoplist(term_norm, label, reason, created_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(term_norm, label) DO UPDATE SET reason = excluded.reason",
                (term_norm, label or "", reason, datetime.now(UTC).isoformat()),
            )

    def remove_stoplist_term(self, term: str, label: str | None = None) -> None:
        term_norm = self._normalize_stoplist_term(term)
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM mention_stoplist WHERE term_norm = ? AND label = ?",
                (term_norm, label or ""),
            )

    def stoplist_terms(self, label: str | None = None) -> list[StoplistTerm]:
        """All stoplist entries, optionally scoped to one `label` (still
        includes global `''` entries, matching what `is_stoplisted` checks)."""
        with self._connect() as connection:
            if label is None:
                rows = connection.execute(
                    "SELECT term_norm, label, reason, created_at FROM mention_stoplist "
                    "ORDER BY label, term_norm"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT term_norm, label, reason, created_at FROM mention_stoplist "
                    "WHERE label IN (?, '') ORDER BY label, term_norm",
                    (label,),
                ).fetchall()
        return [
            StoplistTerm(term_norm=str(r["term_norm"]), label=str(r["label"]), reason=r["reason"],
                         created_at=str(r["created_at"]))
            for r in rows
        ]

    # ------------------------------------------------------------ resolution stats

    def record_resolution(self, run_id: str, label: str, resolved_by: str, count: int = 1) -> None:
        """Accumulate `count` for `(run_id, label, resolved_by)` (plan.md
        §4.6). Upsert-style: `_write_extraction` resolves many entities per
        run, so the same combination increments across calls instead of
        being overwritten by the last one."""
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO resolution_stats(run_id, label, resolved_by, count, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, label, resolved_by) DO UPDATE SET "
                "  count = resolution_stats.count + excluded.count",
                (run_id, label, resolved_by, count, datetime.now(UTC).isoformat()),
            )

    def resolution_stats_for_run(self, run_id: str) -> list[ResolutionStat]:
        """Raw label x resolved_by counts for one sync run. Reading the
        pattern (mostly scoped_exact/alias = healthy, etc., per §4.6) is the
        future diagnostics view's job, not this accessor's."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id, label, resolved_by, count, created_at FROM resolution_stats "
                "WHERE run_id = ? ORDER BY label, resolved_by",
                (run_id,),
            ).fetchall()
        return [
            ResolutionStat(run_id=str(r["run_id"]), label=str(r["label"]), resolved_by=str(r["resolved_by"]),
                           count=int(r["count"]), created_at=str(r["created_at"]))
            for r in rows
        ]

    # ------------------------------------------------------------ hygiene runs

    @staticmethod
    def _hygiene_run_row(row: sqlite3.Row) -> HygieneRun:
        return HygieneRun(
            id=int(row["id"]), run_id=str(row["run_id"]), graph_name=str(row["graph_name"]),
            label=str(row["label"]), isolated_count=int(row["isolated_count"]),
            total_count=int(row["total_count"]), created_at=str(row["created_at"]),
        )

    def record_hygiene_counts(
        self, run_id: str, counts: list[HygieneCount], *, graph_name: str = "default",
    ) -> None:
        """Persist one hygiene run's per-label isolated/total counts
        (plan.md Phase 6 §6.1). Batch-friendly: one sync's hygiene pass
        measures several labels (`Document`, `Decision`, `Commit`, `Term`,
        `System`) at once, so this takes the whole list rather than being
        called once per label. Append-only, one row per `(run_id, label)` --
        counts are a measurement over time, not a mutable "latest" cell,
        the same convention `record_sync_coverage` uses elsewhere in this
        file, so a regression shows up as a row-to-row comparison instead of
        overwriting the evidence of a healthier previous run.
        """
        if not counts:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.executemany(
                "INSERT INTO hygiene_runs(run_id, graph_name, label, isolated_count, "
                "total_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (run_id, graph_name, c.label, c.isolated_count, c.total_count, now)
                    for c in counts
                ],
            )

    def hygiene_trend(
        self, label: str, *, graph_name: str = "default", limit: int = 20,
    ) -> list[HygieneRun]:
        """The most recent `limit` hygiene runs for one `label` on one
        graph, newest first (same ordering convention as `list_reviews`/
        `adoptions` elsewhere in this file) -- a dashboard trend chart
        reverses this list if it wants chronological order. No separate
        date-range query is built here: nothing in §6.1/§6.6 needs one yet,
        and `created_at` is a plain ISO-8601 string a caller can filter on
        directly (`hygiene_trend` plus a Python-side date comparison) if
        that need shows up later.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hygiene_runs WHERE graph_name = ? AND label = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (graph_name, label, limit),
            ).fetchall()
        return [self._hygiene_run_row(row) for row in rows]

    # ------------------------------------------------------------ link candidates

    @staticmethod
    def _link_candidate_row(row: sqlite3.Row) -> LinkCandidate:
        return LinkCandidate(
            id=int(row["id"]), from_uid=str(row["from_uid"]), to_uid=str(row["to_uid"]),
            relation=str(row["relation"]), confidence=float(row["confidence"]),
            derived_rule=str(row["derived_rule"]), state=str(row["state"]),
            created_at=str(row["created_at"]), decided_at=row["decided_at"],
        )

    def create_link_candidate(
        self, from_uid: str, to_uid: str, relation: str, *,
        derived_rule: str, confidence: float = 0.5,
    ) -> int:
        """Propose one derived-edge candidate (plan.md Phase 6 §6.2).

        Idempotent on `(from_uid, to_uid, relation)`: `INSERT OR IGNORE`
        against the table's UNIQUE constraint means the same triple
        proposed twice -- by the same hygiene run or a later one -- never
        duplicates. This is a simpler mechanism than `reviews`' rejection
        cache (§3.0): there is only ever one row per triple, so its `state`
        alone already answers "is this still open" -- `pending` (new or
        re-proposed), or already decided (`approved`/`rejected`). A
        candidate that was already decided is deliberately NOT reset to
        pending by a re-proposal: once decided, it stays decided, the same
        "a human's no should not be silently overridden" principle
        `review_rejections` exists for in Phase 3.

        Returns the candidate's id, whether this call inserted a new row or
        found the existing one for this triple.
        """
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO link_candidates(from_uid, to_uid, relation, "
                "confidence, derived_rule, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (from_uid, to_uid, relation, confidence, derived_rule, str(ReviewState.PENDING), now),
            )
            row = connection.execute(
                "SELECT id FROM link_candidates WHERE from_uid = ? AND to_uid = ? AND relation = ?",
                (from_uid, to_uid, relation),
            ).fetchone()
        return int(row["id"])

    def get_link_candidate(self, candidate_id: int) -> LinkCandidate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM link_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._link_candidate_row(row) if row else None

    def list_link_candidates(
        self, *, state: ReviewState | str | None = ReviewState.PENDING,
        derived_rule: str | None = None, limit: int = 200,
    ) -> list[LinkCandidate]:
        """Newest first, optionally filtered by `state` (defaults to
        `pending` -- §6.2's primary read shape is "what needs review", the
        same default the review-queue UI (§6.5) will want) and by
        `derived_rule`. Pass `state=None` to list candidates in every
        state."""
        query = "SELECT * FROM link_candidates"
        clauses: list[str] = []
        params: list = []
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state))
        if derived_rule is not None:
            clauses.append("derived_rule = ?")
            params.append(derived_rule)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._link_candidate_row(row) for row in rows]

    def approve_link_candidate(self, candidate_id: int) -> LinkCandidate | None:
        """Approve a PENDING candidate. Returns the decided row, or `None`
        if `candidate_id` does not exist or is no longer PENDING -- the
        same decide-once guarantee `approve_review` gives `reviews`, so a
        decided candidate cannot be re-decided out from under whoever
        already acted on it. Writing the resulting real edge (`derived:
        true`, `extraction_method: "derived"`, provenance from both nodes'
        records, per §6.2) is the caller's job, not this accessor's."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE link_candidates SET state = ?, decided_at = ? WHERE id = ? AND state = ?",
                (str(ReviewState.APPROVED), now, candidate_id, str(ReviewState.PENDING)),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM link_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._link_candidate_row(row) if row else None

    def reject_link_candidate(self, candidate_id: int) -> LinkCandidate | None:
        """Reject a PENDING candidate. Returns the decided row, or `None`
        if `candidate_id` does not exist or is no longer PENDING. No
        separate rejection cache is needed here (unlike `reviews`): the row
        itself IS the identity (its UNIQUE triple), so it simply stays
        `rejected` and `create_link_candidate`'s `INSERT OR IGNORE` will
        never resurrect it as pending."""
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE link_candidates SET state = ?, decided_at = ? WHERE id = ? AND state = ?",
                (str(ReviewState.REJECTED), now, candidate_id, str(ReviewState.PENDING)),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM link_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return self._link_candidate_row(row) if row else None

    # ------------------------------------------------------------ merge trace

    @staticmethod
    def _merge_trace_row(row: sqlite3.Row) -> MergeTrace:
        return MergeTrace(
            id=int(row["id"]), survivor_uid=str(row["survivor_uid"]),
            absorbed_uid=str(row["absorbed_uid"]), label=str(row["label"]),
            merged_at=str(row["merged_at"]), reason=row["reason"],
        )

    def record_merge_trace(
        self, survivor_uid: str, absorbed_uid: str, label: str, *, reason: str | None = None,
    ) -> int:
        """Record one EXECUTED pairwise merge (plan.md Phase 6 §6.4) --
        called once a merge actually happens (survivor absorbs the other
        node's edges and `source_record_keys`), never when a merge is
        merely proposed (that proposal lives in `reviews` with
        `type="duplicate_pair"`, built elsewhere, out of this module's
        scope). Append-only: a node should be absorbed only once in
        practice, but nothing here enforces that -- this is a durable audit
        log, not a constraint surface. Returns the new trace row's id."""
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO merge_trace(survivor_uid, absorbed_uid, label, merged_at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (survivor_uid, absorbed_uid, label, datetime.now(UTC).isoformat(), reason),
            )
        return int(cursor.lastrowid)

    def merged_into(self, uid: str) -> str | None:
        """The uid this node was absorbed into, or `None` if it was never
        merged -- a quick "was this node absorbed" check for downstream
        code deciding whether to redirect a reference (plan.md §6.4). This
        is a single-hop lookup only: if `uid`'s survivor was itself later
        absorbed into a third node, this returns the immediate survivor,
        not the end of the chain -- following multi-hop merge chains is
        redirect logic, explicitly out of this module's scope. If a uid
        somehow has more than one trace row, the most recent merge wins."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT survivor_uid FROM merge_trace WHERE absorbed_uid = ? "
                "ORDER BY merged_at DESC, id DESC LIMIT 1",
                (uid,),
            ).fetchone()
        return str(row["survivor_uid"]) if row else None
