"""Durable control state for GitHub App installations and Graphiti ingestion."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from util.paths import DATA_DIR

STATE_TTL_SECONDS = 600


def github_state_db_path() -> Path:
    configured = os.getenv("GITHUB_STATE_DB", "").strip()
    if not configured:
        return DATA_DIR / "github_connector.sqlite3"
    path = Path(configured)
    return path if path.is_absolute() else DATA_DIR / path


def graph_group_for_repository(installation_id: int, repository_id: int) -> str:
    return f"github_{installation_id}_{repository_id}"


class GitHubStore:
    """OAuth state, installation metadata, sync runs and idempotency ledger."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS github_oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_installations (
                    installation_id INTEGER PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    account_login TEXT NOT NULL,
                    account_type TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS github_session_installations (
                    session_hash TEXT NOT NULL,
                    installation_id INTEGER NOT NULL,
                    connected_at TEXT NOT NULL,
                    PRIMARY KEY(session_hash, installation_id),
                    FOREIGN KEY(installation_id) REFERENCES github_installations(installation_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS github_sources (
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    repository_full_name TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    file_types_json TEXT NOT NULL,
                    include_commit_messages INTEGER NOT NULL,
                    last_sync_at TEXT,
                    last_sync_error TEXT,
                    PRIMARY KEY(installation_id, repository_id)
                );
                CREATE TABLE IF NOT EXISTS github_episodes (
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    episode_uid TEXT NOT NULL,
                    graph_episode_uuid TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY(installation_id, repository_id, episode_uid)
                );
                CREATE INDEX IF NOT EXISTS github_episode_source_index
                    ON github_episodes(installation_id, repository_id, source_kind, source_id, ingested_at);
                CREATE TABLE IF NOT EXISTS github_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    installation_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS github_one_active_sync_per_repo
                    ON github_sync_runs(installation_id, repository_id)
                    WHERE status IN ('queued', 'running');
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def session_hash(cls, session_id: str) -> str:
        return cls._hash(session_id)

    def create_oauth_state(self, session_id: str) -> str:
        state = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS)).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM github_oauth_states WHERE expires_at < ?", (self._now(),))
            db.execute(
                "INSERT INTO github_oauth_states VALUES (?, ?, ?)",
                (self._hash(state), self.session_hash(session_id), expires),
            )
        return state

    def consume_oauth_state(self, state: str) -> str | None:
        key = self._hash(state)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_hash, expires_at FROM github_oauth_states WHERE state_hash=?",
                (key,),
            ).fetchone()
            db.execute("DELETE FROM github_oauth_states WHERE state_hash=?", (key,))
        return str(row["session_hash"]) if row and row["expires_at"] >= self._now() else None

    def save_installation(self, installation: dict[str, Any]) -> int:
        installation_id = int(installation.get("id") or 0)
        account = installation.get("account") or {}
        account_id = int(account.get("id") or 0)
        account_login = str(account.get("login") or account.get("name") or "GitHub account")
        if not installation_id or not account_id:
            raise ValueError("GitHub returned an incomplete installation")
        now = self._now()
        with self._connect() as db:
            existing = db.execute(
                "SELECT created_at FROM github_installations WHERE installation_id=?",
                (installation_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            db.execute(
                """INSERT INTO github_installations VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(installation_id) DO UPDATE SET
                       account_id=excluded.account_id,
                       account_login=excluded.account_login,
                       account_type=excluded.account_type,
                       target_type=excluded.target_type,
                       updated_at=excluded.updated_at""",
                (
                    installation_id, account_id, account_login,
                    str(account.get("type") or "User"),
                    str(installation.get("target_type") or "User"),
                    created_at, now,
                ),
            )
        return installation_id

    def link_session_installation(self, session_hash: str, installation_id: int) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO github_session_installations VALUES (?, ?, ?)
                   ON CONFLICT(session_hash, installation_id) DO UPDATE SET
                       connected_at=excluded.connected_at""",
                (session_hash, installation_id, self._now()),
            )

    def session_has_installation(self, session_hash: str, installation_id: int) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM github_session_installations
                   WHERE session_hash=? AND installation_id=?""",
                (session_hash, installation_id),
            ).fetchone()
        return row is not None

    def list_installations(self, session_hash: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT i.* FROM github_installations i
                   JOIN github_session_installations s
                     ON s.installation_id=i.installation_id
                   WHERE s.session_hash=? ORDER BY i.updated_at DESC""",
                (session_hash,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_source(
        self,
        installation_id: int,
        repository_id: int,
        repository_full_name: str,
        default_branch: str,
        file_types: list[str],
        include_commit_messages: bool,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO github_sources(
                       installation_id, repository_id, repository_full_name, default_branch,
                       file_types_json, include_commit_messages
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(installation_id, repository_id) DO UPDATE SET
                       repository_full_name=excluded.repository_full_name,
                       default_branch=excluded.default_branch,
                       file_types_json=excluded.file_types_json,
                       include_commit_messages=excluded.include_commit_messages""",
                (
                    installation_id, repository_id, repository_full_name, default_branch,
                    json.dumps(file_types), int(include_commit_messages),
                ),
            )

    def list_sources(self, session_hash: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT src.* FROM github_sources src
                   JOIN github_session_installations ses
                     ON ses.installation_id=src.installation_id
                   WHERE ses.session_hash=? ORDER BY src.repository_full_name""",
                (session_hash,),
            ).fetchall()
        return [
            {
                **dict(row),
                "file_types": json.loads(row["file_types_json"]),
                "include_commit_messages": bool(row["include_commit_messages"]),
                "group_id": graph_group_for_repository(row["installation_id"], row["repository_id"]),
            }
            for row in rows
        ]

    def delete_installation(self, installation_id: int) -> list[str]:
        """Remove an installation and every repository source under it.

        Returns the group_ids that were synced under this installation, so
        the caller can purge FalkorDB and the bridge store for them too.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT repository_id FROM github_sources WHERE installation_id=?",
                (installation_id,),
            ).fetchall()
            group_ids = [graph_group_for_repository(installation_id, row["repository_id"]) for row in rows]
            db.execute("DELETE FROM github_episodes WHERE installation_id=?", (installation_id,))
            db.execute("DELETE FROM github_sources WHERE installation_id=?", (installation_id,))
            db.execute("DELETE FROM github_sync_runs WHERE installation_id=?", (installation_id,))
            db.execute(
                "DELETE FROM github_session_installations WHERE installation_id=?", (installation_id,)
            )
            db.execute("DELETE FROM github_installations WHERE installation_id=?", (installation_id,))
        return group_ids

    def episode_exists(self, installation_id: int, repository_id: int, episode_uid: str) -> bool:
        with self._connect() as db:
            return db.execute(
                """SELECT 1 FROM github_episodes
                   WHERE installation_id=? AND repository_id=? AND episode_uid=?""",
                (installation_id, repository_id, episode_uid),
            ).fetchone() is not None

    def latest_source_episode(
        self, installation_id: int, repository_id: int, source_kind: str, source_id: str
    ) -> str | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT graph_episode_uuid FROM github_episodes
                   WHERE installation_id=? AND repository_id=?
                     AND source_kind=? AND source_id=?
                   ORDER BY ingested_at DESC, rowid DESC LIMIT 1""",
                (installation_id, repository_id, source_kind, source_id),
            ).fetchone()
        return str(row["graph_episode_uuid"]) if row else None

    def record_episode(
        self,
        installation_id: int,
        repository_id: int,
        source_kind: str,
        source_id: str,
        episode_uid: str,
        graph_episode_uuid: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO github_episodes VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    installation_id, repository_id, source_kind, source_id,
                    episode_uid, graph_episode_uuid, self._now(),
                ),
            )

    def finish_source_sync(
        self, installation_id: int, repository_id: int, error: str | None = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE github_sources SET last_sync_at=?, last_sync_error=?
                   WHERE installation_id=? AND repository_id=?""",
                (self._now(), error, installation_id, repository_id),
            )

    def create_sync_run(self, run_id: str, installation_id: int, repository_id: int) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO github_sync_runs(
                           run_id, installation_id, repository_id, status, started_at
                       ) VALUES (?, ?, ?, 'queued', ?)""",
                    (run_id, installation_id, repository_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("A sync is already queued or running for this repository") from exc

    def fail_orphaned_sync_runs(
        self, installation_id: int, repository_id: int, live_run_ids: set[str],
        error: str = "Previous sync was interrupted",
    ) -> int:
        with self._connect() as db:
            rows = db.execute(
                """SELECT run_id FROM github_sync_runs
                   WHERE installation_id=? AND repository_id=?
                     AND status IN ('queued', 'running')""",
                (installation_id, repository_id),
            ).fetchall()
            stale = [str(row["run_id"]) for row in rows if str(row["run_id"]) not in live_run_ids]
            for run_id in stale:
                db.execute(
                    """UPDATE github_sync_runs SET status='failed', finished_at=?, error=?
                       WHERE run_id=?""",
                    (self._now(), error[:1_000], run_id),
                )
        return len(stale)

    def set_sync_run(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        finished = self._now() if status in {"completed", "failed"} else None
        with self._connect() as db:
            db.execute(
                """UPDATE github_sync_runs SET status=?, finished_at=?, result_json=?, error=?
                   WHERE run_id=?""",
                (status, finished, json.dumps(result) if result else None, error, run_id),
            )

    def get_sync_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM github_sync_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        output = dict(row)
        output["result"] = json.loads(output.pop("result_json")) if output["result_json"] else None
        return output

    def list_sync_runs(self, session_hash: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT run.* FROM github_sync_runs run
                   JOIN github_session_installations ses
                     ON ses.installation_id=run.installation_id
                   WHERE ses.session_hash=? ORDER BY run.started_at DESC LIMIT ?""",
                (session_hash, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            output.append(item)
        return output
