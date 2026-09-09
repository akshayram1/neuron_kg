"""Public Notion OAuth and durable connector state.

Secrets are encrypted before they are written to SQLite.  The database also
stores one-time OAuth state values and the idempotency ledger used by the
Notion -> Graphiti pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from util.paths import DATA_DIR


NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
DEFAULT_STATE_TTL_SECONDS = 600


class NotionOAuthError(RuntimeError):
    """A safe-to-display OAuth failure (never includes token material)."""


class NotionConfigurationError(RuntimeError):
    """Required Notion connector configuration is missing or invalid."""


@dataclass(frozen=True)
class NotionOAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str
    success_redirect: str | None = None

    @classmethod
    def from_env(cls) -> "NotionOAuthSettings":
        values = {
            "client_id": os.getenv("NOTION_OAUTH_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("NOTION_OAUTH_CLIENT_SECRET", "").strip(),
            "redirect_uri": os.getenv("NOTION_OAUTH_REDIRECT_URI", "").strip(),
            "encryption_key": os.getenv("NOTION_TOKEN_ENCRYPTION_KEY", "").strip(),
        }
        required = {"client_id", "client_secret", "redirect_uri", "encryption_key"}
        missing = [name for name in required if not values[name]]
        if missing:
            # Spell out the exact names because OAuth uses slightly different
            # prefixes than the dataclass fields.
            exact = {
                "client_id": "NOTION_OAUTH_CLIENT_ID",
                "client_secret": "NOTION_OAUTH_CLIENT_SECRET",
                "redirect_uri": "NOTION_OAUTH_REDIRECT_URI",
                "encryption_key": "NOTION_TOKEN_ENCRYPTION_KEY",
            }
            env_names = ", ".join(exact[name] for name in missing)
            raise NotionConfigurationError(f"Missing connector configuration: {env_names}")

        placeholder_markers = ("<your", "your_", "notion_se_mila", "client-id", "client-secret")
        placeholders = [
            name
            for name in ("client_id", "client_secret")
            if any(marker in values[name].lower() for marker in placeholder_markers)
        ]
        if placeholders:
            exact = {
                "client_id": "NOTION_OAUTH_CLIENT_ID",
                "client_secret": "NOTION_OAUTH_CLIENT_SECRET",
            }
            env_names = ", ".join(exact[name] for name in placeholders)
            raise NotionConfigurationError(
                f"Replace placeholder values with OAuth credentials from Notion: {env_names}"
            )

        try:
            Fernet(values["encryption_key"].encode())
        except (TypeError, ValueError) as exc:
            raise NotionConfigurationError(
                "NOTION_TOKEN_ENCRYPTION_KEY must be a Fernet key; generate one with "
                "`python -m connectors.notion.oauth --generate-key`."
            ) from exc

        return cls(
            **values,
            success_redirect=os.getenv("NOTION_OAUTH_SUCCESS_REDIRECT", "").strip() or None,
        )


@dataclass(frozen=True)
class NotionConnection:
    workspace_id: str
    bot_id: str
    workspace_name: str
    created_at: str
    updated_at: str
    token: dict[str, Any]

    @property
    def access_token(self) -> str:
        return str(self.token["access_token"])

    @property
    def refresh_token(self) -> str | None:
        value = self.token.get("refresh_token")
        return str(value) if value else None


def notion_state_db_path() -> Path:
    configured = os.getenv("NOTION_STATE_DB", "").strip()
    if not configured:
        return DATA_DIR / "notion_connector.sqlite3"
    path = Path(configured)
    return path if path.is_absolute() else DATA_DIR / path


def graph_group_for_workspace(workspace_id: str) -> str:
    safe_id = "".join(ch for ch in workspace_id if ch.isascii() and (ch.isalnum() or ch in "_-"))
    if not safe_id:
        raise ValueError("Notion workspace id cannot produce a safe Graphiti group id")
    return f"notion_{safe_id}"


class NotionStore:
    """Small SQLite store safe to reuse from CLI and FastAPI processes."""

    def __init__(self, path: Path, encryption_key: str):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(encryption_key.encode())
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    session_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notion_connections (
                    workspace_id TEXT PRIMARY KEY,
                    bot_id TEXT NOT NULL,
                    workspace_name TEXT NOT NULL,
                    encrypted_token BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_sync_at TEXT,
                    last_sync_error TEXT
                );
                CREATE TABLE IF NOT EXISTS notion_episodes (
                    workspace_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    episode_uid TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    graph_episode_uuid TEXT NOT NULL,
                    ingested_at TEXT NOT NULL,
                    PRIMARY KEY (workspace_id, group_id, episode_uid)
                );
                CREATE TABLE IF NOT EXISTS notion_pages (
                    workspace_id TEXT NOT NULL,
                    page_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    last_edited_time TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (workspace_id, page_id)
                );
                CREATE TABLE IF NOT EXISTS notion_sync_runs (
                    run_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_json TEXT,
                    error TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS notion_one_active_sync_per_workspace
                    ON notion_sync_runs(workspace_id)
                    WHERE status IN ('queued', 'running');
                CREATE TABLE IF NOT EXISTS notion_session_connections (
                    session_hash TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    connected_at TEXT NOT NULL,
                    PRIMARY KEY (session_hash, workspace_id),
                    FOREIGN KEY (workspace_id) REFERENCES notion_connections(workspace_id)
                        ON DELETE CASCADE
                );
                """
            )
            # Migration for connector databases created before browser sessions
            # were introduced.
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(oauth_states)").fetchall()
            }
            if "session_hash" not in columns:
                db.execute(
                    "ALTER TABLE oauth_states ADD COLUMN session_hash TEXT NOT NULL DEFAULT ''"
                )
        try:
            self.path.chmod(0o600)
        except OSError:
            # Some container volume drivers do not support chmod. Tokens remain
            # encrypted even when the volume controls permissions externally.
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _state_hash(state: str) -> str:
        return hashlib.sha256(state.encode()).hexdigest()

    @staticmethod
    def session_hash(session_id: str) -> str:
        return hashlib.sha256(session_id.encode()).hexdigest()

    def create_oauth_state(
        self,
        session_id: str,
        ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
    ) -> str:
        state = secrets.token_urlsafe(32)
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as db:
            db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (self._now(),))
            db.execute(
                "INSERT INTO oauth_states(state_hash, session_hash, expires_at) VALUES (?, ?, ?)",
                (self._state_hash(state), self.session_hash(session_id), expires_at),
            )
        return state

    def consume_oauth_state(self, state: str) -> str | None:
        state_hash = self._state_hash(state)
        now = self._now()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT session_hash, expires_at FROM oauth_states WHERE state_hash = ?",
                (state_hash,),
            ).fetchone()
            db.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
        return row["session_hash"] if row and row["expires_at"] >= now else None

    def _encrypt(self, payload: dict[str, Any]) -> bytes:
        compact = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return self._fernet.encrypt(compact)

    def _decrypt(self, encrypted: bytes) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(encrypted))
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise NotionConfigurationError(
                "Cannot decrypt stored Notion credentials. Check NOTION_TOKEN_ENCRYPTION_KEY."
            ) from exc

    def save_connection(self, token_payload: dict[str, Any]) -> NotionConnection:
        required = ("access_token", "workspace_id", "bot_id")
        if any(not token_payload.get(key) for key in required):
            raise NotionOAuthError("Notion token response is missing workspace or token fields")
        workspace_id = str(token_payload["workspace_id"])
        now = self._now()
        with self._connect() as db:
            existing = db.execute(
                "SELECT created_at FROM notion_connections WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            db.execute(
                """
                INSERT INTO notion_connections(
                    workspace_id, bot_id, workspace_name, encrypted_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    bot_id=excluded.bot_id,
                    workspace_name=excluded.workspace_name,
                    encrypted_token=excluded.encrypted_token,
                    updated_at=excluded.updated_at,
                    last_sync_error=NULL
                """,
                (
                    workspace_id,
                    str(token_payload["bot_id"]),
                    str(token_payload.get("workspace_name") or "Notion workspace"),
                    self._encrypt(token_payload),
                    created_at,
                    now,
                ),
            )
        return self.get_connection(workspace_id)

    def update_tokens(self, workspace_id: str, token_payload: dict[str, Any]) -> NotionConnection:
        current = self.get_connection(workspace_id)
        merged = {**current.token, **token_payload, "workspace_id": workspace_id}
        # Rotation responses can omit static install metadata.
        merged.setdefault("bot_id", current.bot_id)
        merged.setdefault("workspace_name", current.workspace_name)
        return self.save_connection(merged)

    def get_connection(self, workspace_id: str) -> NotionConnection:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM notion_connections WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"No Notion connection for workspace {workspace_id}")
        return NotionConnection(
            workspace_id=row["workspace_id"],
            bot_id=row["bot_id"],
            workspace_name=row["workspace_name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            token=self._decrypt(row["encrypted_token"]),
        )

    def link_session_connection(self, session_hash: str, workspace_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT INTO notion_session_connections(session_hash, workspace_id, connected_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_hash, workspace_id) DO UPDATE SET
                       connected_at=excluded.connected_at""",
                (session_hash, workspace_id, self._now()),
            )

    def session_has_connection(self, session_hash: str, workspace_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM notion_session_connections
                   WHERE session_hash=? AND workspace_id=?""",
                (session_hash, workspace_id),
            ).fetchone()
        return row is not None

    def list_connections(self, session_hash: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as db:
            if session_hash is None:
                rows = db.execute(
                    """SELECT workspace_id, bot_id, workspace_name, created_at, updated_at,
                              last_sync_at, last_sync_error
                       FROM notion_connections ORDER BY workspace_name, workspace_id"""
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT c.workspace_id, c.bot_id, c.workspace_name, c.created_at,
                              c.updated_at, c.last_sync_at, c.last_sync_error
                       FROM notion_connections c
                       JOIN notion_session_connections s ON s.workspace_id=c.workspace_id
                       WHERE s.session_hash=?
                       ORDER BY c.workspace_name, c.workspace_id""",
                    (session_hash,),
                ).fetchall()
        return [
            {
                **dict(row),
                "group_id": graph_group_for_workspace(row["workspace_id"]),
            }
            for row in rows
        ]

    def delete_connection(self, workspace_id: str) -> list[str]:
        """Remove a workspace connection and its sync history.

        Returns the group_id it was synced under (one Notion workspace is
        always one group), so the caller can purge FalkorDB and the bridge
        store for it too.
        """
        group_id = graph_group_for_workspace(workspace_id)
        with self._connect() as db:
            db.execute("DELETE FROM notion_episodes WHERE workspace_id=?", (workspace_id,))
            db.execute("DELETE FROM notion_pages WHERE workspace_id=?", (workspace_id,))
            db.execute("DELETE FROM notion_sync_runs WHERE workspace_id=?", (workspace_id,))
            db.execute(
                "DELETE FROM notion_session_connections WHERE workspace_id=?", (workspace_id,)
            )
            db.execute("DELETE FROM notion_connections WHERE workspace_id=?", (workspace_id,))
        return [group_id]

    def episode_exists(self, workspace_id: str, group_id: str, episode_uid: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM notion_episodes
                   WHERE workspace_id=? AND group_id=? AND episode_uid=?""",
                (workspace_id, group_id, episode_uid),
            ).fetchone()
        return row is not None

    def record_episode(
        self,
        workspace_id: str,
        group_id: str,
        episode_uid: str,
        page_id: str,
        graph_episode_uuid: str,
    ) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO notion_episodes(
                       workspace_id, group_id, episode_uid, page_id,
                       graph_episode_uuid, ingested_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    workspace_id,
                    group_id,
                    episode_uid,
                    page_id,
                    graph_episode_uuid,
                    self._now(),
                ),
            )

    def reconcile_pages(self, workspace_id: str, pages: list[dict[str, str]]) -> int:
        now = self._now()
        seen_ids = {page["page_id"] for page in pages}
        with self._connect() as db:
            for page in pages:
                db.execute(
                    """INSERT INTO notion_pages(
                           workspace_id, page_id, title, url, last_edited_time,
                           last_seen_at, is_active
                       ) VALUES (?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(workspace_id, page_id) DO UPDATE SET
                           title=excluded.title,
                           url=excluded.url,
                           last_edited_time=excluded.last_edited_time,
                           last_seen_at=excluded.last_seen_at,
                           is_active=1""",
                    (
                        workspace_id,
                        page["page_id"],
                        page["title"],
                        page["url"],
                        page["last_edited_time"],
                        now,
                    ),
                )
            active = db.execute(
                "SELECT page_id FROM notion_pages WHERE workspace_id=? AND is_active=1",
                (workspace_id,),
            ).fetchall()
            missing = {row["page_id"] for row in active} - seen_ids
            for page_id in missing:
                db.execute(
                    "UPDATE notion_pages SET is_active=0 WHERE workspace_id=? AND page_id=?",
                    (workspace_id, page_id),
                )
        return len(missing)

    def finish_connection_sync(self, workspace_id: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE notion_connections
                   SET last_sync_at=?, last_sync_error=? WHERE workspace_id=?""",
                (self._now(), error, workspace_id),
            )

    def create_sync_run(self, run_id: str, workspace_id: str) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO notion_sync_runs(run_id, workspace_id, status, started_at)
                       VALUES (?, ?, 'queued', ?)""",
                    (run_id, workspace_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError("A sync is already queued or running for this workspace") from exc

    def fail_orphaned_sync_runs(
        self,
        workspace_id: str,
        live_run_ids: set[str],
        error: str = "Previous sync was interrupted",
    ) -> int:
        """Release durable active rows that have no worker in this process."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT run_id FROM notion_sync_runs
                   WHERE workspace_id=? AND status IN ('queued', 'running')""",
                (workspace_id,),
            ).fetchall()
            stale = [str(row["run_id"]) for row in rows if str(row["run_id"]) not in live_run_ids]
            now = self._now()
            for stale_run_id in stale:
                db.execute(
                    """UPDATE notion_sync_runs
                       SET status='failed', finished_at=?, error=? WHERE run_id=?""",
                    (now, error[:1_000], stale_run_id),
                )
            return len(stale)

    def set_sync_run(
        self,
        run_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        finished_at = self._now() if status in {"completed", "failed"} else None
        with self._connect() as db:
            db.execute(
                """UPDATE notion_sync_runs
                   SET status=?, finished_at=?, result_json=?, error=? WHERE run_id=?""",
                (status, finished_at, json.dumps(result) if result else None, error, run_id),
            )

    def get_sync_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM notion_sync_runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json")) if row["result_json"] else None
        return result

    def list_sync_runs(self, session_hash: str, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT run.* FROM notion_sync_runs run
                   JOIN notion_session_connections ses ON ses.workspace_id=run.workspace_id
                   WHERE ses.session_hash=? ORDER BY run.started_at DESC LIMIT ?""",
                (session_hash, limit),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            output.append(item)
        return output


class NotionOAuthClient:
    def __init__(self, settings: NotionOAuthSettings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client

    def authorization_url(self, state: str) -> str:
        query = urlencode(
            {
                "owner": "user",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "response_type": "code",
                "state": state,
            }
        )
        return f"{NOTION_AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    async def _token_request(self, payload: dict[str, str]) -> dict[str, Any]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            response = await client.post(
                NOTION_TOKEN_URL,
                json=payload,
                auth=(self.settings.client_id, self.settings.client_secret),
                headers={"Accept": "application/json"},
            )
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code >= 400:
            try:
                error = response.json().get("error") or response.json().get("message")
            except (ValueError, AttributeError):
                error = None
            raise NotionOAuthError(
                f"Notion OAuth request failed ({response.status_code})"
                + (f": {error}" if error else "")
            )
        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise NotionOAuthError("Notion OAuth returned an invalid token response")
        return data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Notion OAuth utilities")
    parser.add_argument("--generate-key", action="store_true")
    args = parser.parse_args()
    if args.generate_key:
        print(Fernet.generate_key().decode())
    else:
        parser.print_help()
