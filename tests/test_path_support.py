from __future__ import annotations

import pytest

from connectors.core.ledger import ConnectorLedger
from graph.path_support import pair_support, path_support, propose_verified_links


class Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self):
        self.direct: set[frozenset[str]] = set()
        self.two_hop: set[frozenset[str]] = set()
        self.shared: set[frozenset[str]] = set()
        self.nodes = {
            "a": ("Alpha", "Alpha migration references Beta"),
            "b": ("Beta", "Beta implementation"),
            "c": ("Gamma", "unrelated"),
        }

    def query(self, cypher, params=None):
        params = params or {}
        pair = frozenset((params.get("a_uid"), params.get("b_uid")))
        if "RETURN coalesce(a.name" in cypher:
            a_name, a_text = self.nodes.get(params["a_uid"], ("", ""))
            b_name, b_text = self.nodes.get(params["b_uid"], ("", ""))
            return Result([[a_name, a_text, b_name, b_text]])
        if "count(DISTINCT mid)" in cypher:
            return Result([[1 if pair in self.two_hop else 0]])
        if "MENTIONED_IN" in cypher:
            return Result([[1 if pair in self.shared else 0]])
        if "RETURN count(r)" in cypher:
            return Result([[1 if pair in self.direct else 0]])
        raise AssertionError(cypher)


def test_pair_support_uses_strongest_verifiable_signal():
    graph = FakeGraph()
    graph.two_hop.add(frozenset(("a", "c")))
    graph.shared.add(frozenset(("b", "c")))

    assert pair_support(graph, "a", "b") == 0.7  # Beta appears in Alpha text
    assert pair_support(graph, "a", "c") == 0.8
    assert pair_support(graph, "b", "c") == 0.5


def test_path_support_is_geometric_mean_and_marks_low_support():
    graph = FakeGraph()
    graph.direct.add(frozenset(("a", "b")))

    result = path_support(graph, ["a", "b", "c"], minimum=0.4)

    assert result.pair_scores == (1.0, 0.01)
    assert result.score == pytest.approx(0.1)
    assert result.low_support is True


def test_explicit_verification_creates_review_candidate_not_edge(tmp_path):
    graph = FakeGraph()  # a->b has textual support but no direct edge
    ledger = ConnectorLedger(tmp_path / "ledger.sqlite3")

    class Classifier:
        def classify(self, text, head, tail):
            assert (head, tail) == ("Alpha", "Beta")
            return "REFERENCES", 0.91

    ids = propose_verified_links(graph, ledger, ["a", "b"], classifier=Classifier())

    assert len(ids) == 1
    candidate = ledger.get_link_candidate(ids[0])
    assert candidate.state == "pending"
    assert candidate.derived_rule == "query_verified"
    assert candidate.relation == "REFERENCES"
    assert candidate.confidence == pytest.approx(0.7)
