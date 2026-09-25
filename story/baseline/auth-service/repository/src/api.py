"""Auth Service HTTP contract."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    access_token: str
    version: str


def authenticate_v1(client_id: str, credential: str) -> Token:
    """Production endpoint: POST /v1/auth."""
    return Token(access_token=f"v1:{client_id}:{credential}", version="v1")


def authenticate_v2(client_id: str, credential: str) -> Token:
    """Migration endpoint: POST /v2/auth."""
    return Token(access_token=f"v2:{client_id}:{credential}", version="v2")


ROUTES = {
    "POST /v1/auth": authenticate_v1,
    "POST /v2/auth": authenticate_v2,
}
