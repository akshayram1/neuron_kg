"""Jira semantic pass (plan.md §3 Pass B, Block 7) — budgeted LLM extraction
of Decision/Term/System entities and their facts from free text already
chunked and saved by the deterministic pass (Block 6). Structural facts
(assignee, status, project membership) are never re-derived here.

Endpoint resolution is deliberately conservative — verified against a real
extraction call during development, which showed the LLM's rendering of a
WorkItem's own name doesn't reliably match the node's actual `name` property
byte-for-byte (an em dash vs. a hyphen, in one observed case). Rather than
fuzzy-match names, this only resolves:
  - Decision/Term/System endpoints — by normalized name, always (they're
    cross-source entities by design, plan.md §5 Tier 1).
  - the chunk's OWN WorkItem/Project — via the ledger's `primary_node_uid`,
    which is exact by construction, never by name.
  - a Person — only if their name exactly matches (case-insensitive) an
    ASSIGNED_TO/REPORTED_BY neighbor already on that WorkItem.
Anything else (e.g. a different WorkItem referenced by name in free text)
is rejected, not guessed — plan.md: never fabricate an unverified link. That
harder case is exactly what Tier-3 similarity + LLM adjudication (plan.md §5)
is for, once there's a cross-source graph to adjudicate against.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

from falkordb import Graph
from openai import OpenAI

from connectors.core.ledger import (
    ConnectorLedger,
    PendingChunk,
    RecordEdgeRef,
    SemanticStatus,
)
from graph import writer as w
from graph.ontology import is_relation_allowed
from graph.profiles import WorkManagementExtraction, profile_for_record_key
from graph.search import find_similar_uid

logger = logging.getLogger("neuron.semantic_pass")

_SEMANTIC_LABELS = {"Decision", "Term", "System"}
_ENTITY_TYPE_TO_LABEL = {
    "work_item": "WorkItem", "project": "Project", "repository": "Repository",
    "source_file": "SourceFile", "commit": "Commit", "page": "Document",
    "workspace": "Workspace",
}
# Must match graph.schema.VECTOR_LABELS -- System has no vector index (it's
# usually just a proper noun with little embeddable text), Project isn't a
# content-bearing label either.
_EMBEDDABLE_LABELS = {"WorkItem", "Document", "Decision", "Term"}


def _embed(client: OpenAI, model: str, texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    response = client.embeddings.create(model=model, input=texts)
    return [item.embedding for item in response.data]


def semantic_uid(kind: str, name: str) -> str:
    """Same name -> same uid regardless of which record mentioned it, so
    entities merge across sources instead of duplicating (plan.md §5 Tier 1)."""
    return w.make_uid(kind, name.strip().lower())


def _record_own_kind(record_key: str) -> str | None:
    # record_key = "provider:connection_id:entity_type:external_id" and
    # external_id itself may contain ':' (e.g. "cloud1:10045") -- maxsplit=3
    # keeps the first three fields exact regardless.
    parts = record_key.split(":", 3)
    return _ENTITY_TYPE_TO_LABEL.get(parts[2]) if len(parts) >= 3 else None


@dataclass
class SemanticPassResult:
    chunks_processed: int = 0
    llm_calls: int = 0
    entities_written: int = 0
    facts_written: int = 0
    facts_rejected: int = 0
    records_completed: int = 0


def _resolve_endpoint(
    graph: Graph,
    kind: str,
    name: str,
    record_primary_uid: str,
    record_own_kind: str | None,
    semantic_uids: dict[tuple[str, str], str],
) -> str | None:
    if kind in _SEMANTIC_LABELS:
        key = (kind, name.strip().lower())
        if key in semantic_uids:
            return semantic_uids[key]
        uid = semantic_uid(kind, name)
        rows = graph.query(
            "MATCH (n {uid: $uid}) RETURN n.uid LIMIT 1", params={"uid": uid}
        ).result_set
        return rows[0][0] if rows else None
    if kind == record_own_kind:
        return record_primary_uid
    if kind == "Person":
        result = graph.query(
            "MATCH (n {uid: $uid})-[:ASSIGNED_TO|REPORTED_BY|AUTHORED_BY]->(p:Person) "
            "WHERE toLower(p.name) = toLower($name) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid, "name": name},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Project" and record_own_kind == "WorkItem":
        result = graph.query(
            "MATCH (n {uid: $uid})-[:BELONGS_TO]->(p:Project) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Repository" and record_own_kind in {"SourceFile", "Commit"}:
        result = graph.query(
            "MATCH (p:Repository)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    if kind == "Workspace" and record_own_kind == "Document":
        result = graph.query(
            "MATCH (p:Workspace)-[:CONTAINS]->(n {uid: $uid}) RETURN p.uid LIMIT 1",
            params={"uid": record_primary_uid},
        )
        return result.result_set[0][0] if result.result_set else None
    return None


def _embedding_text(label: str, item) -> str:
    if label == "Decision":
        return " — ".join(filter(None, [item.name, item.statement, item.rationale]))
    if label == "Term":
        return " — ".join(filter(None, [item.name, item.definition]))
    return item.name


def _write_extraction(
    graph: Graph,
    ledger: ConnectorLedger,
    chunk: PendingChunk,
    extraction: WorkManagementExtraction,
    primary_uid: str,
    record_own_kind: str | None,
    client: OpenAI,
    embedding_model: str,
) -> tuple[int, int, int]:
    # An entity with no fact connecting it to anything is graph noise, not
    # knowledge -- a floating "Redis" node nobody can query into is worse
    # than not having it at all. Rather than trust the LLM to always attach
    # a fact to every entity it names (unreliable in practice: real syncs
    # showed System/Term entities extracted with zero accompanying facts),
    # enforce it structurally: only keep an extracted entity if it actually
    # appears as a fact's subject or object. This makes "every semantic node
    # has a reason to exist" a property of the write path, not a prompt hope.
    referenced: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        if not is_relation_allowed(fact.subject_kind, fact.relation, fact.object_kind):
            continue
        referenced.add((fact.subject_kind, fact.subject_name.strip().lower()))
        referenced.add((fact.object_kind, fact.object_name.strip().lower()))

    entities_written = 0
    semantic_uids: dict[tuple[str, str], str] = {}
    edges_supported: list[RecordEdgeRef] = []
    for label, all_items in (
        ("Term", extraction.terms), ("Decision", extraction.decisions), ("System", extraction.systems)
    ):
        items = []
        for item in all_items:
            if (label, item.name.strip().lower()) in referenced:
                items.append(item)
            else:
                logger.info("  dropped %s (no connecting fact): %r", label, item.name)
        if not items:
            continue

        if label in _EMBEDDABLE_LABELS:
            # Embed BEFORE deciding uid: a near-duplicate re-extraction (same
            # real thing, different LLM wording -- e.g. "use Redis-backed
            # sliding-window rate limiting" vs "...rate limiter" from two
            # near-identical tickets) won't share a normalized name, so
            # `semantic_uid` alone would silently create a second node for
            # the same real-world entity every time the wording drifts.
            # Verified against real duplicate tickets (see CHECKLIST).
            vectors = _embed(client, embedding_model, [_embedding_text(label, item) for item in items])
            uids = [
                find_similar_uid(graph, label, vector) or semantic_uid(label, item.name)
                for item, vector in zip(items, vectors)
            ]
        else:
            vectors = [None] * len(items)
            uids = [semantic_uid(label, item.name) for item in items]

        rows = [
            {"uid": uid, "props": {**item.model_dump(exclude={"name"}), "name": item.name}}
            for uid, item in zip(uids, items)
        ]
        w.upsert_entities(graph, label, rows)
        w.link_mentioned_in(graph, label, [{"uid": row["uid"], "record_key": chunk.record_key} for row in rows])
        entities_written += len(rows)
        for uid, item in zip(uids, items):
            semantic_uids[(label, item.name.strip().lower())] = uid
            merged_note = " (merged into existing node)" if uid != semantic_uid(label, item.name) else ""
            logger.info("  extracted %s: %r%s", label, item.name, merged_note)

        # SourceRecord nodes are hidden from the product canvas. This visible
        # lineage edge keeps semantic knowledge attached to the Jira
        # WorkItem/Project it came from, yielding one connected project graph.
        if record_own_kind:
            lineage_rows = [
                {
                    "from_uid": uid,
                    "to_uid": primary_uid,
                    "source_record_keys": [chunk.record_key],
                    "evidence": None,
                    "extraction_method": "deterministic",
                    "confidence": 1.0,
                }
                for uid in uids
            ]
            w.upsert_fact_edges(graph, "EXTRACTED_FROM", label, record_own_kind, lineage_rows)
            edges_supported.extend(
                RecordEdgeRef("EXTRACTED_FROM", uid, primary_uid) for uid in uids
            )

        if label in _EMBEDDABLE_LABELS:
            w.upsert_entity_embeddings(graph, label, [
                {"uid": uid, "embedding": vector} for uid, vector in zip(uids, vectors)
            ])

    facts_written = 0
    facts_rejected = 0
    for fact in extraction.facts:
        if not is_relation_allowed(fact.subject_kind, fact.relation, fact.object_kind):
            facts_rejected += 1
            logger.info(
                "  rejected fact (relation not allowed): (%s) %r -%s-> (%s) %r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
            )
            continue
        subject_uid = _resolve_endpoint(
            graph, fact.subject_kind, fact.subject_name,
            primary_uid, record_own_kind, semantic_uids,
        )
        object_uid = _resolve_endpoint(
            graph, fact.object_kind, fact.object_name,
            primary_uid, record_own_kind, semantic_uids,
        )
        if subject_uid is None or object_uid is None:
            facts_rejected += 1
            logger.info(
                "  rejected fact (endpoint unresolved): (%s) %r -%s-> (%s) %r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
            )
            continue
        w.upsert_fact_edges(graph, fact.relation, fact.subject_kind, fact.object_kind, [{
            "from_uid": subject_uid, "to_uid": object_uid, "source_record_keys": [chunk.record_key],
            "evidence": fact.evidence, "extraction_method": "llm", "confidence": 0.9,
        }])
        edges_supported.append(RecordEdgeRef(fact.relation, subject_uid, object_uid))
        facts_written += 1
        logger.info(
            "  wrote fact: (%s) %r -%s-> (%s) %r  evidence=%r",
            fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name, fact.evidence,
        )

    if edges_supported:
        ledger.record_edges_batch(chunk.record_key, edges_supported)
    return entities_written, facts_written, facts_rejected


def run_semantic_pass(
    graph: Graph,
    ledger: ConnectorLedger,
    *,
    budget: int | None = None,
    client: OpenAI | None = None,
    model: str | None = None,
    record_prefix: str | None = None,
    on_progress: Callable[[int, int, str, SemanticPassResult], None] | None = None,
) -> SemanticPassResult:
    """Process up to `budget` pending chunks (default: $LLM_BUDGET_PER_RUN).
    A chunk that fails its LLM call is left 'pending' and retried on a later
    run rather than dropped — no retry-count/backoff yet (known v1 gap: a
    persistently failing chunk keeps consuming one budget slot per run)."""
    client = client or OpenAI()
    model = model or os.getenv("LLM_MODEL", "gpt-5.6-sol")
    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    budget = budget if budget is not None else int(os.getenv("LLM_BUDGET_PER_RUN", "200"))

    result = SemanticPassResult()
    touched_records: set[str] = set()

    chunks = ledger.pending_chunks(budget, record_prefix=record_prefix)
    total_chunks = len(chunks)
    for index, chunk in enumerate(chunks, 1):
        if on_progress is not None:
            on_progress(index - 1, total_chunks, chunk.record_key, result)
        entry = ledger.get(chunk.record_key)
        if entry is None or entry.primary_node_uid is None:
            logger.warning("no primary_node_uid for %s, skipping chunk", chunk.record_key)
            continue

        result.llm_calls += 1
        try:
            profile = profile_for_record_key(chunk.record_key)
            response = client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": profile.instructions},
                    {"role": "user", "content": chunk.text},
                ],
                text_format=profile.schema,
            )
            extraction: WorkManagementExtraction = response.output_parsed
        except Exception:
            logger.exception("LLM extraction failed for chunk %s of %s", chunk.chunk_id, chunk.record_key)
            continue

        entities, facts, rejected = _write_extraction(
            graph, ledger, chunk, extraction, entry.primary_node_uid,
            _record_own_kind(chunk.record_key), client, embedding_model,
        )
        result.chunks_processed += 1
        result.entities_written += entities
        result.facts_written += facts
        result.facts_rejected += rejected
        logger.info(
            "chunk %s of %s: %d terms, %d decisions, %d systems extracted, %d facts written, %d rejected",
            chunk.chunk_index, chunk.record_key,
            len(extraction.terms), len(extraction.decisions), len(extraction.systems), facts, rejected,
        )

        ledger.commit_chunk(chunk.record_key, chunk.chunk_id, SemanticStatus.DONE)
        touched_records.add(chunk.record_key)
        if on_progress is not None:
            on_progress(index, total_chunks, chunk.record_key, result)

    for record_key in touched_records:
        if not ledger.record_fully_processed(record_key):
            continue
        ledger.set_semantic_status(record_key, SemanticStatus.DONE)
        result.records_completed += 1

        # The record's own WorkItem gets one embedding over its full
        # search_text once semantic processing completes -- not per chunk,
        # since a multi-chunk record's embedding should represent the whole
        # thing, not one fragment.
        own_label = _record_own_kind(record_key)
        entry = ledger.get(record_key)
        if own_label in _EMBEDDABLE_LABELS and entry and entry.primary_node_uid:
            rows = graph.query(
                "MATCH (n {uid: $uid}) RETURN n.search_text", params={"uid": entry.primary_node_uid}
            ).result_set
            search_text = rows[0][0] if rows else None
            if search_text:
                vector = _embed(client, embedding_model, [search_text])[0]
                w.upsert_entity_embeddings(graph, own_label, [{"uid": entry.primary_node_uid, "embedding": vector}])

    logger.info(
        "semantic pass done: %d chunks, %d llm calls, %d entities, %d facts (%d rejected), %d records completed",
        result.chunks_processed, result.llm_calls, result.entities_written,
        result.facts_written, result.facts_rejected, result.records_completed,
    )
    return result
