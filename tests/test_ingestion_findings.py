"""ConnectorLedger.record_ingestion_assessments / findings / finding_evidence.

Exercises the fix for graph/semantic_pass.py's previously-always-a-no-op
`getattr(ledger, "record_ingestion_assessments", None)` call: before this,
only the removed story demo's Postgres-only `storage/ledger.py` implemented
it, so no real (or synthetic) connector sync ever persisted an LLM's
`should_flag=True` ingestion judgement anywhere."""

from __future__ import annotations

import pytest

from connectors.core.ledger import ConnectorLedger

BOTH = pytest.mark.parametrize("sql_backend", ["sqlite", "postgres"], indirect=True)


def _flagged(**overrides) -> dict:
    base = {
        "action": "contradiction", "topic_key": "Auth v1 still called!!",
        "title": "Auth v1 still in use", "summary": "Consumer still calls the deprecated endpoint.",
        "reasoning": "Commit X still imports the v1 client.", "evidence": "still using /v1/auth",
        "severity": "high", "confidence": 0.8, "should_flag": True, "related_candidate_uids": [],
    }
    base.update(overrides)
    return base


def test_hasattr_now_true(tmp_path):
    # This is the exact check that showed the gap: real ConnectorLedger never
    # had this method, so graph/semantic_pass.py's `getattr(..., None)` guard
    # silently skipped every assessment it computed.
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    assert hasattr(ledger, "record_ingestion_assessments")


@BOTH
def test_unflagged_assessment_is_not_persisted(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    written = ledger.record_ingestion_assessments(
        "notion:ws:document:page-1", "chunk-1",
        [_flagged(should_flag=False), {"action": "addition"}],  # no should_flag at all
    )
    assert written == []
    assert ledger.findings() == []


@BOTH
def test_flagged_assessment_is_persisted_and_readable(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    written = ledger.record_ingestion_assessments(
        "notion:ws:document:page-1", "chunk-1", [_flagged()],
    )
    assert len(written) == 1
    finding_key = written[0]
    assert finding_key.startswith("llm:notion:contradiction:")

    findings = ledger.findings()
    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_key == finding_key
    assert finding.record_key == "notion:ws:document:page-1"
    assert finding.chunk_id == "chunk-1"
    assert finding.kind == "llm_contradiction"
    assert finding.severity == "high"
    assert finding.status == "open"
    assert finding.title == "Auth v1 still in use"
    assert finding.confidence == pytest.approx(0.8)
    assert finding.properties["action"] == "contradiction"
    assert finding.stale_at is None

    evidence = ledger.finding_evidence(finding_key)
    assert len(evidence) == 1
    assert evidence[0]["record_key"] == "notion:ws:document:page-1"
    assert evidence[0]["excerpt"] == "still using /v1/auth"


@BOTH
def test_reprocessing_the_same_chunk_marks_old_finding_stale(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_ingestion_assessments("notion:ws:document:page-1", "chunk-1", [_flagged()])

    # Re-extraction of the SAME chunk that no longer raises anything.
    ledger.record_ingestion_assessments("notion:ws:document:page-1", "chunk-1", [])

    findings = ledger.findings()
    assert len(findings) == 1
    assert findings[0].status == "stale"
    assert findings[0].stale_reason is not None
    assert ledger.findings(status="open") == []
    assert [f.finding_key for f in ledger.findings(status="stale")] == [findings[0].finding_key]


@BOTH
def test_reprocessing_a_different_chunk_does_not_disturb_other_findings(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_ingestion_assessments("notion:ws:document:page-1", "chunk-1", [_flagged()])
    ledger.record_ingestion_assessments("notion:ws:document:page-2", "chunk-9", [])
    findings = ledger.findings()
    assert len(findings) == 1
    assert findings[0].status == "open"


@BOTH
def test_two_records_on_the_same_topic_consolidate_into_one_finding(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    ledger.record_ingestion_assessments(
        "jira:conn:issue:DATAOS-1", "chunk-a", [_flagged(topic_key="auth-v1-removal")],
    )
    ledger.record_ingestion_assessments(
        "jira:conn:issue:DATAOS-2", "chunk-b", [_flagged(topic_key="auth-v1-removal")],
    )
    findings = ledger.findings()
    assert len(findings) == 1  # same provider + action + topic_key -> same finding_key
    finding_key = findings[0].finding_key
    evidence = ledger.finding_evidence(finding_key)
    assert {row["record_key"] for row in evidence} == {
        "jira:conn:issue:DATAOS-1", "jira:conn:issue:DATAOS-2",
    }


@BOTH
def test_defaults_for_missing_fields(tmp_path, sql_backend):
    ledger = ConnectorLedger(tmp_path / "l.sqlite3")
    written = ledger.record_ingestion_assessments(
        "bitbucket:conn:pull_request:1", "chunk-1",
        [{"should_flag": True}],  # everything else absent
    )
    assert len(written) == 1
    finding = ledger.findings()[0]
    assert finding.kind == "llm_review"  # default action
    assert finding.severity == "warning"
    assert finding.title == "Evidence requires review"
    assert finding.confidence == pytest.approx(0.5)
