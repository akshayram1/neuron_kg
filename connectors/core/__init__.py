"""Shared, provider-independent connector ingestion primitives."""

from connectors.core.actions import RecordAction, resolve_action
from connectors.core.models import (
    SourceAccess,
    SourceBreadcrumb,
    SourceDeletion,
    SourceRecord,
    SourceSelection,
)

__all__ = [
    "RecordAction",
    "SourceAccess",
    "SourceBreadcrumb",
    "SourceDeletion",
    "SourceRecord",
    "SourceSelection",
    "resolve_action",
]
