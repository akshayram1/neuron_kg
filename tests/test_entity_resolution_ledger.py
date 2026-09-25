"""Phase 4 entity resolution ladder storage (25-plan.md §4.5, §4.6, §4.8):
`ConnectorLedger`'s `entity_aliases`, `mention_stoplist`, and
`resolution_stats` tables, plus `DropReason.GENERIC_MENTION`.

Same convention as `tests/test_sync_coverage.py` and `tests/test_reviews.py`
-- a real temp-file `ConnectorLedger`, no mocking. This file only exercises
the storage/accessor layer built here; the resolution ladder logic itself
(mention filter, scoped identity, vector/Laya rungs) lives in
`graph/semantic_pass.py` and is out of scope for these tests.
"""

from __future__ import annotations

from connectors.core.ledger import (
    ConnectorLedger,
    DropReason,
    ExtractionDrop,
)

# --------------------------------------------------------------- entity_aliases


def test_add_and_lookup_alias(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("Term", "ns-1", "k8s", "uid-kubernetes", source="manual")

    assert ledger.lookup_alias("Term", "ns-1", "k8s") == "uid-kubernetes"


def test_alias_add_is_idempotent_no_duplicate_row(tmp_path):
    """Re-adding the same (label, namespace_uid, alias_norm) updates the
    existing row rather than inserting a second one -- this is a lookup
    table, not an append-only log."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("Term", "ns-1", "k8s", "uid-a", source="manual")
    ledger.add_entity_alias("Term", "ns-1", "k8s", "uid-b", source="review:42")

    aliases = ledger.aliases_for_uid("uid-b")
    assert len(aliases) == 1
    assert aliases[0].uid == "uid-b"
    assert aliases[0].source == "review:42"
    # The stale mapping to uid-a is gone, not sitting alongside the new one.
    assert ledger.aliases_for_uid("uid-a") == []
    assert ledger.lookup_alias("Term", "ns-1", "k8s") == "uid-b"


def test_alias_lookup_is_scoped_by_label_and_namespace(tmp_path):
    """Aliases are namespace-aware (plan.md §4.0): the same alias_norm in a
    different namespace, or under a different label, is a different key and
    must not cross-resolve."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("System", "ns-1", "billing-svc", "uid-1", source="manual")
    ledger.add_entity_alias("System", "ns-2", "billing-svc", "uid-2", source="manual")
    ledger.add_entity_alias("Term", "ns-1", "billing-svc", "uid-3", source="manual")

    assert ledger.lookup_alias("System", "ns-1", "billing-svc") == "uid-1"
    assert ledger.lookup_alias("System", "ns-2", "billing-svc") == "uid-2"
    assert ledger.lookup_alias("Term", "ns-1", "billing-svc") == "uid-3"


def test_alias_lookup_miss_returns_none(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.lookup_alias("Term", "ns-1", "does-not-exist") is None


def test_alias_supports_namespaceless_labels(tmp_path):
    """Decision identity is not namespace-scoped (plan.md §4.0): passing
    namespace_uid=None must round-trip through '' consistently for both
    add and lookup."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("Decision", None, "use redis for caching", "uid-decision-1", source="manual")

    assert ledger.lookup_alias("Decision", None, "use redis for caching") == "uid-decision-1"


def test_list_aliases_for_uid(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_entity_alias("Term", "ns-1", "k8s", "uid-kubernetes", source="manual")
    ledger.add_entity_alias("Term", "ns-1", "kube", "uid-kubernetes", source="review:7")
    ledger.add_entity_alias("Term", "ns-1", "kubernetes-cluster", "uid-other", source="manual")

    aliases = ledger.aliases_for_uid("uid-kubernetes")
    assert {a.alias_norm for a in aliases} == {"k8s", "kube"}
    assert all(a.uid == "uid-kubernetes" for a in aliases)

    assert ledger.aliases_for_uid("uid-does-not-exist") == []


# --------------------------------------------------------------- mention_stoplist


def test_seed_terms_present_after_table_creation(tmp_path):
    """The plan's seed set (`data`, `pipeline`, `source`, `table`,
    `service`, `api`, `system`, `config`, `batch source`, `source_table`)
    must already be stoplisted the moment a ledger is created, with no
    explicit seeding call required."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    for term in (
        "data", "pipeline", "source", "table", "service", "api", "system",
        "config", "batch source", "source_table",
    ):
        assert ledger.is_stoplisted(term), f"expected seed term {term!r} to be stoplisted"


