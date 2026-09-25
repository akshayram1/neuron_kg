"""Auth Service HTTP contract and approved v1 sunset."""

from dataclasses import dataclass

V1_SUNSET_DATE = "2026-10-01"


@dataclass(frozen=True)
class Token:
    access_token: str
    version: str


def authenticate_v1(client_id: str, credential: str) -> Token:
    """Deprecated endpoint POST /v1/auth; remove on V1_SUNSET_DATE."""
    return Token(access_token=f"v1:{client_id}:{credential}", version="v1")


def authenticate_v2(client_id: str, credential: str) -> Token:
    """Replacement endpoint POST /v2/auth."""
    return Token(access_token=f"v2:{client_id}:{credential}", version="v2")


ROUTES = {
    "POST /v1/auth": authenticate_v1,
    "POST /v2/auth": authenticate_v2,
}
