"""Tests for graph/expand.py (plan.md §1.3 one-hop expansion, §1.4 authority
tiers). Uses a real, uniquely-named FalkorDB graph per test -- same pattern
as tests/test_graph_integration.py -- so the actual Cypher in
`expand_neighbors` is exercised, not a mock. Each test creates and tears
down its own graph; nothing is left behind in a shared graph name.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from graph.access import AccessScope
from graph.expand import (
    HUB_LABELS,
    MAX_SEEDS,
    PER_SEED,
    TIER_ORDER,
    expand_neighbors,
    tier_for_extraction_method,
)
from graph.falkor_client import build_client

pytestmark = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


@pytest.fixture
def graph():
    client = build_client()
    g = client.select_graph(f"neuron_test_expand_{uuid4().hex}")
    try:
        yield g
    finally:
        g.delete()


def _node(graph, label: str, uid: str, name: str | None = None) -> None:
    graph.query(
        f"CREATE (n:{label} {{uid: $uid, name: $name}})",
        params={"uid": uid, "name": name or uid},
    )


def _mentioned_in(graph, uid: str, record_key: str, *, public: bool = True,
                   provider: str = "jira", connection_id: str = "conn1") -> None:
    """Give a node an accessible SourceRecord via MENTIONED_IN -- this is
    what expand_neighbors's ACL check runs against."""
    graph.query(
        """
        MERGE (sr:SourceRecord {record_key: $record_key})
        SET sr.public = $public, sr.provider = $provider,
            sr.connection_id = $connection_id, sr.deleted_at = null
        WITH sr
        MATCH (n {uid: $uid})
        CREATE (n)-[:MENTIONED_IN]->(sr)
        """,
        params={
            "uid": uid, "record_key": record_key, "public": public,
            "provider": provider, "connection_id": connection_id,
        },
    )


def _edge(graph, from_uid: str, rel: str, to_uid: str, *,
          derived: bool = False, extraction_method: str | None = "deterministic",
          invalid_at: str | None = None) -> None:
    graph.query(
        f"""
        MATCH (a {{uid: $from_uid}}), (b {{uid: $to_uid}})
        CREATE (a)-[r:{rel} {{
            derived: $derived, extraction_method: $extraction_method,
            invalid_at: $invalid_at
        }}]->(b)
        """,
        params={
            "from_uid": from_uid, "to_uid": to_uid, "derived": derived,
            "extraction_method": extraction_method, "invalid_at": invalid_at,
        },
    )


PUBLIC_SCOPE = AccessScope(allow_public=True)


def test_hub_labels_are_skipped_as_expansion_targets(graph):
    _node(graph, "WorkItem", "seed")
    _node(graph, "WorkItem", "n1")
    _node(graph, "Repository", "hub1")  # Repository is a HUB_LABELS member
    assert "Repository" in HUB_LABELS

    _edge(graph, "seed", "IMPLEMENTS", "n1")
    _edge(graph, "seed", "PARENT_OF", "hub1")
    _mentioned_in(graph, "n1", "sr-n1")
    _mentioned_in(graph, "hub1", "sr-hub1")

    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None)
    uids = {hit.uid for hit in hits}
    assert "n1" in uids
    assert "hub1" not in uids


def test_per_seed_cap_is_honoured(graph):
    _node(graph, "WorkItem", "seed")
    neighbor_count = PER_SEED + 3
    for i in range(neighbor_count):
        uid = f"n{i}"
        _node(graph, "WorkItem", uid)
        _edge(graph, "seed", "IMPLEMENTS", uid)
        _mentioned_in(graph, uid, f"sr-{uid}")

    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None)
    assert len(hits) == PER_SEED


def test_derived_edges_are_ordered_after_asserted(graph):
    _node(graph, "WorkItem", "seed")
    _node(graph, "WorkItem", "derived_target")
    _node(graph, "WorkItem", "asserted_target")
    _mentioned_in(graph, "derived_target", "sr-derived")
    _mentioned_in(graph, "asserted_target", "sr-asserted")

    # Derived edge written first so a naive "return query order" would put
    # it ahead of the asserted one -- the ordering must come from the
    # asserted-before-derived rule, not incidental write/scan order.
    _edge(graph, "seed", "REFERENCES", "derived_target",
          derived=True, extraction_method="derived")
    _edge(graph, "seed", "IMPLEMENTS", "asserted_target",
          derived=False, extraction_method="deterministic")

    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None, min_tier="derived")
    uids = [hit.uid for hit in hits]
    assert uids.index("asserted_target") < uids.index("derived_target")


