"""Bitbucket Cloud OAuth2 (classic per-user grant, not an App installation).

Bitbucket fixes the granted scopes on the OAuth consumer itself (set when the
consumer is created in Bitbucket workspace settings) — the authorize URL takes
no `scope` parameter, unlike Jira/GitHub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://bitbucket.org/site/oauth2/authorize"
TOKEN_URL = "https://bitbucket.org/site/oauth2/access_token"


class BitbucketConfigurationError(RuntimeError):
    pass


class BitbucketOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class BitbucketOAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str
    max_file_bytes: int = 1_000_000
    max_commits_per_sync: int = 100

    @classmethod
    def from_env(cls) -> "BitbucketOAuthSettings":
        values = {
            "client_id": os.getenv("BITBUCKET_OAUTH_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("BITBUCKET_OAUTH_CLIENT_SECRET", "").strip(),
            "redirect_uri": os.getenv("BITBUCKET_OAUTH_REDIRECT_URI", "").strip(),
            "encryption_key": (
                os.getenv("BITBUCKET_TOKEN_ENCRYPTION_KEY", "").strip()
                or os.getenv("CONNECTOR_TOKEN_ENCRYPTION_KEY", "").strip()
            ),
        }
        names = {
            "client_id": "BITBUCKET_OAUTH_CLIENT_ID",
            "client_secret": "BITBUCKET_OAUTH_CLIENT_SECRET",
            "redirect_uri": "BITBUCKET_OAUTH_REDIRECT_URI",
            "encryption_key": "BITBUCKET_TOKEN_ENCRYPTION_KEY or CONNECTOR_TOKEN_ENCRYPTION_KEY",
        }
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            raise BitbucketConfigurationError("Missing Bitbucket configuration: " + ", ".join(missing))
        try:
            max_file_bytes = int(os.getenv("BITBUCKET_MAX_FILE_BYTES", "1000000"))
            max_commits = max(1, min(1_000, int(os.getenv("BITBUCKET_MAX_COMMITS_PER_SYNC", "100"))))
        except ValueError as exc:
            raise BitbucketConfigurationError(
                "BITBUCKET_MAX_FILE_BYTES and BITBUCKET_MAX_COMMITS_PER_SYNC must be integers"
            ) from exc
        return cls(**values, max_file_bytes=max_file_bytes, max_commits_per_sync=max_commits)

    def authorization_url(self, state: str) -> str:
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code", "state": state,
        })

    async def exchange_code(self, code: str) -> dict:
        return await self._token({"grant_type": "authorization_code", "code": code})

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _token(self, data: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(TOKEN_URL, data=data, auth=(self.client_id, self.client_secret))
            except httpx.TransportError as exc:
                raise BitbucketOAuthError("Could not reach Bitbucket OAuth") from exc
        if response.status_code >= 400:
            raise BitbucketOAuthError(f"Bitbucket OAuth failed ({response.status_code})")
        value = response.json()
        if not isinstance(value, dict) or not value.get("access_token"):
            raise BitbucketOAuthError("Bitbucket returned an incomplete OAuth token")
        return value
