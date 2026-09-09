from __future__ import annotations

import hashlib
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from connectors.core.chunking.code import chunk_code
from connectors.core.chunking.models import ChunkPolicy, SourceChunk
from connectors.core.chunking.semantic import chunk_semantic
from connectors.core.chunking.tokens import TokenCounter, default_token_counter
from connectors.core.hashing import normalize_text
from connectors.core.models import SourceRecord

_CODE_ENTITY_TYPES = {"code", "code_file", "source_file", "codesymbol"}


def is_code_record(record: SourceRecord) -> bool:
    return bool(record.language) or record.entity_type.lower() in _CODE_ENTITY_TYPES


def chunk_record(
    record: SourceRecord,
    policy: ChunkPolicy,
    *,
    counter: TokenCounter | None = None,
) -> list[SourceChunk]:
    counter = counter or default_token_counter()
    text = normalize_text(record.content).strip()
    if not text:
        return []
    if is_code_record(record):
        pieces, route = chunk_code(text, record.language, policy, counter)
    else:
        pieces = chunk_semantic(text, policy, counter)
        route = "semantic_structural"

    occurrences: defaultdict[str, int] = defaultdict(int)
    output: list[SourceChunk] = []
    for index, piece in enumerate(pieces):
        digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
        occurrence = occurrences[digest]
        occurrences[digest] += 1
        chunk_id = str(uuid5(NAMESPACE_URL, f"{record.record_key}:{digest}:{occurrence}"))
        output.append(
            SourceChunk(
                original_entity_id=record.external_id,
                chunk_id=chunk_id,
                chunk_index=index,
                text=piece,
                token_count=counter.count(piece),
                content_hash=digest,
                route=route,
            )
        )
    return output
