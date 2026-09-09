"""Deterministic, secret-safe hashes for canonical source records."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any, Mapping

from connectors.core.models import SourceRecord

_SENSITIVE_KEY = re.compile(
    r"(^|_)(access_token|refresh_token|id_token|oauth_token|api_token|auth_token|bearer_token)($|_)|secret|password|authorization|signed_url|private_key",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    """Normalize transport artifacts without changing meaningful indentation."""
    return unicodedata.normalize("NFC", value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n"))


def _safe_value(value: Any, *, path: str = "metadata") -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                raise ValueError(f"Sensitive field is not allowed in SourceRecord metadata: {path}.{key}")
            output[key] = _safe_value(item, path=f"{path}.{key}")
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_safe_value(item, path=path) for item in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def canonical_record_payload(record: SourceRecord) -> dict[str, Any]:
    return {
        "identity": record.record_key,
        "name": normalize_text(record.name).strip(),
        "content": normalize_text(record.content),
        "url": record.url,
        "parent_external_id": record.parent_external_id,
        "breadcrumbs": [
            {
                "external_id": item.external_id,
                "name": normalize_text(item.name).strip(),
                "entity_type": item.entity_type,
            }
            for item in record.breadcrumbs
        ],
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
        "mime_type": record.mime_type,
        "language": record.language,
        "metadata": _safe_value(record.metadata),
        "access_policy": {
            "public": record.access.public,
            "principals": sorted(set(record.access.principals)),
            "policy_version": record.access.policy_version,
        },
    }


def record_content_hash(record: SourceRecord) -> str:
    encoded = json.dumps(
        canonical_record_payload(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
