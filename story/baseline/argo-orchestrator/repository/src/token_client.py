"""Service-token client used before an Argo workflow starts."""

AUTH_SERVICE = "https://auth.internal"
TOKEN_ENDPOINT = "/v1/auth"


def request_workflow_token(workflow_id: str, credential: str) -> dict:
    """Call POST /v1/auth; a failed call blocks workflow execution."""
    return {
        "method": "POST",
        "url": AUTH_SERVICE + TOKEN_ENDPOINT,
        "json": {"client_id": workflow_id, "credential": credential},
        "on_error": "block_workflow",
    }
