"""Bitbucket request pacing and provider-directed retry behavior."""

from __future__ import annotations

import asyncio

import httpx

from connectors.bitbucket.api import (
    BitbucketApiClient, BitbucketRateLimited, BitbucketUnauthorized,
)


def test_429_is_retried_and_then_succeeds():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with BitbucketApiClient(
            "token", client=http, min_interval=0, max_retries=2,
        ) as client:
            return await client.json("https://api.bitbucket.org/2.0/example")

    assert asyncio.run(run()) == {"ok": True}
    assert calls == 2


def test_429_increases_the_gap_for_later_urls():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with BitbucketApiClient(
            "token", client=http, min_interval=0, max_retries=2,
        ) as client:
            # Avoid a real two-second wait while still exercising the adaptive
            # state transition produced by the provider response.
            client._max_inline_retry = 0
            try:
                await client.json("https://api.bitbucket.org/2.0/example")
            except BitbucketRateLimited:
                return client._adaptive_interval
        raise AssertionError("expected the long retry to be deferred")

    assert asyncio.run(run()) == 2.2


def test_all_endpoint_tasks_share_the_concurrency_gate():
    active = 0
    peak = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, json={"ok": True})

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with BitbucketApiClient(
            "token", client=http, max_concurrency=1, min_interval=0,
        ) as client:
            await asyncio.gather(*[
                client.json(f"https://api.bitbucket.org/2.0/example/{index}")
                for index in range(8)
            ])

    asyncio.run(run())
    assert peak == 1


def test_long_provider_wait_is_returned_to_the_durable_job_queue():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "1800", "RateLimit-Reason": "rolling-quota"},
        )

    async def run():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with BitbucketApiClient(
            "token", client=http, min_interval=0, max_retries=2,
        ) as client:
            await client.json("https://api.bitbucket.org/2.0/example")

    try:
        asyncio.run(run())
    except BitbucketRateLimited as exc:
        assert exc.retry_after_seconds == 1800
        assert "rolling-quota" in str(exc)
    else:
        raise AssertionError("long Retry-After must be deferred to the job queue")


def test_sync_refresh_wrapper_replays_once_and_persists_rotated_token(monkeypatch):
    # Import inside the test so the suite's SQLite isolation fixture is active
    # before the route module constructs its durable JOB_STORE.
    from demo_ui.backend import bitbucket_routes

    opened_tokens = []

    class Client:
        def __init__(self, token):
            opened_tokens.append(token)
            self.token = token

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Store:
        token = {"access_token": "expired", "refresh_token": "refresh-1"}

        def get_connection(self, _connection_id):
            return {"token": dict(self.token)}

        def update_token(self, _connection_id, token):
            self.token.update(token)
            return {"token": dict(self.token)}

    class Settings:
        async def refresh(self, refresh_token):
            assert refresh_token == "refresh-1"
            return {"access_token": "fresh"}

    attempts = []

    async def operation(client):
        attempts.append(client.token)
        if client.token == "expired":
            raise BitbucketUnauthorized("expired")
        return "fetched"

    monkeypatch.setattr(bitbucket_routes, "BitbucketApiClient", Client)
    store = Store()
    result = asyncio.run(bitbucket_routes._with_refresh(
        store, Settings(), "connection-1", operation,
    ))

    assert result == "fetched"
    assert attempts == ["expired", "fresh"]
    assert opened_tokens == ["expired", "fresh"]
    assert store.token == {"access_token": "fresh", "refresh_token": "refresh-1"}
