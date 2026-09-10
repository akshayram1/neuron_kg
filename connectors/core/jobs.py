"""Small durable leased job queue for connector syncs.

SQLite is sufficient at the current scale and keeps execution resumable across
API restarts without introducing Temporal/Prefect before operational need.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _job(row: sqlite3.Row) -> ConnectorJob:
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

    def claim(self, worker_id: str, *, lease_seconds: int = 3600) -> ConnectorJob | None:
        now = self._now()
        now_iso = now.isoformat()
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE connector_jobs
                   SET status='retry', lease_owner=NULL, lease_expires_at=NULL,
                       available_at=?, updated_at=?,
                       last_error=coalesce(last_error, 'Worker lease expired; recovered')
                   WHERE status='running' AND lease_expires_at < ?""",
                (now_iso, now_iso, now_iso),
            )
            row = db.execute(
                """SELECT * FROM connector_jobs
                   WHERE status IN ('queued', 'retry') AND available_at <= ?
                     AND attempts < max_attempts
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

    def fail(self, job: ConnectorJob, worker_id: str, error: str) -> str:
        terminal = job.attempts >= job.max_attempts
        status = "dead" if terminal else "retry"
        delay = min(300, 2 ** max(0, job.attempts - 1))
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

