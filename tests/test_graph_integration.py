from __future__ import annotations

import os
from uuid import uuid4

import pytest

from connectors.core.ledger import ConnectorLedger
from connectors.github_app.api import GitHubCommit, GitHubRepository
from connectors.jira.api import JiraIssue, JiraPerson, JiraProject, JiraSite
from graph import github_pipeline, jira_pipeline, writer
from graph.access import AccessScope
from graph.falkor_client import build_client
from graph.graph_view import fetch_graph
from graph.history import fetch_fact_history


pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


def test_acl_history_and_reverse_order_resolution(tmp_path):
    client = build_client()
    graph = client.select_graph(f"neuron_test_{uuid4().hex}")
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")
    try:
        repository = GitHubRepository(
            repository_id=7, full_name="acme/login", name="login", owner="acme",
            default_branch="main", private=True, html_url="https://github.com/acme/login",
        )
        commit = GitHubCommit(
            sha="abcdef1234567890", message="Implement EAPD-1 Redis limiter",
            author_name="Akshay", author_email="akshay@example.com",
            authored_at="2026-01-02T00:00:00Z",
            html_url="https://github.com/acme/login/commit/abcdef1234567890",
        )
        github_pipeline.write_repository(graph, ledger, repository, 99)
        github_pipeline.write_commit(graph, ledger, repository, commit, 99)

        project = JiraProject("100", "EAPD", "Platform AI", "")
        site = JiraSite("cloud", "Rubikai", "https://rubikai.atlassian.net")
        person = JiraPerson("acct", "Akshay", "akshay@example.com")
        issue = JiraIssue(
            issue_id="1", key="EAPD-1", summary="Redis limiter",
            content="EAPD-1 Redis limiter", description="Redis limiter",
            status="To Do", issue_type="Task", assignee=person, reporter=person,
            labels=("security",), blocks=(), created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        jira_pipeline.write_project(graph, ledger, project, site, "jira-conn")
        jira_pipeline.write_issue(graph, ledger, issue, project, site, "jira-conn")

        all_scope = AccessScope.from_connections({"github": ["99"], "jira": ["jira-conn"]})
        view = fetch_graph(graph, all_scope)
        relations = {edge["label"] for edge in view["edges"]}
        assert "IMPLEMENTS" in relations
        assert "SAME_AS" in relations

        anonymous = fetch_graph(graph, AccessScope(allow_public=False))
        assert anonymous == {"nodes": [], "edges": []}
        jira_only = fetch_graph(
            graph, AccessScope.from_connections({"jira": ["jira-conn"]}, allow_public=False)
        )
        assert all("github" not in node["group"] for node in jira_only["nodes"])
        assert "IMPLEMENTS" not in {edge["label"] for edge in jira_only["edges"]}

        implements = next(edge for edge in view["edges"] if edge["label"] == "IMPLEMENTS")
        assert implements["validAt"] == "2026-01-02T00:00:00+00:00"
        assert implements["validAt"] == "2026-01-02T00:00:00+00:00"
        commit_key = github_pipeline.commit_record(repository, commit, 99).record_key
        writer.remove_record_support(graph, commit_key, [{
            "rel_type": "IMPLEMENTS", "from_uid": implements["source"],
            "to_uid": implements["target"],
        }])
        intervals = fetch_fact_history(graph, all_scope, implements["factUid"])
        assert len(intervals) == 1 and intervals[0]["state"] == "historical"

        writer.upsert_fact_edges(graph, "IMPLEMENTS", "Commit", "WorkItem", [{
            "from_uid": implements["source"], "to_uid": implements["target"],
            "source_record_keys": [commit_key], "evidence": "EAPD-1",
            "extraction_method": "exact_anchor", "confidence": 1.0,
            "extractor_version": "test", "model": None,
            "chunk_id": "chunk", "chunk_hash": "hash",
        }])
        intervals = fetch_fact_history(graph, all_scope, implements["factUid"])
        assert {item["state"] for item in intervals} == {"historical", "live"}
        assert next(item for item in intervals if item["state"] == "live")["chunkId"] == "chunk"
        valid_then = fetch_fact_history(
            graph, all_scope, implements["factUid"], valid_at="2026-01-02T00:00:00Z",
        )
        assert len(valid_then) == 1 and valid_then[0]["state"] == "historical"
    finally:
        graph.delete()
