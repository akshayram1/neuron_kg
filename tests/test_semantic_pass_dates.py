"""25-plan.md §5.1 "Stated vs record time" -- wiring `graph/dates.py`'s
`stated_dates(evidence, reference_time)` into `_write_extraction`'s
fact-writing loop in `graph/semantic_pass.py`.

Same fake-graph convention as `tests/test_admission_gates.py` (a `_FakeGraph`
just real enough for `_write_extraction`'s fact loop), extended here to
record the params of every `graph.query` call so the row dict handed to
`w.upsert_fact_edges` -- and this task's stopgap supplementary write, see
QUERIES.md -- can be inspected directly, not just trusted by inspection.

The subject/relation/object triple (`Project -PROVIDES_API-> Api`) and the
`record_own_kind="Project"`/`primary_uid="proj-1"` wiring below are copied
from `tests/test_admission_gates.py::test_fact_passing_every_gate_is_still_
written`, the one case already proven (before this change) to clear every
admission gate and reach the write call with this `_FakeGraph` -- so any
failure here is about date wiring, not an unrelated endpoint-resolution
difference.

A real-FalkorDB end-to-end check (same convention as
`tests/test_writer_temporal.py`: `NEURON_INTEGRATION=1`, one throwaway graph
per test, deleted in `finally`) confirms the properties actually land on the
written edge, including the two fields (`valid_at_basis`, `invalid_at`) that
`w.upsert_fact_edges` does not yet natively accept -- see QUERIES.md.
"""

from __future__ import annotations

import os
import uuid

import pytest

from connectors.core.ledger import ConnectorLedger, PendingChunk
from graph.profiles import ExtractedFact, WorkManagementExtraction
from graph.token_usage import TokenUsage
from graph.axioms import DEFAULT_AXIOMS
from graph.semantic_pass import _write_extraction

SOURCE_TIME = "2026-01-01T00:00:00Z"


class _FakeResult:
    def __init__(self, rows):
        self.result_set = rows


class _CapturingFakeGraph:
    """Same shape as `test_admission_gates.py::_FakeGraph`, plus recording
    every call's params so tests can inspect exactly what was written."""

    def __init__(self, source_time: str | None = SOURCE_TIME):
        self.source_time = source_time
        self.calls: list[tuple[str, dict]] = []

    def query(self, cypher, params=None):
        params = params or {}
        self.calls.append((cypher, params))
        if "SourceRecord" in cypher:
            return _FakeResult([[self.source_time]] if self.source_time is not None else [])
        if "RETURN n.uid LIMIT 1" in cypher:
            return _FakeResult([[params["uid"]]])
        return _FakeResult([])

    def fact_edge_calls(self):
        """Calls made by `w.upsert_fact_edges` (the UNWIND/MERGE write)."""
        return [(c, p) for c, p in self.calls if "MERGE (a)-[r:" in c]

    def stopgap_calls(self):
        """Calls made by `_write_extraction`'s own supplementary SET (this
        task's stopgap for `valid_at_basis`/`invalid_at`, see QUERIES.md)."""
        return [(c, p) for c, p in self.calls if "valid_at_basis" in c and "MERGE" not in c]


def _extraction(*facts: ExtractedFact) -> WorkManagementExtraction:
    return WorkManagementExtraction(facts=list(facts))


def _write(graph, ledger, chunk, extraction):
    return _write_extraction(
        graph, ledger, chunk, extraction, "proj-1", "Project",
        client=None, embedding_model="test-embed", extraction_model="test-model",
        profile_name="work_management", token_usage=TokenUsage(), axioms=DEFAULT_AXIOMS,
    )


def _fact(evidence: str) -> ExtractedFact:
    return ExtractedFact(
        subject_name="Order Service", subject_kind="Project", relation="PROVIDES_API",
        object_name="Checkout API", object_kind="Api", evidence=evidence,
    )


# --------------------------------------------------------- row-dict wiring

