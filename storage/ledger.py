"""Postgres implementation of the connector-ledger contract used by ingestion."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Iterable

from connectors.core.actions import RecordAction, resolve_action
from connectors.core.ledger import (
    ChunkWrite,
    ChunkDiff,
    ExtractionDrop,
    LedgerEntry,
    PendingChunk,
    RecordEdgeRef,
    SemanticStatus,
    normalize_chunk_write,
)
from connectors.core.models import SourceRecord
from storage.postgres import PostgresStore


def _now() -> datetime:
    return datetime.now(UTC)


def _json_default(value):
    """Serialize immutable connector metadata without weakening its model."""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _json(value) -> str:
    return json.dumps(value, default=_json_default)


class PostgresLedger:
    """Per-graph ledger backed by shared PostgreSQL tables.

    ``stage`` is called by the fixture adapter before a normal pipeline writer.
    The writer continues using its unchanged ``prepare_record``/``commit``
    contract, while the staged canonical record gives Postgres the complete
    source content and metadata needed for history and rebuilding Falkor.
    """

    def __init__(self, store: PostgresStore, graph_id: str):
        self.store = store
        self.graph_id = graph_id
        self._staged: dict[str, tuple[SourceRecord, str | None]] = {}

    def stage(self, record: SourceRecord, project_ref: str | None = None) -> None:
        self._staged[record.record_key] = (record, project_ref)

    def get(self, record_key: str) -> LedgerEntry | None:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT record_key, content_hash, primary_node_uid, semantic_status,
                          semantic_priority, update_count, updated_at
                   FROM source_records WHERE graph_id=%s AND record_key=%s""",
                (self.graph_id, record_key),
            ).fetchone()
        if not row:
            return None
        return LedgerEntry(
            record_key=row[0], content_hash=row[1],
            primary_node_uid=str(row[2]) if row[2] else None,
            semantic_status=row[3], semantic_priority=int(row[4]),
            update_count=int(row[5]), updated_at=row[6].isoformat(),
        )

    def plan(self, record_key: str, current_hash: str | None, *, deleted: bool = False) -> RecordAction:
        previous = self.get(record_key)
        return resolve_action(previous.content_hash if previous else None, current_hash, deleted=deleted)

    def commit(
        self, record_key: str, content_hash: str, *, primary_node_uid: str | None = None,
        semantic_status: SemanticStatus | str = SemanticStatus.PENDING,
    ) -> None:
        staged = self._staged.get(record_key)
        if staged is None:
            raise RuntimeError(f"PostgresLedger.commit called before stage for {record_key}")
        record, project_ref = staged
        previous = self.get(record_key)
        if previous and previous.content_hash != content_hash:
            self.store.mark_record_findings_stale(
                self.graph_id, record_key,
                reason="The source record changed; this finding is retained as history pending recomputation.",
            )
        priority = 200 if previous and previous.content_hash != content_hash else 100
        update_count = (previous.update_count + 1) if previous and previous.content_hash != content_hash else (previous.update_count if previous else 0)
        metadata = {**record.metadata, "content": record.content}
        with self.store.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_records(
                    graph_id, record_key, provider, connection_id, entity_type,
                    external_id, project_ref, name, url, content_hash,
                    primary_node_uid, semantic_status, semantic_priority,
                    update_count, source_time, metadata
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (graph_id, record_key) DO UPDATE SET
                    project_ref=excluded.project_ref, name=excluded.name, url=excluded.url,
                    content_hash=excluded.content_hash,
                    primary_node_uid=coalesce(excluded.primary_node_uid, source_records.primary_node_uid),
                    semantic_status=excluded.semantic_status,
                    semantic_priority=excluded.semantic_priority,
                    update_count=excluded.update_count, source_time=excluded.source_time,
                    metadata=excluded.metadata, updated_at=now(), deleted_at=NULL
                """,
                (
                    self.graph_id, record.record_key, record.provider, record.connection_id,
                    record.entity_type, record.external_id, project_ref, record.name,
                    record.url, content_hash, primary_node_uid, str(semantic_status), priority,
                    update_count, record.reference_time, _json(metadata),
                ),
            )
            version = connection.execute(
                """SELECT coalesce(max(version), 0) FROM record_versions
                   WHERE graph_id=%s AND record_key=%s""",
                (self.graph_id, record_key),
            ).fetchone()[0]
            exists = connection.execute(
                """SELECT 1 FROM record_versions
                   WHERE graph_id=%s AND record_key=%s AND content_hash=%s""",
                (self.graph_id, record_key, content_hash),
            ).fetchone()
            if not exists:
                connection.execute(
                    """INSERT INTO record_versions(
                           graph_id, record_key, version, content_hash, content, metadata
                       ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)""",
                    (self.graph_id, record_key, int(version) + 1, content_hash,
                     record.content, _json(record.metadata)),
                )
            connection.commit()

    def set_semantic_status(self, record_key: str, status: SemanticStatus | str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """UPDATE source_records SET semantic_status=%s, updated_at=now()
                   WHERE graph_id=%s AND record_key=%s""",
                (str(status), self.graph_id, record_key),
            )
            connection.commit()

    def save_chunks(
        self, record_key: str, chunks: Iterable[tuple[str, int, str] | ChunkWrite],
        *, adopt_from: str | None = None,
    ) -> ChunkDiff:
        incoming = [normalize_chunk_write(item) for item in chunks]
        incoming_ids = {item.chunk_id for item in incoming}
        kept = added = reused_done = 0
        with self.store.connect() as connection:
            current = {
                str(row[0]): (row[1], row[2])
                for row in connection.execute(
                    """SELECT chunk_id, text, status FROM source_chunks
                       WHERE graph_id=%s AND record_key=%s AND superseded_at IS NULL""",
                    (self.graph_id, record_key),
                ).fetchall()
            }
            source_done: dict[str, str] = {}
            if adopt_from:
                source_done = {
                    row[0]: str(row[1])
                    for row in connection.execute(
                        """SELECT text, status FROM source_chunks
                           WHERE graph_id=%s AND record_key=%s AND superseded_at IS NULL""",
                        (self.graph_id, adopt_from),
                    ).fetchall()
                    if row[1] == SemanticStatus.DONE
                }
            for item in incoming:
                chunk_id, index, text = item.chunk_id, item.chunk_index, item.text
                content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                status = source_done.get(text, item.status)
                if chunk_id in current:
                    kept += 1
                    if current[chunk_id][1] == SemanticStatus.DONE:
                        reused_done += 1
                    kept_status = (
                        SemanticStatus.DONE
                        if current[chunk_id][1] == SemanticStatus.DONE
                        or str(item.status) == str(SemanticStatus.DONE)
                        else SemanticStatus.PENDING
                    )
                    connection.execute(
                        """UPDATE source_chunks SET chunk_index=%s, text=%s, content_hash=%s,
                                  status=%s, llm_text=%s, resolution_status=%s, resolution_reason=%s,
                                  superseded_at=NULL
                           WHERE graph_id=%s AND record_key=%s AND chunk_id=%s""",
                        (index, text, content_hash, str(kept_status), item.llm_text, item.resolution_status,
                         item.resolution_reason, self.graph_id, record_key, chunk_id),
                    )
                else:
                    added += 1
                    reused_done += status == SemanticStatus.DONE
                    connection.execute(
                        """INSERT INTO source_chunks(
                               graph_id, record_key, chunk_id, chunk_index, text,
                               content_hash, status, llm_text, resolution_status, resolution_reason
                           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (self.graph_id, record_key, chunk_id, index, text, content_hash,
                         str(status), item.llm_text, item.resolution_status, item.resolution_reason),
                    )
            stale = [chunk_id for chunk_id in current if chunk_id not in incoming_ids]
            if stale:
                connection.execute(
                    """UPDATE source_chunks SET superseded_at=now()
                       WHERE graph_id=%s AND record_key=%s AND chunk_id=ANY(%s::uuid[])""",
                    (self.graph_id, record_key, stale),
                )
            connection.commit()
        return ChunkDiff(kept=kept, added=added, superseded=len(stale), reused_done=reused_done)

    def find_moved_from(self, record_key: str, content_hash: str) -> LedgerEntry | None:
        prefix = record_key.split(":", 2)[:2]
        like = ":".join(prefix) + ":%"
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT record_key FROM source_records
                   WHERE graph_id=%s AND record_key<>%s AND record_key LIKE %s
                     AND content_hash=%s ORDER BY updated_at DESC LIMIT 1""",
                (self.graph_id, record_key, like, content_hash),
            ).fetchone()
        return self.get(row[0]) if row else None

    def pending_chunks(self, limit: int, record_prefix: str | None = None) -> list[PendingChunk]:
        prefix_clause = "AND c.record_key LIKE %s" if record_prefix else ""
        params: list[object] = [self.graph_id]
        if record_prefix:
            params.append(record_prefix + "%")
        params.append(limit)
        with self.store.connect() as connection:
            rows = connection.execute(
                f"""SELECT c.record_key, c.chunk_id, c.chunk_index,
                           coalesce(c.llm_text, c.text), c.text
                    FROM source_chunks c JOIN source_records r
                      ON r.graph_id=c.graph_id AND r.record_key=c.record_key
                    WHERE c.graph_id=%s AND c.status='pending'
                      AND c.superseded_at IS NULL {prefix_clause}
                    ORDER BY r.semantic_priority DESC, c.committed_at, c.chunk_index
                    LIMIT %s""",
                params,
            ).fetchall()
        return [PendingChunk(row[0], str(row[1]), int(row[2]), row[3], row[4]) for row in rows]

    def commit_chunk(
        self, record_key: str, chunk_id: str,
        status: SemanticStatus | str = SemanticStatus.DONE,
    ) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """UPDATE source_chunks SET status=%s WHERE graph_id=%s
                   AND record_key=%s AND chunk_id=%s""",
                (str(status), self.graph_id, record_key, chunk_id),
            )
            connection.commit()

    def record_fully_processed(self, record_key: str) -> bool:
        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT count(*) FILTER (WHERE status <> 'done')
                   FROM source_chunks WHERE graph_id=%s AND record_key=%s
                     AND superseded_at IS NULL""",
                (self.graph_id, record_key),
            ).fetchone()
        return int(row[0]) == 0

    def record_drops(self, record_key: str, chunk_id: str, drops: list[ExtractionDrop]) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM extraction_drops WHERE graph_id=%s AND record_key=%s AND chunk_id=%s",
                (self.graph_id, record_key, chunk_id),
            )
            if drops:
                connection.cursor().executemany(
                    """INSERT INTO extraction_drops(
                           graph_id, record_key, chunk_id, reason, subject_kind,
                           subject_name, relation, object_kind, object_name, detail
                       ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(
                        self.graph_id, record_key, chunk_id, str(item.reason),
                        item.subject_kind, item.subject_name, item.relation,
                        item.object_kind, item.object_name, item.detail,
                    ) for item in drops],
                )
            connection.commit()

    def record_ingestion_assessments(
        self, record_key: str, chunk_id: str, assessments: list[dict],
    ) -> None:
        """Persist validated LLM judgements as user-visible findings.

        Ordinary additions/updates stay in the graph without alert noise.
        Reprocessing the same chunk first resolves its older LLM findings so
        changed evidence cannot leave a stale warning open.
        """
        prefix = f"llm:{record_key}:{chunk_id}:"
        for finding in self.store.findings(self.graph_id):
            if finding["key"].startswith(prefix):
                self.store.mark_finding_stale(
                    self.graph_id, finding["key"],
                    reason="The evidence chunk was reprocessed and no longer produced this finding.",
                )

        with self.store.connect() as connection:
            row = connection.execute(
                """SELECT project_ref FROM source_records
                   WHERE graph_id=%s AND record_key=%s""",
                (self.graph_id, record_key),
            ).fetchone()
        project_ref = row[0] if row else None

        for item in assessments:
            if not item.get("should_flag"):
                continue
            topic_key = "-".join(
                part for part in re.sub(
                    r"[^a-z0-9]+", "-", str(item.get("topic_key") or item.get("title") or "finding").lower()
                ).strip("-").split("-") if part
            )[:100]
            finding_key = (
                f"llm:{project_ref or record_key.split(':', 1)[0]}:"
                f"{item.get('action') or 'review'}:{topic_key}"
            )
            finding_id = self.store.upsert_finding(
                self.graph_id,
                finding_key=finding_key,
                kind=f"llm_{item.get('action') or 'review'}",
                severity=str(item.get("severity") or "warning"),
                title=str(item.get("title") or "Evidence requires review"),
                summary=str(item.get("summary") or ""),
                reasoning=str(item.get("reasoning") or ""),
                confidence=float(item.get("confidence") or 0.5),
                project_ref=project_ref,
                trigger_record_key=record_key,
                properties={
                    "source": "selective_ingestion_llm",
                    "chunkId": chunk_id,
                    "action": item.get("action"),
                    "topicKey": topic_key,
                    "relatedCandidateUids": item.get("related_candidate_uids") or [],
                },
            )
            self.store.add_finding_evidence(
                self.graph_id, finding_id, record_key, "new_evidence",
                chunk_id=chunk_id, excerpt=str(item.get("evidence") or ""),
            )

    def seed_axioms_if_empty(self, rows: list[dict]) -> int:
        with self.store.connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM relation_axioms WHERE graph_id=%s", (self.graph_id,)
            ).fetchone()[0]
            if count:
                return 0
            connection.cursor().executemany(
                """INSERT INTO relation_axioms(
                       graph_id, relation, subject_kind, object_kind, extractable,
                       functional, is_transitive, is_symmetric, is_asymmetric,
                       inverse_of, sub_property_of, temporal, adopted_batch
                   ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(
                    self.graph_id, row["relation"], row["subject_kind"], row["object_kind"],
                    row.get("extractable", False), row.get("functional", False),
                    row.get("is_transitive", False), row.get("is_symmetric", False),
                    row.get("is_asymmetric", False), row.get("inverse_of"),
                    row.get("sub_property_of"), row.get("temporal", "state"),
                    row.get("adopted_batch"),
                ) for row in rows],
            )
            connection.commit()
        return len(rows)

    def axiom_rows(self) -> list[dict]:
        columns = (
            "relation", "subject_kind", "object_kind", "extractable", "functional",
            "is_transitive", "is_symmetric", "is_asymmetric", "inverse_of",
            "sub_property_of", "temporal", "adopted_batch",
        )
        with self.store.connect() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM relation_axioms WHERE graph_id=%s",
                (self.graph_id,),
            ).fetchall()
        return [dict(zip(columns, row)) for row in rows]

    def record_miss(self, kind: str, key: str, example: str | None = None) -> None:
        with self.store.connect() as connection:
            connection.execute(
                """INSERT INTO ontology_misses(graph_id, kind, key, example)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (graph_id, kind, key) DO UPDATE SET
                     count=ontology_misses.count+1,
                     example=coalesce(excluded.example, ontology_misses.example),
                     last_seen_at=now()""",
                (self.graph_id, kind, key, example),
            )
            connection.commit()

    def record_edge(self, record_key: str, rel_type: str, from_uid: str, to_uid: str) -> None:
        self.record_edges_batch(record_key, [RecordEdgeRef(rel_type, from_uid, to_uid)])

    def record_edges_batch(self, record_key: str, edges: list[RecordEdgeRef]) -> None:
        if not edges:
            return
        with self.store.connect() as connection:
            connection.cursor().executemany(
                """INSERT INTO record_edges(graph_id, record_key, rel_type, from_uid, to_uid)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                [(self.graph_id, record_key, edge.rel_type, edge.from_uid, edge.to_uid) for edge in edges],
            )
            connection.commit()

    def edges_for_record(self, record_key: str) -> list[RecordEdgeRef]:
        with self.store.connect() as connection:
            rows = connection.execute(
                """SELECT rel_type, from_uid, to_uid FROM record_edges
                   WHERE graph_id=%s AND record_key=%s""",
                (self.graph_id, record_key),
            ).fetchall()
        return [RecordEdgeRef(row[0], str(row[1]), str(row[2])) for row in rows]

    def clear_edges(self, record_key: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "DELETE FROM record_edges WHERE graph_id=%s AND record_key=%s",
                (self.graph_id, record_key),
            )
            connection.commit()

    def record_keys_with_prefix(self, prefix: str) -> list[str]:
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT record_key FROM source_records WHERE graph_id=%s AND record_key LIKE %s",
                (self.graph_id, prefix + "%"),
            ).fetchall()
        return [row[0] for row in rows]

    def commit_delete(self, record_key: str) -> None:
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE source_records SET deleted_at=now() WHERE graph_id=%s AND record_key=%s",
                (self.graph_id, record_key),
            )
            connection.execute(
                "UPDATE source_chunks SET superseded_at=now() WHERE graph_id=%s AND record_key=%s AND superseded_at IS NULL",
                (self.graph_id, record_key),
            )
            connection.commit()
