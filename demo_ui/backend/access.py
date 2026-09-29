"""Build an ACL scope from connector sessions owned by this browser."""

from __future__ import annotations

import os

from fastapi import Request

from connectors.bitbucket.oauth import BitbucketOAuthSettings
from connectors.core.oauth_store import OAuthConnectorStore
from connectors.github_app.store import GitHubStore, github_state_db_path
from connectors.jira.oauth import JiraOAuthSettings
from connectors.notion.oauth import NotionOAuthSettings, NotionStore, notion_state_db_path
from connectors.synthetic.loader import (
    LOCAL_DATA_BITBUCKET_CONNECTION, LOCAL_DATA_JIRA_CONNECTION, LOCAL_DATA_NOTION_CONNECTION,
)
from graph.retrieval.access import AccessScope
from util.paths import DATA_DIR


def access_scope_for_request(request: Request) -> AccessScope:
    # The operator-provided local export has no OAuth session to prove
    # ownership of, so every local UI session can see its ingested records,
    # just as an owning OAuth session can see its connector records.
    allowed: dict[str, list[str]] = {
        "jira": [LOCAL_DATA_JIRA_CONNECTION],
        "bitbucket": [LOCAL_DATA_BITBUCKET_CONNECTION],
        "notion": [LOCAL_DATA_NOTION_CONNECTION],
    }

    jira_session = request.cookies.get("neuron_jira_session")
    if jira_session:
        try:
            settings = JiraOAuthSettings.from_env()
            store = OAuthConnectorStore(
                DATA_DIR / os.getenv("OAUTH_CONNECTOR_STATE_DB", "oauth_connectors.sqlite3"),
                "jira", settings.encryption_key,
            )
            rows = store.list_connections(store.session_hash(jira_session))
            allowed["jira"] += [str(row["connection_id"]) for row in rows]
        except Exception:
            pass

    github_session = request.cookies.get("neuron_github_session")
    if github_session:
        try:
            store = GitHubStore(github_state_db_path())
            rows = store.list_installations(store.session_hash(github_session))
            allowed["github"] = [str(row["installation_id"]) for row in rows]
        except Exception:
            pass

    bitbucket_session = request.cookies.get("neuron_bitbucket_session")
    if bitbucket_session:
        try:
            settings = BitbucketOAuthSettings.from_env()
            store = OAuthConnectorStore(
                DATA_DIR / os.getenv("OAUTH_CONNECTOR_STATE_DB", "oauth_connectors.sqlite3"),
                "bitbucket", settings.encryption_key,
            )
            rows = store.list_connections(store.session_hash(bitbucket_session))
            allowed["bitbucket"] += [str(row["connection_id"]) for row in rows]
        except Exception:
            pass

    notion_session = request.cookies.get("neuron_notion_session")
    if notion_session:
        try:
            settings = NotionOAuthSettings.from_env()
            store = NotionStore(notion_state_db_path(), settings.encryption_key)
            rows = store.list_connections(store.session_hash(notion_session))
            allowed["notion"] += [str(row["workspace_id"]) for row in rows]
        except Exception:
            pass

    return AccessScope.from_connections(allowed)
