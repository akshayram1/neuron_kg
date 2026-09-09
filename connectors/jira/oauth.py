from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

JIRA_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
JIRA_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
JIRA_SCOPES = "offline_access read:jira-work read:jira-user"


class JiraConfigurationError(RuntimeError):
    pass


class JiraOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraOAuthSettings:
    client_id: str
    client_secret: str
    redirect_uri: str
    encryption_key: str

    @classmethod
    def from_env(cls) -> "JiraOAuthSettings":
        values = {
            "client_id": os.getenv("JIRA_OAUTH_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("JIRA_OAUTH_CLIENT_SECRET", "").strip(),
            "redirect_uri": os.getenv("JIRA_OAUTH_REDIRECT_URI", "").strip(),
            "encryption_key": (
                os.getenv("JIRA_TOKEN_ENCRYPTION_KEY", "").strip()
                or os.getenv("CONNECTOR_TOKEN_ENCRYPTION_KEY", "").strip()
            ),
        }
        names = {
            "client_id": "JIRA_OAUTH_CLIENT_ID",
            "client_secret": "JIRA_OAUTH_CLIENT_SECRET",
            "redirect_uri": "JIRA_OAUTH_REDIRECT_URI",
            "encryption_key": "JIRA_TOKEN_ENCRYPTION_KEY or CONNECTOR_TOKEN_ENCRYPTION_KEY",
        }
        missing = [names[key] for key, value in values.items() if not value]
        if missing:
            raise JiraConfigurationError("Missing Jira configuration: " + ", ".join(missing))
        return cls(**values)

    def authorization_url(self, state: str) -> str:
        return JIRA_AUTHORIZE_URL + "?" + urlencode(
            {
                "audience": "api.atlassian.com",
                "client_id": self.client_id,
                "scope": JIRA_SCOPES,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "response_type": "code",
                "prompt": "consent",
            }
        )

    async def exchange_code(self, code: str) -> dict:
        return await self._token(
            {
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token(
            {
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            }
        )

    async def _token(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(JIRA_TOKEN_URL, json=payload)
            except httpx.TransportError as exc:
                raise JiraOAuthError("Could not reach Atlassian OAuth") from exc
        if response.status_code >= 400:
            raise JiraOAuthError(f"Atlassian OAuth failed ({response.status_code})")
        value = response.json()
        if not isinstance(value, dict) or not value.get("access_token"):
            raise JiraOAuthError("Atlassian returned an incomplete OAuth token")
        return value
