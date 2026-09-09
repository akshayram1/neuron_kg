"""GitHub App OAuth and installation-token authentication."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from util.paths import ROOT

API_VERSION = "2026-03-10"


class GitHubConfigurationError(RuntimeError):
    """The GitHub App configuration is incomplete or invalid."""


class GitHubAuthError(RuntimeError):
    """A safe-to-display GitHub authentication error."""


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


@dataclass(frozen=True)
class GitHubAppSettings:
    app_id: str
    client_id: str
    client_secret: str
    app_slug: str
    private_key_path: Path
    callback_url: str
    max_file_bytes: int
    max_commits_per_sync: int

    @classmethod
    def from_env(cls) -> "GitHubAppSettings":
        key_value = os.getenv("GITHUB_PRIVATE_KEY_PATH", "").strip()
        key_path = Path(key_value) if key_value else Path()
        if key_value and not key_path.is_absolute():
            key_path = ROOT / key_path
        raw = {
            "app_id": os.getenv("GITHUB_APP_ID", "").strip(),
            "client_id": os.getenv("GITHUB_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("GITHUB_CLIENT_SECRET", "").strip(),
            "app_slug": os.getenv("GITHUB_APP_SLUG", "").strip(),
            "callback_url": os.getenv("GITHUB_OAUTH_CALLBACK_URL", "").strip(),
        }
        labels = {
            "app_id": "GITHUB_APP_ID",
            "client_id": "GITHUB_CLIENT_ID",
            "client_secret": "GITHUB_CLIENT_SECRET",
            "app_slug": "GITHUB_APP_SLUG",
            "callback_url": "GITHUB_OAUTH_CALLBACK_URL",
        }
        missing = [labels[key] for key, value in raw.items() if not value]
        if not key_value:
            missing.append("GITHUB_PRIVATE_KEY_PATH")
        if missing:
            raise GitHubConfigurationError(
                "Missing GitHub connector configuration: " + ", ".join(missing)
            )
        if not key_path.is_file():
            raise GitHubConfigurationError(
                f"GitHub App private key was not found at {key_path}"
            )
        try:
            private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        except (OSError, ValueError, TypeError) as exc:
            raise GitHubConfigurationError("GITHUB_PRIVATE_KEY_PATH is not a valid PEM private key") from exc
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise GitHubConfigurationError("GitHub App private key must be an RSA private key")
        try:
            max_file_bytes = max(1, int(os.getenv("GITHUB_MAX_FILE_KB", "512"))) * 1024
            max_commits = max(1, min(1_000, int(os.getenv("GITHUB_MAX_COMMITS_PER_SYNC", "100"))))
        except ValueError as exc:
            raise GitHubConfigurationError(
                "GITHUB_MAX_FILE_KB and GITHUB_MAX_COMMITS_PER_SYNC must be integers"
            ) from exc
        return cls(
            **raw,
            private_key_path=key_path,
            max_file_bytes=max_file_bytes,
            max_commits_per_sync=max_commits,
        )

    def installation_url(self, state: str) -> str:
        return f"https://github.com/apps/{self.app_slug}/installations/new?{urlencode({'state': state})}"

    def create_jwt(self, now: int | None = None) -> str:
        issued = int(now if now is not None else time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = _b64url(json.dumps({
            "iat": issued - 60,
            "exp": issued + 9 * 60,
            "iss": self.client_id or self.app_id,
        }, separators=(",", ":")).encode())
        signing_input = f"{header}.{payload}".encode()
        private_key = serialization.load_pem_private_key(
            self.private_key_path.read_bytes(), password=None
        )
        signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64url(signature)}"


class GitHubAuthClient:
    def __init__(self, settings: GitHubAppSettings):
        self.settings = settings

    @staticmethod
    def _headers(token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "graphiti-context-connector",
        }

    async def exchange_user_code(self, code: str) -> str:
        payload = {
            "client_id": self.settings.client_id,
            "client_secret": self.settings.client_secret,
            "code": code,
            "redirect_uri": self.settings.callback_url,
        }
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    "https://github.com/login/oauth/access_token",
                    json=payload,
                    headers={"Accept": "application/json", "User-Agent": "graphiti-context-connector"},
                )
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"Could not exchange GitHub OAuth code: {exc}") from exc
        data = response.json() if response.content else {}
        token = str(data.get("access_token") or "")
        if response.is_error or not token:
            message = data.get("error_description") or data.get("error") or response.reason_phrase
            raise GitHubAuthError(f"GitHub OAuth failed: {message}")
        return token

    async def user_installations(self, user_token: str) -> list[dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.get(
                    "https://api.github.com/user/installations",
                    headers=self._headers(user_token),
                    params={"per_page": 100},
                )
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"Could not verify GitHub installation: {exc}") from exc
        if response.is_error:
            raise GitHubAuthError(f"Could not verify GitHub installation ({response.status_code})")
        return list(response.json().get("installations", []))

    async def user_installation(self, user_token: str, installation_id: int) -> dict[str, Any]:
        installations = await self.user_installations(user_token)
        match = next((item for item in installations if int(item.get("id", 0)) == installation_id), None)
        if not match:
            raise GitHubAuthError("This GitHub installation is not available to the signed-in user")
        return match

    async def installation_token(self, installation_id: int) -> str:
        url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                response = await client.post(
                    url,
                    headers=self._headers(self.settings.create_jwt()),
                )
        except httpx.HTTPError as exc:
            raise GitHubAuthError(f"Could not create GitHub installation token: {exc}") from exc
        data = response.json() if response.content else {}
        token = str(data.get("token") or "")
        if response.is_error or not token:
            message = data.get("message") or response.reason_phrase
            raise GitHubAuthError(f"GitHub installation token failed: {message}")
        return token
