"""Resolve source changes before doing expensive chunking or LLM work."""

from __future__ import annotations

from enum import StrEnum


class RecordAction(StrEnum):
    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"
    KEEP = "keep"


def resolve_action(
    previous_hash: str | None,
    current_hash: str | None,
    *,
    deleted: bool = False,
) -> RecordAction:
    if deleted:
        return RecordAction.DELETE if previous_hash is not None else RecordAction.KEEP
    if current_hash is None:
        raise ValueError("current_hash is required for a live source record")
    if previous_hash is None:
        return RecordAction.INSERT
    if previous_hash == current_hash:
        return RecordAction.KEEP
    return RecordAction.UPDATE