def test_stated_start_date_sets_valid_at_and_basis(tmp_path):
    graph = _CapturingFakeGraph()
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service has provided the Checkout API since 12 March 2026.",
    )
    fact = _fact("Order Service has provided the Checkout API since 12 March 2026")

    entities, written, rejected = _write(graph, ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 1, 0)
    [(_, params)] = graph.fact_edge_calls()
    row = params["rows"][0]
    assert row["valid_at"] == "2026-03-12"
    assert row["valid_at_basis"] == "stated"
    assert row.get("invalid_at") is None
    assert row["ended_unknown"] is False

    # Stopgap: writer.py doesn't accept valid_at_basis/invalid_at yet (see
    # QUERIES.md), so `_write_extraction` persists valid_at_basis itself.
    [(_, stopgap_params)] = graph.stopgap_calls()
    assert stopgap_params["valid_at_basis"] == "stated"
    assert "invalid_at" not in stopgap_params  # no end was stated


def test_no_stated_date_falls_back_to_source_time_unchanged(tmp_path):
    """Critical no-regression case: evidence with no stated date at all must
    produce the exact same `valid_at` as before this change (`source_time`),
    with the new `valid_at_basis` correctly defaulting to `record_time` and
    NO extra graph write (the stopgap is skipped entirely for this, the
    common, case)."""
    graph = _CapturingFakeGraph()
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provides the Checkout API to downstream teams.",
    )
    fact = _fact("Order Service provides the Checkout API")

    entities, written, rejected = _write(graph, ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 1, 0)
    [(_, params)] = graph.fact_edge_calls()
    row = params["rows"][0]
    assert row["valid_at"] == SOURCE_TIME
    assert row["valid_at_basis"] == "record_time"
    assert "invalid_at" not in row
    assert row["ended_unknown"] is False
    assert graph.stopgap_calls() == []  # no extra query for the common case


def test_resolvable_stated_end_sets_invalid_at(tmp_path):
    graph = _CapturingFakeGraph()
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provided the Checkout API until 4 August 2026.",
    )
    fact = _fact("Order Service provided the Checkout API until 4 August 2026")

    entities, written, rejected = _write(graph, ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 1, 0)
    [(_, params)] = graph.fact_edge_calls()
    row = params["rows"][0]
    assert row["invalid_at"] == "2026-08-04"
    assert row["ended_unknown"] is False
    # No start was stated, so valid_at still falls back to source_time.
    assert row["valid_at"] == SOURCE_TIME
    assert row["valid_at_basis"] == "record_time"

    [(_, stopgap_params)] = graph.stopgap_calls()
    assert stopgap_params["invalid_at"] == "2026-08-04"


def test_unresolvable_stated_end_sets_ended_unknown_not_invalid_at(tmp_path):
    graph = _CapturingFakeGraph()
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provided the Checkout API until the migration.",
    )
    fact = _fact("Order Service provided the Checkout API until the migration")

    entities, written, rejected = _write(graph, ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 1, 0)
    [(_, params)] = graph.fact_edge_calls()
    row = params["rows"][0]
    assert row["ended_unknown"] is True
    assert "invalid_at" not in row  # never set (not even None) when unresolved

    # ended_unknown is natively accepted by upsert_fact_edges already, and no
    # invalid_at/stated basis applies here, so no stopgap write is needed.
    assert graph.stopgap_calls() == []


def test_neither_start_nor_end_stated_matches_pre_existing_behavior(tmp_path):
    """Same scenario as the pre-Phase-5.1 admission-gates regression test
    (`test_admission_gates.py::test_fact_passing_every_gate_is_still_
    written`): a fact with plain evidence and no date language at all must
    still be written exactly as before -- one live edge, no drops."""
    graph = _CapturingFakeGraph()
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provides the Checkout API to downstream teams.",
    )
    fact = _fact("Order Service provides the Checkout API")

    entities, written, rejected = _write(graph, ledger, chunk, _extraction(fact))

    assert (entities, written, rejected) == (0, 1, 0)
    assert ledger.drop_counts() == {}
    edges = ledger.edges_for_record(chunk.record_key)
    assert len(edges) == 1
    assert edges[0].rel_type == "PROVIDES_API"
    assert edges[0].from_uid == "proj-1"


# --------------------------------------------------- real FalkorDB, end-to-end

pytestmark_integration = pytest.mark.skipif(
    os.getenv("NEURON_INTEGRATION") != "1",
    reason="set NEURON_INTEGRATION=1 to use local FalkorDB",
)