def test_stoplist_lookup_is_case_and_whitespace_normalized(tmp_path):
    """"Normalized" means lowercased and stripped -- a caller should not
    have to pre-clean the mention text before checking it."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.is_stoplisted("Data")
    assert ledger.is_stoplisted("  DATA  ")
    assert ledger.is_stoplisted("Pipeline")


def test_stoplist_lookup_miss(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.is_stoplisted("kubernetes") is False
    assert ledger.is_stoplisted("blast radius") is False


def test_add_and_remove_stoplist_term(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.is_stoplisted("widget") is False

    ledger.add_stoplist_term("Widget", reason="too generic in practice")
    assert ledger.is_stoplisted("widget") is True
    assert ledger.is_stoplisted("WIDGET") is True

    ledger.remove_stoplist_term("widget")
    assert ledger.is_stoplisted("widget") is False


def test_add_stoplist_term_is_idempotent(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_stoplist_term("widget", reason="first")
    ledger.add_stoplist_term("widget", reason="second")

    terms = [t for t in ledger.stoplist_terms() if t.term_norm == "widget"]
    assert len(terms) == 1
    assert terms[0].reason == "second"


def test_stoplist_term_can_be_scoped_to_one_label(tmp_path):
    """A non-global entry only blocks the label it was scoped to; a global
    (seeded) entry blocks every label the mention filter checks."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.add_stoplist_term("gadget", label="Term")

    assert ledger.is_stoplisted("gadget", label="Term") is True
    assert ledger.is_stoplisted("gadget", label="System") is False
    assert ledger.is_stoplisted("gadget") is False  # no label given -> global-only check

    # Seeded terms are global and block every label.
    assert ledger.is_stoplisted("data", label="Term") is True
    assert ledger.is_stoplisted("data", label="System") is True


# --------------------------------------------------------------- resolution_stats


def test_record_resolution_accumulates_for_same_combination(tmp_path):
    """`_write_extraction` resolves many entities per run -- repeated calls
    for the same (run_id, label, resolved_by) must accumulate, not
    overwrite."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_resolution("run-1", "Term", "scoped_exact")
    ledger.record_resolution("run-1", "Term", "scoped_exact")
    ledger.record_resolution("run-1", "Term", "scoped_exact", count=3)

    stats = ledger.resolution_stats_for_run("run-1")
    assert len(stats) == 1
    assert stats[0].label == "Term"
    assert stats[0].resolved_by == "scoped_exact"
    assert stats[0].count == 5


def test_record_resolution_keeps_different_combinations_separate(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_resolution("run-1", "Term", "scoped_exact")
    ledger.record_resolution("run-1", "Term", "vector")
    ledger.record_resolution("run-1", "System", "scoped_exact")
    ledger.record_resolution("run-2", "Term", "scoped_exact")

    run1 = {(s.label, s.resolved_by): s.count for s in ledger.resolution_stats_for_run("run-1")}
    assert run1 == {("Term", "scoped_exact"): 1, ("Term", "vector"): 1, ("System", "scoped_exact"): 1}

    run2 = {(s.label, s.resolved_by): s.count for s in ledger.resolution_stats_for_run("run-2")}
    assert run2 == {("Term", "scoped_exact"): 1}


def test_resolution_stats_for_unknown_run_is_empty(tmp_path):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert ledger.resolution_stats_for_run("no-such-run") == []


def test_resolution_stats_read_back_covers_the_ladders_vocabulary(tmp_path):
    """Not an enum -- the ladder's current outcomes (§4.2) must all be
    representable as plain strings without any schema change."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    for resolved_by in ("scoped_exact", "alias", "vector", "review_required", "laya_suggest", "laya", "new"):
        ledger.record_resolution("run-1", "Term", resolved_by)

    stats = ledger.resolution_stats_for_run("run-1")
    assert {s.resolved_by for s in stats} == {
        "scoped_exact", "alias", "vector", "review_required", "laya_suggest", "laya", "new",
    }
    assert all(s.count == 1 for s in stats)


# --------------------------------------------------------------- DropReason.GENERIC_MENTION


def test_generic_mention_is_a_valid_drop_reason():
    assert DropReason.GENERIC_MENTION == "generic_mention"


def test_generic_mention_round_trips_through_record_drops(tmp_path):
    """§4.5: rejected mentions are recorded and counted "like record_miss
    does for relations" -- in practice via the same generic
    record_drops/drop_counts/drops mechanism every other DropReason already
    uses, verified here for GENERIC_MENTION specifically."""
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_drops("notion:c:page:p1", "chunk-1", [
        ExtractionDrop(reason=DropReason.GENERIC_MENTION, subject_kind="Term", subject_name="data"),
        ExtractionDrop(reason=DropReason.GENERIC_MENTION, subject_kind="Term", subject_name="pipeline"),
        ExtractionDrop(reason=DropReason.RELATION_NOT_ALLOWED, subject_kind="Decision"),
    ])

    counts = ledger.drop_counts()
    assert counts["generic_mention"] == 2
    assert counts["relation_not_allowed"] == 1

    generic = ledger.drops(reason=DropReason.GENERIC_MENTION)
    assert {d.subject_name for d in generic} == {"data", "pipeline"}
    assert all(d.reason == "generic_mention" for d in generic)
