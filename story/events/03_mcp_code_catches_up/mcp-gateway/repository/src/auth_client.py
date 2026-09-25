"""Authentication client used by MCP Gateway."""

AUTH_SERVICE = "https://auth.internal"
AUTH_API_VERSION = "v2"
AUTH_ENDPOINT = f"/{AUTH_API_VERSION}/auth"


def open_authenticated_session(client_id: str, credential: str) -> dict:
    """Call POST /v2/auth before opening an MCP tool session."""
    request = {
        "method": "POST",
        "url": AUTH_SERVICE + AUTH_ENDPOINT,
        "json": {"client_id": client_id, "credential": credential},
        "retries": 2,
    }
    return request