@pytest.fixture
def real_graph():
    from graph import writer as w
    from graph.falkor_client import build_client
    from graph.semantic_pass import semantic_uid

    client = build_client()
    g = client.select_graph(f"neuron_test_{uuid.uuid4().hex}")
    w.upsert_source_records(g, [{"record_key": "jira:c:project:proj-1", "source_time": SOURCE_TIME}])
    w.upsert_entities(g, "Project", [{"uid": "proj-1", "props": {"name": "Order Service"}}])
    # `_resolve_endpoint` (Api is a `_SEMANTIC_LABELS` kind) only resolves an
    # endpoint that already exists as a node -- it does not create one. The
    # `_FakeGraph`-based tests above fake this via a blanket "yes it exists"
    # response; a real graph needs the node actually present, at the same
    # deterministic uid `_resolve_endpoint` computes (`semantic_uid`).
    w.upsert_entities(g, "Api", [{"uid": semantic_uid("Api", "Checkout API"), "props": {"name": "Checkout API"}}])
    try:
        yield g
    finally:
        g.delete()


@pytestmark_integration
def test_stated_start_date_lands_on_the_written_edge_in_falkordb(real_graph, tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service has provided the Checkout API since 12 March 2026.",
    )
    fact = _fact("Order Service has provided the Checkout API since 12 March 2026")

    entities, written, rejected = _write(real_graph, ledger, chunk, _extraction(fact))
    assert (entities, written, rejected) == (0, 1, 0)

    rows = real_graph.query(
        "MATCH (:Project {uid: 'proj-1'})-[r:PROVIDES_API]->(:Api) "
        "RETURN r.valid_at, r.valid_at_basis, r.invalid_at, r.ended_unknown"
    ).result_set
    assert len(rows) == 1
    valid_at, valid_at_basis, invalid_at, ended_unknown = rows[0]
    assert valid_at == "2026-03-12"
    assert valid_at_basis == "stated"
    assert invalid_at is None
    assert ended_unknown is False


@pytestmark_integration
def test_no_stated_date_lands_as_record_time_in_falkordb(real_graph, tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provides the Checkout API to downstream teams.",
    )
    fact = _fact("Order Service provides the Checkout API")

    entities, written, rejected = _write(real_graph, ledger, chunk, _extraction(fact))
    assert (entities, written, rejected) == (0, 1, 0)

    rows = real_graph.query(
        "MATCH (:Project {uid: 'proj-1'})-[r:PROVIDES_API]->(:Api) "
        "RETURN r.valid_at, r.valid_at_basis, r.invalid_at, r.ended_unknown"
    ).result_set
    assert len(rows) == 1
    valid_at, valid_at_basis, invalid_at, ended_unknown = rows[0]
    # No date stated in the evidence -- `valid_at` falls back to the
    # record's own `source_time` from the `SourceRecord` node, exactly as
    # before this change.
    assert valid_at == SOURCE_TIME
    # No stopgap write happens for the "record_time"/no-invalid_at case, so
    # `valid_at_basis` is simply absent on the edge -- exactly like every
    # edge written before this feature existed. A future reader (chat.py)
    # should treat a missing `valid_at_basis` as `record_time`.
    assert valid_at_basis is None
    assert invalid_at is None
    assert ended_unknown is False


@pytestmark_integration
def test_resolvable_end_date_lands_on_the_written_edge_in_falkordb(real_graph, tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    chunk = PendingChunk(
        "jira:c:project:proj-1", "chunk-1", 0,
        "Order Service provided the Checkout API until 4 August 2026.",
    )
    fact = _fact("Order Service provided the Checkout API until 4 August 2026")

    entities, written, rejected = _write(real_graph, ledger, chunk, _extraction(fact))
    assert (entities, written, rejected) == (0, 1, 0)

    rows = real_graph.query(
        "MATCH (:Project {uid: 'proj-1'})-[r:PROVIDES_API]->(:Api) "
        "RETURN r.invalid_at, r.ended_unknown"
    ).result_set
    invalid_at, ended_unknown = rows[0]
    assert invalid_at == "2026-08-04"
    assert ended_unknown is False
