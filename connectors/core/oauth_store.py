"""Encrypted browser-session state shared by OAuth connector implementations."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class OAuthStoreError(RuntimeError):
    pass


class OAuthConnectorStore:
    def __init__(self, path: str | Path, provider: str, encryption_key: str):
        self.path = Path(path)
        self.provider = provider
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fernet = Fernet(encryption_key.encode())
        except (TypeError, ValueError) as exc:
            raise OAuthStoreError(f"Invalid {provider} token encryption key") from exc
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
                CREATE TABLE IF NOT EXISTS oauth_states (
                    provider TEXT NOT NULL,
                    state_hash TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY(provider, state_hash)
                );
                CREATE TABLE IF NOT EXISTS oauth_connections (
                    provider TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    encrypted_token BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(provider, connection_id)
                );
                CREATE TABLE IF NOT EXISTS oauth_session_connections (
                    provider TEXT NOT NULL,
                    session_hash TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    PRIMARY KEY(provider, session_hash, connection_id)
                );
                CREATE TABLE IF NOT EXISTS oauth_sources (
                    provider TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    last_sync_at TEXT,
                    last_sync_error TEXT,
                    PRIMARY KEY(provider, connection_id, source_id)
                );
                CREATE TABLE IF NOT EXISTS oauth_sync_runs (
                    provider TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT,
                    PRIMARY KEY(provider, run_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS oauth_one_active_sync_per_source
                    ON oauth_sync_runs(provider, connection_id, source_id)
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

    def create_state(self, session_id: str, ttl_seconds: int = 600) -> str:
        state = secrets.token_urlsafe(32)
        expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as db:
            db.execute(
                "DELETE FROM oauth_states WHERE provider=? AND expires_at < ?",
                (self.provider, self._now()),
            )
            db.execute(
                "INSERT INTO oauth_states VALUES (?, ?, ?, ?)",
                (self.provider, self._hash(state), self.session_hash(session_id), expires),
            )
        return state

    def consume_state(self, state: str) -> str | None:
        state_hash = self._hash(state)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_hash, expires_at FROM oauth_states WHERE provider=? AND state_hash=?",
                (self.provider, state_hash),
            ).fetchone()
            db.execute(
                "DELETE FROM oauth_states WHERE provider=? AND state_hash=?",
                (self.provider, state_hash),
            )
        return str(row["session_hash"]) if row and row["expires_at"] >= self._now() else None

    def _encrypt(self, token: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(token, separators=(",", ":")).encode())

    def _decrypt(self, payload: bytes) -> dict[str, Any]:
        try:
            value = json.loads(self._fernet.decrypt(payload))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise OAuthStoreError(f"Could not decrypt stored {self.provider} credentials") from exc
        if not isinstance(value, dict):
            raise OAuthStoreError(f"Stored {self.provider} credentials are invalid")
        return value

    def save_connection(
        self,
        *,
        connection_id: str,
        account_id: str,
        account_name: str,
        token: dict[str, Any],
    ) -> dict[str, Any]:
        if not connection_id or not token.get("access_token"):
            raise OAuthStoreError(f"Incomplete {self.provider} connection")
        now = self._now()
        with self._connect() as db:
            existing = db.execute(
                "SELECT created_at FROM oauth_connections WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else now
            db.execute(
                """INSERT INTO oauth_connections VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, connection_id) DO UPDATE SET
                       account_id=excluded.account_id,
                       account_name=excluded.account_name,
                       encrypted_token=excluded.encrypted_token,
                       updated_at=excluded.updated_at""",
                (
                    self.provider, connection_id, account_id, account_name,
                    self._encrypt(token), created_at, now,
                ),
            )
        return self.get_connection(connection_id)

    def get_connection(self, connection_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM oauth_connections WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            ).fetchone()
        if not row:
            raise KeyError(f"No {self.provider} connection {connection_id}")
        value = dict(row)
        value["token"] = self._decrypt(value.pop("encrypted_token"))
        return value

    def update_token(self, connection_id: str, token: dict[str, Any]) -> dict[str, Any]:
        current = self.get_connection(connection_id)
        merged = {**current["token"], **token}
        return self.save_connection(
            connection_id=connection_id,
            account_id=current["account_id"],
            account_name=current["account_name"],
            token=merged,
        )

    def link_session(self, session_hash: str, connection_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO oauth_session_connections VALUES (?, ?, ?, ?)
                   ON CONFLICT(provider, session_hash, connection_id) DO UPDATE SET
                       connected_at=excluded.connected_at""",
                (self.provider, session_hash, connection_id, self._now()),
            )

    def session_has_connection(self, session_hash: str, connection_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM oauth_session_connections
                   WHERE provider=? AND session_hash=? AND connection_id=?""",
                (self.provider, session_hash, connection_id),
            ).fetchone()
        return row is not None

    def list_connections(self, session_hash: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT c.connection_id, c.account_id, c.account_name, c.created_at, c.updated_at
                   FROM oauth_connections c JOIN oauth_session_connections s
                     ON s.provider=c.provider AND s.connection_id=c.connection_id
                   WHERE c.provider=? AND s.session_hash=? ORDER BY c.updated_at DESC""",
                (self.provider, session_hash),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_source(
        self,
        connection_id: str,
        source_id: str,
        source_name: str,
        group_id: str,
        config: dict[str, Any],
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO oauth_sources(provider, connection_id, source_id, source_name, group_id, config_json)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, connection_id, source_id) DO UPDATE SET
                       source_name=excluded.source_name, group_id=excluded.group_id,
                       config_json=excluded.config_json""",
                (self.provider, connection_id, source_id, source_name, group_id, json.dumps(config)),
            )

    def list_sources(self, session_hash: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT src.* FROM oauth_sources src JOIN oauth_session_connections ses
                     ON ses.provider=src.provider AND ses.connection_id=src.connection_id
                   WHERE src.provider=? AND ses.session_hash=? ORDER BY src.source_name""",
                (self.provider, session_hash),
            ).fetchall()
        return [{**dict(row), "config": json.loads(row["config_json"])} for row in rows]

    def delete_connection(self, connection_id: str) -> list[str]:
        """Remove a connection and every source recorded under it.

        Returns the group_ids that were synced under this connection, so the
        caller can purge FalkorDB and the bridge store for them too. Does not
        touch the shared source-record ledger (connectors.core.ledger) --
        that is provider-scoped by record_key, not by this store's schema.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT group_id FROM oauth_sources WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            ).fetchall()
            group_ids = [str(row["group_id"]) for row in rows]
            db.execute(
                "DELETE FROM oauth_sources WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            )
            db.execute(
                "DELETE FROM oauth_sync_runs WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            )
            db.execute(
                "DELETE FROM oauth_session_connections WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            )
            db.execute(
                "DELETE FROM oauth_connections WHERE provider=? AND connection_id=?",
                (self.provider, connection_id),
            )
        return group_ids

    def fail_orphaned_runs(
        self,
        connection_id: str,
        source_id: str,
        live_run_ids: set[str],
        error: str = "Previous sync was interrupted",
    ) -> int:
        with self._connect() as db:
            rows = db.execute(
                """SELECT run_id FROM oauth_sync_runs
                   WHERE provider=? AND connection_id=? AND source_id=?
                     AND status IN ('queued', 'running')""",
                (self.provider, connection_id, source_id),
            ).fetchall()
            stale = [str(row["run_id"]) for row in rows if str(row["run_id"]) not in live_run_ids]
            now = self._now()
            for run_id in stale:
                db.execute(
                    """UPDATE oauth_sync_runs SET status='failed', finished_at=?, error=?
                       WHERE provider=? AND run_id=?""",
                    (now, error[:1000], self.provider, run_id),
                )
            return len(stale)

    def create_run(self, run_id: str, connection_id: str, source_id: str) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO oauth_sync_runs(
                           provider, run_id, connection_id, source_id, status, started_at
                       ) VALUES (?, ?, ?, ?, 'queued', ?)""",
                    (self.provider, run_id, connection_id, source_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"A {self.provider} sync is already active for this source") from exc

    def set_run(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        finished = self._now() if status in {"completed", "failed"} else None
        with self._connect() as db:
            db.execute(
                """UPDATE oauth_sync_runs SET status=?, finished_at=?, result_json=?, error=?
                   WHERE provider=? AND run_id=?""",
                (status, finished, json.dumps(result) if result else None, error, self.provider, run_id),
            )
            if finished:
                run = db.execute(
                    "SELECT connection_id, source_id FROM oauth_sync_runs WHERE provider=? AND run_id=?",
                    (self.provider, run_id),
                ).fetchone()
                if run:
                    db.execute(
                        """UPDATE oauth_sources SET last_sync_at=?, last_sync_error=?
                           WHERE provider=? AND connection_id=? AND source_id=?""",
                        (finished, error, self.provider, run["connection_id"], run["source_id"]),
                    )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM oauth_sync_runs WHERE provider=? AND run_id=?",
                (self.provider, run_id),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
        return value

    def list_runs(self, session_hash: str, limit: int = 25) -> list[dict[str, Any]]:
        """Recent runs visible to this browser session, newest first."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT run.* FROM oauth_sync_runs run
                   JOIN oauth_session_connections session
                     ON session.provider=run.provider AND session.connection_id=run.connection_id
                   WHERE run.provider=? AND session.session_hash=?
                   ORDER BY run.started_at DESC LIMIT ?""",
                (self.provider, session_hash, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
            output.append(value)
        return output
