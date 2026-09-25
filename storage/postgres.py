"""PostgreSQL + pgvector storage for durable Neuron knowledge state.

Postgres is the source of truth for source versions, chunks, extracted facts,
findings, demo runs and vector projections. FalkorDB remains the traversal
projection used by the graph canvas and graph algorithms.

The module imports psycopg lazily so the existing SQLite/Qdrant application can
still start while a deployment is being migrated. Story-demo endpoints require
``DATABASE_URL`` and fail with a clear configuration error when it is absent.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterable, Iterator, Sequence


EMBEDDING_DIMENSION = 1536


class PostgresConfigurationError(RuntimeError):
    pass


def database_url() -> str | None:
    return os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresStore:
    def __init__(self, url: str | None = None):
        self.url = url or database_url()
        if not self.url:
            raise PostgresConfigurationError(
                "DATABASE_URL is required for the Postgres/pgvector story demo"
            )

    @contextmanager
    def connect(self, *, register_vectors: bool = True):
        try:
            import psycopg
            from pgvector.psycopg import register_vector
        except ImportError as exc:  # pragma: no cover - deployment configuration
            raise PostgresConfigurationError(
                "Install psycopg[binary] and pgvector before using DATABASE_URL"
            ) from exc
        with psycopg.connect(self.url, autocommit=False) as connection:
            if register_vectors:
                register_vector(connection)
            yield connection

    def bootstrap(self) -> None:
        """Create the durable schema. Safe to call at every startup."""
        statements = [
            "CREATE EXTENSION IF NOT EXISTS vector",
            """
            CREATE TABLE IF NOT EXISTS knowledge_graphs (
                id UUID PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                falkor_name TEXT NOT NULL UNIQUE,
                vector_collection TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS source_records (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                provider TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                external_id TEXT NOT NULL,
                project_ref TEXT,
                name TEXT NOT NULL,
                url TEXT,
                content_hash TEXT NOT NULL,
                primary_node_uid UUID,
                semantic_status TEXT NOT NULL DEFAULT 'pending',
                semantic_priority INTEGER NOT NULL DEFAULT 100,
                update_count INTEGER NOT NULL DEFAULT 0,
                source_time TIMESTAMPTZ,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                deleted_at TIMESTAMPTZ,
                PRIMARY KEY (graph_id, record_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS record_versions (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (graph_id, record_key, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS source_chunks (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                chunk_id UUID NOT NULL,
                chunk_index INTEGER NOT NULL,
                text TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                llm_text TEXT,
                resolution_status TEXT NOT NULL DEFAULT 'unresolved',
                resolution_reason TEXT,
                embedding vector(1536),
                embedded_model TEXT,
                committed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                superseded_at TIMESTAMPTZ,
                PRIMARY KEY (graph_id, record_key, chunk_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS entity_embeddings (
                collection TEXT NOT NULL,
                uid UUID NOT NULL,
                label TEXT NOT NULL,
                content_embedding vector(1536) NOT NULL,
                name_embedding vector(1536) NOT NULL,
                embedded_text TEXT,
                embedded_model TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (collection, uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS fact_ledger (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                fact_uid UUID NOT NULL,
                subject_uid UUID NOT NULL,
                subject_label TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object_uid UUID NOT NULL,
                object_label TEXT NOT NULL,
                source_record_keys TEXT[] NOT NULL DEFAULT '{}',
                evidence TEXT,
                extraction_method TEXT NOT NULL,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                valid_from TIMESTAMPTZ,
                valid_to TIMESTAMPTZ,
                derived BOOLEAN NOT NULL DEFAULT false,
                derived_rule TEXT,
                premise_fact_uids UUID[] NOT NULL DEFAULT '{}',
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (graph_id, fact_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS record_edges (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                rel_type TEXT NOT NULL,
                from_uid UUID NOT NULL,
                to_uid UUID NOT NULL,
                PRIMARY KEY (graph_id, record_key, rel_type, from_uid, to_uid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS extraction_drops (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                record_key TEXT NOT NULL,
                chunk_id UUID NOT NULL,
                reason TEXT NOT NULL,
                subject_kind TEXT,
                subject_name TEXT,
                relation TEXT,
                object_kind TEXT,
                object_name TEXT,
                detail TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS relation_axioms (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                relation TEXT NOT NULL,
                subject_kind TEXT NOT NULL,
                object_kind TEXT NOT NULL,
                extractable BOOLEAN NOT NULL DEFAULT false,
                functional BOOLEAN NOT NULL DEFAULT false,
                is_transitive BOOLEAN NOT NULL DEFAULT false,
                is_symmetric BOOLEAN NOT NULL DEFAULT false,
                is_asymmetric BOOLEAN NOT NULL DEFAULT false,
                inverse_of TEXT,
                sub_property_of TEXT,
                temporal TEXT NOT NULL DEFAULT 'state',
                adopted_batch TEXT,
                PRIMARY KEY (graph_id, relation, subject_kind, object_kind)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS ontology_misses (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                example TEXT,
                count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                dismissed_at TIMESTAMPTZ,
                PRIMARY KEY (graph_id, kind, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS graph_settings (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (graph_id, key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS findings (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                id UUID NOT NULL,
                finding_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                project_ref TEXT,
                trigger_record_key TEXT,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                stale_at TIMESTAMPTZ,
                stale_reason TEXT,
                resolved_at TIMESTAMPTZ,
                PRIMARY KEY (graph_id, id),
                UNIQUE (graph_id, finding_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS finding_evidence (
                graph_id UUID NOT NULL,
                finding_id UUID NOT NULL,
                record_key TEXT NOT NULL,
                chunk_id UUID,
                fact_uid UUID,
                role TEXT NOT NULL,
                excerpt TEXT,
                PRIMARY KEY (graph_id, finding_id, record_key, role),
                FOREIGN KEY (graph_id, finding_id)
                    REFERENCES findings(graph_id, id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wisdom_signals (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                id UUID NOT NULL,
                signal_key TEXT NOT NULL,
                pattern_key TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                statement TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                suggested_action TEXT NOT NULL,
                confidence DOUBLE PRECISION NOT NULL,
                project_ref TEXT,
                finding_id UUID NOT NULL,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (graph_id, id),
                UNIQUE (graph_id, signal_key),
                FOREIGN KEY (graph_id, finding_id)
                    REFERENCES findings(graph_id, id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wisdom_proposals (
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                id UUID NOT NULL,
                proposal_key TEXT NOT NULL,
                wisdom_type TEXT NOT NULL,
                topic_key TEXT NOT NULL,
                title TEXT NOT NULL,
                statement TEXT NOT NULL,
                rationale TEXT NOT NULL,
                recommended_action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'proposed',
                confidence DOUBLE PRECISION NOT NULL,
                scope JSONB NOT NULL DEFAULT '{}'::jsonb,
                properties JSONB NOT NULL DEFAULT '{}'::jsonb,
                version INTEGER NOT NULL DEFAULT 1,
                generation_method TEXT NOT NULL DEFAULT 'llm',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                reviewed_at TIMESTAMPTZ,
                reviewed_by TEXT,
                PRIMARY KEY (graph_id, id),
                UNIQUE (graph_id, proposal_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS wisdom_proposal_evidence (
                graph_id UUID NOT NULL,
                proposal_id UUID NOT NULL,
                signal_id UUID NOT NULL,
                finding_id UUID NOT NULL,
                role TEXT NOT NULL DEFAULT 'supporting',
                PRIMARY KEY (graph_id, proposal_id, signal_id),
                FOREIGN KEY (graph_id, proposal_id)
                    REFERENCES wisdom_proposals(graph_id, id) ON DELETE CASCADE,
                FOREIGN KEY (graph_id, signal_id)
                    REFERENCES wisdom_signals(graph_id, id) ON DELETE CASCADE,
                FOREIGN KEY (graph_id, finding_id)
                    REFERENCES findings(graph_id, id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS graph_outbox (
                id BIGSERIAL PRIMARY KEY,
                graph_id UUID NOT NULL REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload JSONB NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                projected_at TIMESTAMPTZ,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                UNIQUE (graph_id, event_key)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS story_runs (
                id UUID PRIMARY KEY,
                graph_id UUID REFERENCES knowledge_graphs(id) ON DELETE CASCADE,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                progress JSONB NOT NULL DEFAULT '{}'::jsonb,
                error TEXT,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_source_records_graph_provider ON source_records(graph_id, provider)",
            "CREATE INDEX IF NOT EXISTS idx_source_chunks_pending ON source_chunks(graph_id, status, committed_at) WHERE superseded_at IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_fact_ledger_subject ON fact_ledger(graph_id, subject_uid, predicate) WHERE valid_to IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_fact_ledger_object ON fact_ledger(graph_id, object_uid, predicate) WHERE valid_to IS NULL",
            "CREATE INDEX IF NOT EXISTS idx_findings_open ON findings(graph_id, status, severity, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_wisdom_signals_pattern ON wisdom_signals(graph_id, pattern_key, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_wisdom_proposals_status ON wisdom_proposals(graph_id, status, updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_entity_embeddings_label ON entity_embeddings(collection, label)",
            "CREATE INDEX IF NOT EXISTS idx_entity_embeddings_content_hnsw ON entity_embeddings USING hnsw (content_embedding vector_cosine_ops)",
            "CREATE INDEX IF NOT EXISTS idx_entity_embeddings_name_hnsw ON entity_embeddings USING hnsw (name_embedding vector_cosine_ops)",
            "CREATE INDEX IF NOT EXISTS idx_source_chunks_embedding_hnsw ON source_chunks USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL AND superseded_at IS NULL",
            "ALTER TABLE source_chunks ADD COLUMN IF NOT EXISTS llm_text TEXT",
            "ALTER TABLE source_chunks ADD COLUMN IF NOT EXISTS resolution_status TEXT NOT NULL DEFAULT 'unresolved'",
            "ALTER TABLE source_chunks ADD COLUMN IF NOT EXISTS resolution_reason TEXT",
            "ALTER TABLE findings ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "ALTER TABLE findings ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "ALTER TABLE findings ADD COLUMN IF NOT EXISTS stale_at TIMESTAMPTZ",
            "ALTER TABLE findings ADD COLUMN IF NOT EXISTS stale_reason TEXT",
            # No write path populates this yet (see graph/vector_store.py's
            # search_above docstring) -- the column exists so the namespace
            # filter is real once a follow-up wires it into upsert_vectors's
            # callers, and existing rows read back NULL (no namespace).
            "ALTER TABLE entity_embeddings ADD COLUMN IF NOT EXISTS namespace_uid TEXT",
        ]
        # A pristine database does not have a ``vector`` type to register yet.
        # Create the extension using a raw psycopg connection, commit it, and
        # only then register pgvector adapters before creating vector columns.
        with self.connect(register_vectors=False) as connection:
            with connection.cursor() as cursor:
                cursor.execute(statements[0])
            connection.commit()
            from pgvector.psycopg import register_vector
            register_vector(connection)
            with connection.cursor() as cursor:
                for statement in statements[1:]:
                    cursor.execute(statement)
            connection.commit()

    def create_graph(
        self, name: str, display_name: str, falkor_name: str, vector_collection: str,
    ) -> dict[str, Any]:
        graph_id = uuid.uuid4()
        with self.connect() as connection:
            row = connection.execute(
                """
                INSERT INTO knowledge_graphs(id, name, display_name, falkor_name, vector_collection)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, name, display_name, falkor_name, vector_collection, created_at
                """,
                (graph_id, name, display_name, falkor_name, vector_collection),
            ).fetchone()
            connection.commit()
        return self._graph_row(row)

    def graph(self, name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, name, display_name, falkor_name, vector_collection, created_at
                   FROM knowledge_graphs WHERE name = %s""",
                (name,),
            ).fetchone()
        return self._graph_row(row) if row else None

    def list_graphs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, name, display_name, falkor_name, vector_collection, created_at
                   FROM knowledge_graphs ORDER BY created_at"""
            ).fetchall()
        return [self._graph_row(row) for row in rows]

    def delete_graph(self, name: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """DELETE FROM knowledge_graphs WHERE name = %s
                   RETURNING id, name, display_name, falkor_name, vector_collection, created_at""",
                (name,),
            ).fetchone()
            connection.commit()
        return self._graph_row(row) if row else None

    @staticmethod
    def _graph_row(row) -> dict[str, Any]:
        return {
            "id": str(row[0]), "name": row[1], "displayName": row[2],
            "falkorName": row[3], "vectorCollection": row[4],
            "createdAt": row[5].isoformat(),
        }

    def start_run(self, phase: str, graph_id: str | None = None) -> str:
        run_id = uuid.uuid4()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO story_runs(id, graph_id, phase, status) VALUES (%s, %s, %s, 'running')",
                (run_id, graph_id, phase),
            )
            connection.commit()
        return str(run_id)

    def update_run(
        self, run_id: str, *, status: str | None = None,
        progress: dict[str, Any] | None = None, error: str | None = None,
    ) -> None:
        finished = _now() if status in {"completed", "failed"} else None
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE story_runs
                SET status = coalesce(%s, status), progress = coalesce(%s::jsonb, progress),
                    error = %s, finished_at = coalesce(%s, finished_at)
                WHERE id = %s
                """,
                (status, json.dumps(progress) if progress is not None else None, error, finished, run_id),
            )
            connection.commit()

    def run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, graph_id, phase, status, progress, error, started_at, finished_at
                   FROM story_runs WHERE id = %s""",
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "runId": str(row[0]), "graphId": str(row[1]) if row[1] else None,
            "phase": row[2], "status": row[3], "progress": row[4] or {},
            "error": row[5], "startedAt": row[6].isoformat(),
            "finishedAt": row[7].isoformat() if row[7] else None,
        }

    def runs_for_graph(self, graph_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            ids = connection.execute(
                """SELECT id FROM story_runs WHERE graph_id=%s
                   ORDER BY started_at DESC LIMIT %s""",
                (graph_id, limit),
            ).fetchall()
        return [run for row in ids if (run := self.run(str(row[0]))) is not None]

    def findings(self, graph_id: str, *, include_resolved: bool = True) -> list[dict[str, Any]]:
        clause = "" if include_resolved else "AND status = 'open'"
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, finding_key, kind, severity, status, title, summary, reasoning,
                       confidence, project_ref, trigger_record_key, properties,
                       created_at, updated_at, last_seen_at, status_changed_at,
                       stale_at, stale_reason, resolved_at
                FROM findings WHERE graph_id = %s {clause}
                ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC
                """,
                (graph_id,),
            ).fetchall()
        findings = [{
            "id": str(row[0]), "key": row[1], "kind": row[2], "severity": row[3],
            "status": row[4], "title": row[5], "summary": row[6], "reasoning": row[7],
            "confidence": row[8], "projectRef": row[9], "triggerRecordKey": row[10],
            "properties": row[11] or {}, "createdAt": row[12].isoformat(),
            "updatedAt": row[13].isoformat(), "lastSeenAt": row[14].isoformat(),
            "statusChangedAt": row[15].isoformat(),
            "staleAt": row[16].isoformat() if row[16] else None,
            "staleReason": row[17],
            "resolvedAt": row[18].isoformat() if row[18] else None,
            "sources": [],
        } for row in rows]
        by_id = {item["id"]: item for item in findings}
        if not by_id:
            return findings
        with self.connect() as connection:
            evidence_rows = connection.execute(
                """
                SELECT fe.finding_id, fe.record_key, fe.role, fe.excerpt, fe.chunk_id,
                       sr.provider, sr.name, sr.url,
                       coalesce(sr.source_time, sr.updated_at, sr.ingested_at)
                FROM finding_evidence fe
                LEFT JOIN source_records sr
                  ON sr.graph_id=fe.graph_id AND sr.record_key=fe.record_key
                WHERE fe.graph_id=%s AND fe.finding_id=ANY(%s::uuid[])
                ORDER BY fe.finding_id, fe.role, fe.record_key
                """,
                (graph_id, list(by_id)),
            ).fetchall()
        for row in evidence_rows:
            item = by_id.get(str(row[0]))
            if item is None:
                continue
            item["sources"].append({
                "recordKey": row[1], "role": row[2], "excerpt": row[3],
                "chunkId": str(row[4]) if row[4] else None,
                "provider": row[5], "name": row[6], "url": row[7],
                "sourceTime": row[8].isoformat() if row[8] else None,
            })
        return findings

    def upsert_finding(
        self, graph_id: str, *, finding_key: str, kind: str, severity: str,
        title: str, summary: str, reasoning: str, confidence: float,
        project_ref: str | None = None, trigger_record_key: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> str:
        finding_id = uuid.uuid5(uuid.NAMESPACE_URL, f"neuron:{graph_id}:{finding_key}")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO findings(
                    graph_id, id, finding_key, kind, severity, status, title, summary,
                    reasoning, confidence, project_ref, trigger_record_key, properties
                ) VALUES (%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (graph_id, finding_key) DO UPDATE SET
                    kind = excluded.kind, severity = excluded.severity, status = 'open',
                    title = excluded.title, summary = excluded.summary,
                    reasoning = excluded.reasoning, confidence = excluded.confidence,
                    project_ref = excluded.project_ref,
                    trigger_record_key = excluded.trigger_record_key,
                    properties = excluded.properties, updated_at = now(), last_seen_at = now(),
                    status_changed_at = CASE WHEN findings.status <> 'open'
                                             THEN now() ELSE findings.status_changed_at END,
                    stale_at = NULL, stale_reason = NULL, resolved_at = NULL
                """,
                (graph_id, finding_id, finding_key, kind, severity, title, summary,
                 reasoning, confidence, project_ref, trigger_record_key,
                 json.dumps(properties or {})),
            )
            connection.commit()
        return str(finding_id)

    def resolve_finding(self, graph_id: str, finding_key: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE findings SET status='resolved', updated_at=now(), resolved_at=now()
                   WHERE graph_id=%s AND finding_key=%s AND status <> 'resolved'""",
                (graph_id, finding_key),
            )
            connection.commit()
        return cursor.rowcount > 0

    def mark_finding_stale(
        self, graph_id: str, finding_key: str, *, reason: str,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE findings
                   SET status='stale', stale_at=now(), stale_reason=%s,
                       status_changed_at=now(), updated_at=now()
                   WHERE graph_id=%s AND finding_key=%s AND status <> 'stale'""",
                (reason, graph_id, finding_key),
            )
            connection.commit()
        return cursor.rowcount > 0

    def mark_record_findings_stale(
        self, graph_id: str, record_key: str, *, reason: str,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE findings
                   SET status='stale', stale_at=now(), stale_reason=%s,
                       status_changed_at=now(), updated_at=now()
                   WHERE graph_id=%s AND status='open'
                     AND (trigger_record_key=%s OR EXISTS (
                         SELECT 1 FROM finding_evidence fe
                         WHERE fe.graph_id=findings.graph_id AND fe.finding_id=findings.id
                           AND fe.record_key=%s
                     ))""",
                (reason, graph_id, record_key, record_key),
            )
            connection.commit()
        return cursor.rowcount

    def add_finding_evidence(
        self, graph_id: str, finding_id: str, record_key: str, role: str,
        *, chunk_id: str | None = None, fact_uid: str | None = None, excerpt: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO finding_evidence(
                    graph_id, finding_id, record_key, chunk_id, fact_uid, role, excerpt
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (graph_id, finding_id, record_key, role) DO UPDATE SET
                    chunk_id=excluded.chunk_id, fact_uid=excluded.fact_uid, excerpt=excluded.excerpt
                """,
                (graph_id, finding_id, record_key, chunk_id, fact_uid, role, excerpt),
            )
            connection.commit()

    def upsert_wisdom_signal(
        self, graph_id: str, *, signal_key: str, pattern_key: str,
        signal_type: str, statement: str, reasoning: str,
        suggested_action: str, confidence: float, finding_id: str,
        project_ref: str | None = None,
        properties: dict[str, Any] | None = None,
    ) -> str:
        signal_id = uuid.uuid5(uuid.NAMESPACE_URL, f"neuron:{graph_id}:wisdom-signal:{signal_key}")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wisdom_signals(
                    graph_id, id, signal_key, pattern_key, signal_type,
                    statement, reasoning, suggested_action, confidence,
                    project_ref, finding_id, properties
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (graph_id, signal_key) DO UPDATE SET
                    pattern_key=excluded.pattern_key, signal_type=excluded.signal_type,
                    statement=excluded.statement, reasoning=excluded.reasoning,
                    suggested_action=excluded.suggested_action,
                    confidence=excluded.confidence, project_ref=excluded.project_ref,
                    finding_id=excluded.finding_id, properties=excluded.properties,
                    updated_at=now()
                """,
                (graph_id, signal_id, signal_key, pattern_key, signal_type,
                 statement, reasoning, suggested_action, confidence,
                 project_ref, finding_id, json.dumps(properties or {})),
            )
            connection.commit()
        return str(signal_id)

    def wisdom_signals(
        self, graph_id: str, *, pattern_key: str | None = None,
    ) -> list[dict[str, Any]]:
        pattern_clause = "AND ws.pattern_key=%s" if pattern_key else ""
        params: list[Any] = [graph_id]
        if pattern_key:
            params.append(pattern_key)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT ws.id, ws.signal_key, ws.pattern_key, ws.signal_type,
                       ws.statement, ws.reasoning, ws.suggested_action,
                       ws.confidence, ws.project_ref, ws.finding_id,
                       ws.properties, ws.created_at, ws.updated_at,
                       f.title, f.summary, f.status
                FROM wisdom_signals ws
                JOIN findings f ON f.graph_id=ws.graph_id AND f.id=ws.finding_id
                WHERE ws.graph_id=%s {pattern_clause}
                ORDER BY ws.created_at
                """,
                params,
            ).fetchall()
        return [{
            "id": str(row[0]), "key": row[1], "patternKey": row[2],
            "signalType": row[3], "statement": row[4], "reasoning": row[5],
            "suggestedAction": row[6], "confidence": float(row[7]),
            "projectRef": row[8], "findingId": str(row[9]),
            "properties": row[10] or {}, "createdAt": row[11].isoformat(),
            "updatedAt": row[12].isoformat(), "findingTitle": row[13],
            "findingSummary": row[14], "findingStatus": row[15],
        } for row in rows]

    def upsert_wisdom_proposal(
        self, graph_id: str, *, proposal_key: str, wisdom_type: str,
        topic_key: str, title: str, statement: str, rationale: str,
        recommended_action: str, confidence: float,
        scope: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
        generation_method: str = "llm",
    ) -> str:
        proposal_id = uuid.uuid5(
            uuid.NAMESPACE_URL, f"neuron:{graph_id}:wisdom-proposal:{proposal_key}"
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wisdom_proposals(
                    graph_id, id, proposal_key, wisdom_type, topic_key, title,
                    statement, rationale, recommended_action, confidence,
                    scope, properties, generation_method
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                ON CONFLICT (graph_id, proposal_key) DO UPDATE SET
                    wisdom_type=excluded.wisdom_type, topic_key=excluded.topic_key,
                    title=excluded.title, statement=excluded.statement,
                    rationale=excluded.rationale,
                    recommended_action=excluded.recommended_action,
                    confidence=excluded.confidence, scope=excluded.scope,
                    properties=excluded.properties,
                    generation_method=excluded.generation_method, updated_at=now()
                """,
                (graph_id, proposal_id, proposal_key, wisdom_type, topic_key,
                 title, statement, rationale, recommended_action, confidence,
                 json.dumps(scope or {}), json.dumps(properties or {}), generation_method),
            )
            connection.commit()
        return str(proposal_id)

    def add_wisdom_proposal_evidence(
        self, graph_id: str, proposal_id: str, signal_id: str,
        finding_id: str, *, role: str = "supporting",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wisdom_proposal_evidence(
                    graph_id, proposal_id, signal_id, finding_id, role
                ) VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (graph_id, proposal_id, signal_id) DO UPDATE SET
                    finding_id=excluded.finding_id, role=excluded.role
                """,
                (graph_id, proposal_id, signal_id, finding_id, role),
            )
            connection.commit()

    def review_wisdom_proposal(
        self, graph_id: str, proposal_id: str, *, decision: str,
        reviewed_by: str = "story-demo-user",
    ) -> bool:
        if decision not in {"active", "rejected"}:
            raise ValueError(f"unsupported wisdom decision: {decision}")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE wisdom_proposals
                SET status=%s, reviewed_at=now(), reviewed_by=%s, updated_at=now()
                WHERE graph_id=%s AND id=%s AND status='proposed'
                """,
                (decision, reviewed_by, graph_id, proposal_id),
            )
            connection.commit()
        return cursor.rowcount > 0

    def wisdom_proposals(self, graph_id: str) -> list[dict[str, Any]]:
        findings = {item["id"]: item for item in self.findings(graph_id)}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, proposal_key, wisdom_type, topic_key, title, statement,
                       rationale, recommended_action, status, confidence, scope,
                       properties, version, generation_method, created_at,
                       updated_at, reviewed_at, reviewed_by
                FROM wisdom_proposals WHERE graph_id=%s
                ORDER BY CASE status WHEN 'proposed' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
                         updated_at DESC
                """,
                (graph_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                """
                SELECT proposal_id, signal_id, finding_id, role
                FROM wisdom_proposal_evidence WHERE graph_id=%s
                ORDER BY proposal_id, role, finding_id
                """,
                (graph_id,),
            ).fetchall()
        proposals = [{
            "id": str(row[0]), "key": row[1], "type": row[2],
            "topicKey": row[3], "title": row[4], "statement": row[5],
            "rationale": row[6], "recommendedAction": row[7],
            "status": row[8], "confidence": float(row[9]),
            "scope": row[10] or {}, "properties": row[11] or {},
            "version": int(row[12]), "generationMethod": row[13],
            "createdAt": row[14].isoformat(), "updatedAt": row[15].isoformat(),
            "reviewedAt": row[16].isoformat() if row[16] else None,
            "reviewedBy": row[17], "supportingFindings": [], "signalIds": [],
        } for row in rows]
        by_id = {item["id"]: item for item in proposals}
        for proposal_id, signal_id, finding_id, role in evidence_rows:
            proposal = by_id.get(str(proposal_id))
            if proposal is None:
                continue
            proposal["signalIds"].append(str(signal_id))
            finding = findings.get(str(finding_id))
            if finding and not any(item["id"] == finding["id"] for item in proposal["supportingFindings"]):
                proposal["supportingFindings"].append({**finding, "role": role})
        return proposals

    def current_records(
        self, graph_id: str, *, providers: Sequence[str] | None = None,
        project_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["graph_id=%s", "deleted_at IS NULL"]
        params: list[Any] = [graph_id]
        if providers:
            clauses.append("provider = ANY(%s::text[])")
            params.append(list(providers))
        if project_ref:
            clauses.append("project_ref=%s")
            params.append(project_ref)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT record_key, provider, entity_type, project_ref, name,
                           metadata, source_time
                    FROM source_records WHERE {' AND '.join(clauses)}""",
                params,
            ).fetchall()
        return [{
            "record_key": row[0], "provider": row[1], "entity_type": row[2],
            "project_ref": row[3], "name": row[4], "metadata": row[5] or {},
            "source_time": row[6].isoformat() if row[6] else None,
        } for row in rows]

    def unembedded_chunks(self, graph_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return live chunks that still need a pgvector representation."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT record_key, chunk_id, chunk_index, text
                   FROM source_chunks
                   WHERE graph_id=%s AND superseded_at IS NULL AND embedding IS NULL
                   ORDER BY committed_at, record_key, chunk_index
                   LIMIT %s""",
                (graph_id, limit),
            ).fetchall()
        return [{
            "record_key": row[0], "chunk_id": str(row[1]),
            "chunk_index": int(row[2]), "text": row[3],
        } for row in rows]

    def chunk_resolution_counts(
        self, graph_id: str, *, since: datetime | None = None,
        linked_record_keys: Iterable[str] = (),
    ) -> dict[str, int]:
        since_clause = "AND committed_at >= %s" if since else ""
        linked_keys = list(linked_record_keys)
        params: list[Any] = [linked_keys, linked_keys, graph_id]
        if since:
            params.append(since)
        with self.connect() as connection:
            row = connection.execute(
                f"""
                SELECT count(*),
                       count(*) FILTER (WHERE resolution_status='deterministic'),
                       count(*) FILTER (WHERE resolution_status='partial'),
                       count(*) FILTER (WHERE resolution_status='unresolved'),
                       count(*) FILTER (
                           WHERE resolution_status<>'deterministic'
                             AND record_key=ANY(%s::text[])
                       ),
                       count(*) FILTER (
                           WHERE resolution_status<>'deterministic'
                             AND NOT (record_key=ANY(%s::text[]))
                       )
                FROM source_chunks
                WHERE graph_id=%s AND superseded_at IS NULL {since_clause}
                """,
                params,
            ).fetchone()
        return {
            "chunks_total": int(row[0]),
            "chunks_deterministic": int(row[1]),
            "chunks_partial": int(row[2]),
            "chunks_unresolved": int(row[3]),
            "chunks_llm_skipped": int(row[1]),
            "chunks_hybrid": int(row[4]),
            "chunks_llm_only": int(row[5]),
        }

    def set_chunk_embeddings(
        self, graph_id: str, rows: Sequence[dict[str, Any]], *, model: str,
    ) -> None:
        if not rows:
            return
        with self.connect() as connection:
            connection.cursor().executemany(
                """UPDATE source_chunks
                   SET embedding=%s, embedded_model=%s
                   WHERE graph_id=%s AND record_key=%s AND chunk_id=%s""",
                [(
                    row["embedding"], model, graph_id,
                    row["record_key"], row["chunk_id"],
                ) for row in rows],
            )
            connection.commit()

    def search_related_chunks(
        self, graph_id: str, embedding: list[float], *,
        exclude_record_key: str, before: datetime | None = None,
        limit: int = 5, min_similarity: float = 0.55,
    ) -> list[dict[str, Any]]:
        """Retrieve a small evidence neighbourhood for pre-ingest reasoning.

        ``before`` is the start of the current ingestion event. It prevents
        records arriving in the same event from being presented to the model
        as if they were historical knowledge.
        """
        before_clause = "AND c.committed_at < %s" if before else ""
        params: list[Any] = [embedding, graph_id, exclude_record_key]
        if before:
            params.append(before)
        params.extend([embedding, min_similarity, embedding, limit])
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.record_key, c.chunk_id, c.text, r.provider, r.name,
                       1 - (c.embedding <=> %s::vector) AS similarity
                FROM source_chunks c
                JOIN source_records r
                  ON r.graph_id=c.graph_id AND r.record_key=c.record_key
                WHERE c.graph_id=%s AND c.record_key<>%s
                  AND c.embedding IS NOT NULL AND c.superseded_at IS NULL
                  AND r.deleted_at IS NULL {before_clause}
                  AND 1 - (c.embedding <=> %s::vector) >= %s
                ORDER BY c.embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [{
            "record_key": row[0], "chunk_id": str(row[1]), "text": row[2],
            "provider": row[3], "name": row[4], "similarity": float(row[5]),
        } for row in rows]

    def search_related_nodes(
        self, collection: str, embedding: list[float], *, limit: int = 5,
        min_similarity: float = 0.55, before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest existing graph entities for bounded LLM adjudication."""
        before_clause = "AND updated_at < %s" if before else ""
        params: list[Any] = [embedding, collection, embedding, min_similarity]
        if before:
            params.append(before)
        params.extend([embedding, limit])
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT uid, label, embedded_text,
                       1 - (content_embedding <=> %s::vector) AS similarity
                FROM entity_embeddings
                WHERE collection=%s
                  AND 1 - (content_embedding <=> %s::vector) >= %s
                  {before_clause}
                ORDER BY content_embedding <=> %s::vector
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [{
            "uid": str(row[0]), "label": row[1], "text": row[2] or "",
            "similarity": float(row[3]),
        } for row in rows]

    def invalidate_facts(
        self, graph_id: str, predicate: str, *, subject_uids: Sequence[str],
    ) -> None:
        if not subject_uids:
            return
        with self.connect() as connection:
            connection.execute(
                """UPDATE fact_ledger SET valid_to=now()
                   WHERE graph_id=%s AND predicate=%s AND valid_to IS NULL
                     AND subject_uid=ANY(%s::uuid[])""",
                (graph_id, predicate, list(subject_uids)),
            )
            connection.commit()

    def upsert_facts(self, graph_id: str, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        values = [(
            graph_id, row["fact_uid"], row["subject_uid"], row["subject_label"],
            row["predicate"], row["object_uid"], row["object_label"],
            row.get("source_record_keys") or [], row.get("evidence"),
            row.get("extraction_method") or "deterministic", row.get("confidence", 1.0),
            row.get("valid_from"), row.get("valid_to"), row.get("derived", False),
            row.get("derived_rule"), row.get("premise_fact_uids") or [],
            json.dumps(row.get("properties") or {}),
        ) for row in rows]
        with self.connect() as connection:
            connection.cursor().executemany(
                """
                INSERT INTO fact_ledger(
                    graph_id, fact_uid, subject_uid, subject_label, predicate,
                    object_uid, object_label, source_record_keys, evidence,
                    extraction_method, confidence, valid_from, valid_to, derived,
                    derived_rule, premise_fact_uids, properties
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT (graph_id, fact_uid) DO UPDATE SET
                    source_record_keys=(
                        SELECT array_agg(DISTINCT item)
                        FROM unnest(fact_ledger.source_record_keys || excluded.source_record_keys) item
                    ),
                    evidence=coalesce(excluded.evidence, fact_ledger.evidence),
                    confidence=excluded.confidence, valid_to=excluded.valid_to,
                    derived=excluded.derived, derived_rule=excluded.derived_rule,
                    premise_fact_uids=excluded.premise_fact_uids,
                    properties=excluded.properties, last_confirmed_at=now()
                """,
                values,
            )
            connection.commit()

    def vector_upsert(self, collection: str, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        values = [(
            collection, row["uid"], row["label"], row["embedding"],
            row.get("name_embedding") or row["embedding"], row.get("embedded_text"),
            row.get("embedded_model") or "text-embedding-3-small",
        ) for row in rows]
        with self.connect() as connection:
            connection.cursor().executemany(
                """
                INSERT INTO entity_embeddings(
                    collection, uid, label, content_embedding, name_embedding,
                    embedded_text, embedded_model
                ) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (collection, uid) DO UPDATE SET
                    label=excluded.label, content_embedding=excluded.content_embedding,
                    name_embedding=excluded.name_embedding,
                    embedded_text=excluded.embedded_text,
                    embedded_model=excluded.embedded_model, updated_at=now()
                """,
                values,
            )
            connection.commit()

    def vector_search(
        self, collection: str, embedding: list[float], *, label: str | None,
        limit: int, channel: str,
        min_similarity: float | None = None, max_similarity: float | None = None,
        namespace_uid: str | None = None,
    ) -> list[tuple[str, float]]:
        """`min_similarity`/`max_similarity`/`namespace_uid` back
        `graph.vector_store.search_above` (25-plan.md §4.2's threshold,
        multi-candidate ladder rungs). All three default to None, which
        reproduces the exact query `search()`'s plain top-k caller has
        always run -- this is an additive extension, not a behavior change,
        for callers that don't pass them."""
        column = "name_embedding" if channel == "name" else "content_embedding"
        clauses = []
        params: list[Any] = [embedding, collection]
        if label:
            clauses.append("AND label = %s")
            params.append(label)
        if namespace_uid is not None:
            clauses.append("AND namespace_uid = %s")
            params.append(namespace_uid)
        if min_similarity is not None:
            clauses.append(f"AND 1 - ({column} <=> %s::vector) >= %s")
            params.extend([embedding, min_similarity])
        if max_similarity is not None:
            clauses.append(f"AND 1 - ({column} <=> %s::vector) < %s")
            params.extend([embedding, max_similarity])
        extra_clause = " ".join(clauses)
        params.extend([embedding, limit])
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT uid, 1 - ({column} <=> %s::vector) AS similarity
                FROM entity_embeddings
                WHERE collection = %s {extra_clause}
                ORDER BY {column} <=> %s::vector
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [(str(row[0]), float(row[1])) for row in rows]

    def vector_delete(self, collection: str, uids: Sequence[str]) -> None:
        if not uids:
            return
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM entity_embeddings WHERE collection=%s AND uid = ANY(%s::uuid[])",
                (collection, list(uids)),
            )
            connection.commit()

    def vector_count(self, collection: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT count(*) FROM entity_embeddings WHERE collection=%s", (collection,)
            ).fetchone()
        return int(row[0])

    def vector_clear(self, collection: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM entity_embeddings WHERE collection=%s", (collection,))
            connection.commit()


class PostgresVectorClient:
    """Small compatibility handle consumed by ``graph.vector_store``."""

    def __init__(self, store: PostgresStore | None = None):
        self.store = store or PostgresStore()

    def collection_exists(self, collection: str) -> bool:
        # Collections are namespaces in one table, so an empty collection is
        # valid without a registry row.
        return True

    def delete_collection(self, collection: str) -> None:
        self.store.vector_clear(collection)
