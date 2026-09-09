"""Normalize a source record into an action and stable chunks."""

from __future__ import annotations

from dataclasses import dataclass

from connectors.core.actions import RecordAction
from connectors.core.chunking.models import SourceChunk
from connectors.core.chunking.router import chunk_record
from connectors.core.hashing import record_content_hash
from connectors.core.ledger import ConnectorLedger
from connectors.core.models import SourceDeletion, SourceRecord
from graph.profiles import ExtractionProfile, profile_for_record


@dataclass(frozen=True)
class PreparedRecord:
    record: SourceRecord
    action: RecordAction
    content_hash: str
    profile: ExtractionProfile
    chunks: tuple[SourceChunk, ...]


@dataclass(frozen=True)
class PreparedDeletion:
    deletion: SourceDeletion
    action: RecordAction


def prepare_record(record: SourceRecord, ledger: ConnectorLedger) -> PreparedRecord:
    """Hash first; unchanged records never enter the chunking path."""
    content_hash = record_content_hash(record)
    action = ledger.plan(record.record_key, content_hash)
    profile = profile_for_record(record)
    chunks = (
        tuple(chunk_record(record, profile.chunk_policy))
        if action in {RecordAction.INSERT, RecordAction.UPDATE}
        else ()
    )
    return PreparedRecord(record, action, content_hash, profile, chunks)


def prepare_deletion(deletion: SourceDeletion, ledger: ConnectorLedger) -> PreparedDeletion:
    return PreparedDeletion(
        deletion=deletion,
        action=ledger.plan(deletion.record_key, None, deleted=True),
    )
