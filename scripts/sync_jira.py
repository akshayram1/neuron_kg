"""CLI: run a Jira Pass A sync — fetch a project and write structural facts.
LLM extraction is Notion-only.

    uv run python -m scripts.sync_jira --list-connections
    uv run python -m scripts.sync_jira --connection <id> --project-key HERA
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
from typing import Awaitable, Callable, TypeVar

from util import paths as _paths  # noqa: F401 — loads .env from repo root
from util.logging import configure_logging

from connectors.core.ledger import ConnectorLedger
from connectors.core.oauth_store import OAuthConnectorStore
from connectors.jira.api import JiraApiClient, JiraUnauthorized
from connectors.jira.oauth import JiraOAuthSettings
from graph import jira_pipeline as jp
from graph.falkor_client import get_graph
from graph.schema import bootstrap_schema

LEDGER_PATH = "connector_ledger.sqlite3"
OAUTH_STORE_PATH = "oauth_connectors.sqlite3"

T = TypeVar("T")


async def _with_refresh(
    store: OAuthConnectorStore,
    settings: JiraOAuthSettings,
    connection_id: str,
    operation: Callable[[JiraApiClient], Awaitable[T]],
) -> T:
    connection = store.get_connection(connection_id)
    try:
        async with JiraApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)
    except JiraUnauthorized:
        refresh_token = connection["token"].get("refresh_token")
        if not refresh_token:
            raise
        token = await settings.refresh(str(refresh_token))
        connection = store.update_token(connection_id, token)
        async with JiraApiClient(connection["token"]["access_token"]) as client:
            return await operation(client)


def list_connections() -> None:
    try:
        db = sqlite3.connect(OAUTH_STORE_PATH)
        rows = db.execute(
            "SELECT connection_id, account_name, created_at FROM oauth_connections WHERE provider='jira'"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        print(f"No Jira connections in {OAUTH_STORE_PATH}. Authorize one first.")
        return
    for connection_id, account_name, created_at in rows:
        print(f"{connection_id}  {account_name}  (connected {created_at})")


async def sync_project(connection_id: str, project_key: str) -> None:
    settings = JiraOAuthSettings.from_env()
    store = OAuthConnectorStore(OAUTH_STORE_PATH, "jira", settings.encryption_key)
    ledger = ConnectorLedger(LEDGER_PATH)
    graph = get_graph()
    bootstrap_schema(graph)

    sites = await _with_refresh(store, settings, connection_id, lambda c: c.sites())
    if not sites:
        raise SystemExit("No accessible Jira sites for this connection")
    site = sites[0]

    projects = await _with_refresh(store, settings, connection_id, lambda c: c.projects(site.cloud_id))
    project = next((p for p in projects if p.key == project_key), None)
    if project is None:
        available = ", ".join(p.key for p in projects) or "(none visible)"
        raise SystemExit(f"Project {project_key!r} not found. Available: {available}")

    issues = await _with_refresh(store, settings, connection_id, lambda c: c.issues(site.cloud_id, project.key))
    print(f"Fetched {len(issues)} issues from {project.key} ({project.name})")

    action = jp.write_project(graph, ledger, project, site, connection_id)
    print(f"project {project.key}: {action}")

    counts: dict[str, int] = {}
    for issue in issues:
        action = jp.write_issue(graph, ledger, issue, project, site, connection_id)
        counts[str(action)] = counts.get(str(action), 0) + 1
        print(f"  {issue.key}: {action}")
    print(f"issues by action: {counts}")

    node_counts = graph.query(
        "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS n ORDER BY label"
    ).result_set
    print("Graph node counts:", {row[0]: row[1] for row in node_counts})


def main() -> None:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-connections", action="store_true")
    ap.add_argument("--connection", help="Jira OAuth connection_id")
    ap.add_argument("--project-key")
    args = ap.parse_args()

    if args.list_connections:
        list_connections()
        return
    if not args.connection or not args.project_key:
        raise SystemExit("--connection and --project-key are required (or pass --list-connections)")

    asyncio.run(sync_project(args.connection, args.project_key))


if __name__ == "__main__":
    main()
