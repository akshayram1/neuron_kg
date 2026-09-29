"""Small durable leased job queue for connector syncs.

SQLite is sufficient at the current scale and keeps execution resumable across
API restarts without introducing Temporal/Prefect before operational need. The
same store runs on Postgres (``NEURON_SQL_BACKEND=postgres``) via
``storage.sql_backend``; there ``claim()`` uses ``FOR UPDATE SKIP LOCKED``
instead of SQLite's database-wide ``BEGIN IMMEDIATE`` write lock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from storage.sql_backend import connect, is_postgres


@dataclass(frozen=True)
class ConnectorJob:
    job_id: str
    provider: str
    payload: dict[str, Any]
    status: str
    attempts: int
    max_attempts: int
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    created_at: str
    updated_at: str
    last_error: str | None


class ConnectorJobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS connector_jobs (
                    job_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS connector_jobs_claim
                    ON connector_jobs(status, available_at, created_at);
                """
            )

    def _connect(self):
        db = connect(self.path, timeout=30)
        if not is_postgres(db):
            db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _job(row: Any) -> ConnectorJob:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        return ConnectorJob(**data)

    def enqueue(
        self, job_id: str, provider: str, payload: dict[str, Any], *, max_attempts: int = 3
    ) -> None:
        now = self._now().isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO connector_jobs(
                       job_id, provider, payload_json, status, attempts, max_attempts,
                       available_at, created_at, updated_at
                   ) VALUES (?, ?, ?, 'queued', 0, ?, ?, ?, ?)""",
                (job_id, provider, json.dumps(payload, separators=(",", ":")),
                 max_attempts, now, now, now),
            )

    # A lease that expired while its worker was on the final attempt can never
    # be claimed again (attempts < max_attempts), so it is dead-lettered
    # instead of parked in 'retry' forever.
    _RECOVER_SET = """SET status=CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry' END,
                       lease_owner=NULL, lease_expires_at=NULL,
                       available_at=?, updated_at=?,
                       last_error=coalesce(last_error, 'Worker lease expired; recovered')"""
    _CLAIMABLE = """status IN ('queued', 'retry') AND available_at <= ?
                     AND attempts < max_attempts"""

    def claim(self, worker_id: str, *, lease_seconds: int = 3600) -> ConnectorJob | None:
        now = self._now()
        now_iso = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            if is_postgres(db):
                return self._claim_postgres(db, worker_id, now_iso, lease_expires)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                f"""UPDATE connector_jobs {self._RECOVER_SET}
                   WHERE status='running' AND lease_expires_at < ?""",
                (now_iso, now_iso, now_iso),
            )
            row = db.execute(
                f"""SELECT * FROM connector_jobs
                   WHERE {self._CLAIMABLE}
                   ORDER BY available_at, created_at LIMIT 1""",
                (now_iso,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                """UPDATE connector_jobs SET status='running', attempts=attempts+1,
                       lease_owner=?, lease_expires_at=?, updated_at=? WHERE job_id=?""",
                (worker_id, lease_expires, now_iso, row["job_id"]),
            )
            claimed = db.execute(
                "SELECT * FROM connector_jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._job(claimed)

    def _claim_postgres(
        self, db: Any, worker_id: str, now_iso: str, lease_expires: str
    ) -> ConnectorJob | None:
        # BEGIN IMMEDIATE is a no-op on Postgres, so concurrency comes from row
        # locks: SKIP LOCKED makes each worker pass over rows another worker is
        # recovering/claiming, and under READ COMMITTED a row whose status
        # changed after our snapshot is re-checked against the WHERE before it
        # is locked. Each job is therefore leased by exactly one worker.
        db.execute(
            f"""UPDATE connector_jobs {self._RECOVER_SET}
               WHERE job_id IN (
                   SELECT job_id FROM connector_jobs
                   WHERE status='running' AND lease_expires_at < ?
                   FOR UPDATE SKIP LOCKED)""",
            (now_iso, now_iso, now_iso),
        )
        claimed = db.execute(
            f"""UPDATE connector_jobs SET status='running', attempts=attempts+1,
                   lease_owner=?, lease_expires_at=?, updated_at=?
               WHERE job_id = (
                   SELECT job_id FROM connector_jobs
                   WHERE {self._CLAIMABLE}
                   ORDER BY available_at, created_at LIMIT 1
                   FOR UPDATE SKIP LOCKED)
               RETURNING *""",
            (worker_id, lease_expires, now_iso, now_iso),
        ).fetchone()
        return self._job(claimed) if claimed else None

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 3600) -> bool:
        now = self._now()
        with self._connect() as db:
            cursor = db.execute(
                """UPDATE connector_jobs SET lease_expires_at=?, updated_at=?
                   WHERE job_id=? AND status='running' AND lease_owner=?""",
                ((now + timedelta(seconds=lease_seconds)).isoformat(), now.isoformat(),
                 job_id, worker_id),
            )
        return cursor.rowcount == 1

    def complete(self, job_id: str, worker_id: str) -> None:
        now = self._now().isoformat()
        with self._connect() as db:
            db.execute(
                """UPDATE connector_jobs SET status='completed', lease_owner=NULL,
                       lease_expires_at=NULL, updated_at=?
                   WHERE job_id=? AND lease_owner=?""",
                (now, job_id, worker_id),
            )

    def fail(
        self, job: ConnectorJob, worker_id: str, error: str, *,
        retry_after_seconds: float | None = None,
    ) -> str:
        terminal = job.attempts >= job.max_attempts
        status = "dead" if terminal else "retry"
        delay = min(300, 2 ** max(0, job.attempts - 1))
        if retry_after_seconds is not None:
            delay = max(delay, min(3600, max(1, retry_after_seconds)))
        now = self._now()
        available = (now + timedelta(seconds=delay)).isoformat()
        with self._connect() as db:
            db.execute(
                """UPDATE connector_jobs SET status=?, available_at=?, lease_owner=NULL,
                       lease_expires_at=NULL, updated_at=?, last_error=?
                   WHERE job_id=? AND lease_owner=?""",
                (status, available, now.isoformat(), error[:2_000], job.job_id, worker_id),
            )
        return status

    def get(self, job_id: str) -> ConnectorJob | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM connector_jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._job(row) if row else None
