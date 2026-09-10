"""Build a consistent, safe source header for semantic chunks."""

from __future__ import annotations

from connectors.core.hashing import normalize_text
from connectors.core.models import SourceRecord


def source_header(record: SourceRecord) -> str:
    lines = [
        "[SOURCE]",
        f"Provider: {record.provider}",
        f"Entity type: {record.entity_type}",
        f"Title: {normalize_text(record.name).strip()}",
    ]
    if record.breadcrumbs:
        lines.append("Path: " + " / ".join(item.name for item in record.breadcrumbs))
    if record.url:
        lines.append(f"URL: {record.url}")
    lines.append(f"External ID: {record.external_id}")
    if record.created_at:
        lines.append(f"Created at: {record.created_at.isoformat()}")
    if record.updated_at:
        lines.append(f"Updated at: {record.updated_at.isoformat()}")
    return "\n".join(lines)


def episode_body(record: SourceRecord, chunk_text: str) -> str:
    return f"{source_header(record)}\n\n[CONTENT]\n{chunk_text.strip()}".strip()
