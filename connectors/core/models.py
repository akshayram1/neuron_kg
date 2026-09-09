"""Canonical records emitted by every source adapter.

Provider adapters stop at this boundary. They must not construct Graphiti nodes,
edges, or prompts themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True)
class SourceBreadcrumb:
    external_id: str
    name: str
    entity_type: str | None = None


@dataclass(frozen=True)
class SourceAccess:
    """Source ACL information retained outside extraction prompts by default."""

    public: bool = False
    principals: tuple[str, ...] = ()
    policy_version: str = "1"


@dataclass(frozen=True)
class SourceSelection:
    external_id: str
    name: str
    entity_type: str
    parent_external_id: str | None = None
    selectable: bool = True


@dataclass(frozen=True)
class SourceRecord:
    provider: str
    connection_id: str
    entity_type: str
    external_id: str
    name: str
    content: str = ""
    url: str | None = None
    parent_external_id: str | None = None
    breadcrumbs: tuple[SourceBreadcrumb, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    mime_type: str | None = None
    language: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    access: SourceAccess = field(default_factory=SourceAccess)

    def __post_init__(self) -> None:
        required = {
            "provider": self.provider,
            "connection_id": self.connection_id,
            "entity_type": self.entity_type,
            "external_id": self.external_id,
            "name": self.name,
        }
        empty = [key for key, value in required.items() if not str(value).strip()]
        if empty:
            raise ValueError(f"SourceRecord requires non-empty: {', '.join(empty)}")
        # Stop callers from changing hash-relevant metadata after construction.
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "breadcrumbs", tuple(self.breadcrumbs))

    @property
    def record_key(self) -> str:
        return ":".join(
            (self.provider.lower(), self.connection_id, self.entity_type, self.external_id)
        )

    @property
    def reference_time(self) -> datetime | None:
        return self.updated_at or self.created_at


@dataclass(frozen=True)
class SourceDeletion:
    provider: str
    connection_id: str
    entity_type: str
    external_id: str
    deleted_at: datetime | None = None
    reason: str | None = None

    @property
    def record_key(self) -> str:
        return ":".join(
            (self.provider.lower(), self.connection_id, self.entity_type, self.external_id)
        )