def test_exclude_edges_is_honoured(graph):
    _node(graph, "WorkItem", "seed")
    _node(graph, "WorkItem", "n1")
    _edge(graph, "seed", "IMPLEMENTS", "n1")
    _mentioned_in(graph, "n1", "sr-n1")

    # Present without exclude_edges...
    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None)
    assert "n1" in {hit.uid for hit in hits}

    # ...but never appears once the exact (from_uid, rel, to_uid) triple is
    # excluded. This is the hidden-edge test Phase 0's harness depends on.
    hidden = frozenset({("seed", "IMPLEMENTS", "n1")})
    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None, exclude_edges=hidden)
    assert "n1" not in {hit.uid for hit in hits}


def test_acl_filtering_is_applied(graph):
    _node(graph, "WorkItem", "seed")
    _node(graph, "WorkItem", "visible")
    _node(graph, "WorkItem", "hidden")
    _edge(graph, "seed", "IMPLEMENTS", "visible")
    _edge(graph, "seed", "DOCUMENTS", "hidden")
    _mentioned_in(graph, "visible", "sr-visible", public=True)
    _mentioned_in(graph, "hidden", "sr-hidden", public=False)

    # PUBLIC_SCOPE only authorizes SourceRecords with public=true, and
    # `hidden`'s only SourceRecord is private -- it is graph-adjacent to the
    # seed but must never come back.
    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None)
    uids = {hit.uid for hit in hits}
    assert "visible" in uids
    assert "hidden" not in uids


def test_max_seeds_and_overall_clamp_are_honoured(graph):
    seed_uids = [f"seed{i}" for i in range(MAX_SEEDS + 2)]
    for i, seed_uid in enumerate(seed_uids):
        _node(graph, "WorkItem", seed_uid)
        neighbor_uid = f"n{i}"
        _node(graph, "WorkItem", neighbor_uid)
        _edge(graph, seed_uid, "IMPLEMENTS", neighbor_uid)
        _mentioned_in(graph, neighbor_uid, f"sr-{neighbor_uid}")

    hits = expand_neighbors(graph, seed_uids, PUBLIC_SCOPE, None)
    uids = {hit.uid for hit in hits}

    # Only the first MAX_SEEDS seeds are used, so neighbours of the last two
    # seeds must never appear even though real edges exist for them.
    assert len(hits) <= MAX_SEEDS * PER_SEED
    assert len(hits) == MAX_SEEDS
    for i in range(MAX_SEEDS, MAX_SEEDS + 2):
        assert f"n{i}" not in uids


def test_tier_for_extraction_method_covers_all_cases():
    assert tier_for_extraction_method("deterministic") == "primary"
    assert tier_for_extraction_method("exact_anchor") == "primary"
    assert tier_for_extraction_method("changelog") == "primary"
    assert tier_for_extraction_method("llm") == "secondary"
    assert tier_for_extraction_method("derived") == "derived"
    assert tier_for_extraction_method(None) == "unknown"
    assert tier_for_extraction_method("something_else") == "unknown"

    # Ordering: unknown < derived < secondary < primary (plan.md §1.4).
    assert TIER_ORDER.index("unknown") < TIER_ORDER.index("derived")
    assert TIER_ORDER.index("derived") < TIER_ORDER.index("secondary")
    assert TIER_ORDER.index("secondary") < TIER_ORDER.index("primary")


def test_min_tier_filters_out_lower_tier_candidates(graph):
    _node(graph, "WorkItem", "seed")
    _node(graph, "WorkItem", "primary_target")
    _node(graph, "WorkItem", "secondary_target")
    _mentioned_in(graph, "primary_target", "sr-primary")
    _mentioned_in(graph, "secondary_target", "sr-secondary")
    _edge(graph, "seed", "IMPLEMENTS", "primary_target",
          derived=False, extraction_method="deterministic")
    _edge(graph, "seed", "DOCUMENTS", "secondary_target",
          derived=False, extraction_method="llm")

    hits = expand_neighbors(graph, ["seed"], PUBLIC_SCOPE, None, min_tier="primary")
    uids = {hit.uid for hit in hits}
    assert "primary_target" in uids
    assert "secondary_target" not in uids
