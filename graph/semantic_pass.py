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
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from falkordb import Graph
from openai import OpenAI

from connectors.core.ledger import (
    ConnectorLedger,
    DropReason,
    ExtractionDrop,
    PendingChunk,
    RecordEdgeRef,
    SemanticStatus,
)
from graph import vector_store
from graph import writer as w
from graph.axioms import SWAPPED, AxiomSet, DEFAULT_AXIOMS, load_axioms
from graph.profiles import WorkManagementExtraction, profile_for_record_key
from graph.search import find_similar_uid
from graph.token_usage import TokenUsage

logger = logging.getLogger("neuron.semantic_pass")


def evidence_in_chunk(evidence: str | None, chunk_text: str) -> bool:
    """True when the claimed quote actually appears in the source chunk.

    The extraction schema asks for a verbatim span. Constrained decoding
    still lets a weak model paraphrase; that would silently break provenance
    if we stored it. Empty evidence is treated as missing, not verbatim.
    """
    if not evidence or not evidence.strip():
        return False
    needle = " ".join(evidence.casefold().split())
    haystack = " ".join(chunk_text.casefold().split())
    return bool(needle) and needle in haystack

_SEMANTIC_LABELS = {"Decision", "Term", "System"}
_ENTITY_TYPE_TO_LABEL = {
    "work_item": "WorkItem", "project": "Project", "repository": "Repository",
    "source_file": "SourceFile", "commit": "Commit", "pull_request": "PullRequest",
    "page": "Document", "workspace": "Workspace",
}
# Must match graph.schema.VECTOR_LABELS -- System has no vector index (it's
# usually just a proper noun with little embeddable text), Project isn't a
# content-bearing label either.
_EMBEDDABLE_LABELS = {"WorkItem", "Document", "Decision", "Term", "PullRequest", "Commit", "SourceFile"}


def _embed(
    client: OpenAI, model: str, texts: list[str], token_usage: TokenUsage,
) -> list[list[float]]:
    if not texts:
        return []
    response = client.embeddings.create(
        model=model, input=[vector_store.truncate_for_embedding(t) for t in texts]
    )
    token_usage.add(response.usage)
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
    token_usage: TokenUsage = field(default_factory=TokenUsage)


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



def _drop(reason: str, fact, detail: str | None = None) -> ExtractionDrop:
    """One discarded (or corrected) extraction, with enough of the original
    triple to judge it later without re-reading the source chunk."""
    return ExtractionDrop(
        reason=str(reason),
        subject_kind=fact.subject_kind, subject_name=fact.subject_name,
        relation=fact.relation,
        object_kind=fact.object_kind, object_name=fact.object_name,
        detail=str(detail)[:500] if detail else None,
    )


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
    extraction_model: str,
    profile_name: str,
    token_usage: TokenUsage,
    collection: str = vector_store.COLLECTION,
    axioms: AxiomSet = DEFAULT_AXIOMS,
) -> tuple[int, int, int]:
    source_rows = graph.query(
        "MATCH (sr:SourceRecord {record_key: $record_key}) RETURN sr.source_time LIMIT 1",
        params={"record_key": chunk.record_key},
    ).result_set
    source_time = source_rows[0][0] if source_rows else None
    # An entity with no fact connecting it to anything is graph noise, not
    # knowledge -- a floating "Redis" node nobody can query into is worse
    # than not having it at all. Rather than trust the LLM to always attach
    # a fact to every entity it names (unreliable in practice: real syncs
    # showed System/Term entities extracted with zero accompanying facts),
    # enforce it structurally: only keep an extracted entity if it actually
    # appears as a fact's subject or object. This makes "every semantic node
    # has a reason to exist" a property of the write path, not a prompt hope.
    drops: list[ExtractionDrop] = []
    referenced: set[tuple[str, str]] = set()
    for fact in extraction.facts:
        # `resolve_direction`, not `is_relation_allowed`: a fact that is only
        # valid reversed still names real endpoints, and counting it as
        # unreferenced here would drop those entities before the write loop
        # below ever gets the chance to swap it.
        if axioms.resolve_direction(fact.subject_kind, fact.relation, fact.object_kind) is None:
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
                drops.append(ExtractionDrop(
                    reason=DropReason.ENTITY_NO_CONNECTING_FACT,
                    subject_kind=label, subject_name=item.name,
                ))
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
            vectors = _embed(
                client, embedding_model,
                [_embedding_text(label, item) for item in items], token_usage,
            )
            uids = [
                find_similar_uid(graph, label, vector, collection=collection) or semantic_uid(label, item.name)
                for item, vector in zip(items, vectors)
            ]
        else:
            vectors = [None] * len(items)
            uids = [semantic_uid(label, item.name) for item in items]

        # `search_text` is the text this node is *represented by*, and two
        # separate things depend on it existing:
        #   - the FalkorDB fulltext (BM25) index is built on `search_text`, so
        #     without it a Decision/Term can never match the keyword leg of
        #     hybrid search — only the vector leg (observed: every Decision hit
        #     came back `methods=['vector']`, never 'fulltext').
        #   - `scripts/rebuild_vectors` re-embeds from `search_text`, so
        #     without it the Qdrant projection is NOT rebuildable for exactly
        #     the semantic entities that matter most (observed: a rebuild after
        #     wiping Qdrant reported "Decision: 0 nodes with text").
        # Storing the same string that was embedded keeps both in agreement.
        rows = [
            {
                "uid": uid,
                "props": {
                    **item.model_dump(exclude={"name"}),
                    "name": item.name,
                    "search_text": _embedding_text(label, item),
                },
            }
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
                    "chunk_id": chunk.chunk_id,
                    "chunk_hash": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                    "extractor_version": profile_name,
                    "model": extraction_model,
                    "valid_at": source_time,
                }
                for uid in uids
            ]
            w.upsert_fact_edges(graph, "EXTRACTED_FROM", label, record_own_kind, lineage_rows)
            edges_supported.extend(
                RecordEdgeRef("EXTRACTED_FROM", uid, primary_uid) for uid in uids
            )

        if label in _EMBEDDABLE_LABELS:
            # The name channel gets the entity's bare name; `_embedding_text`
            # already put name + definition/statement into the content one.
            name_vectors = _embed(
                client, embedding_model, [item.name for item in items], token_usage,
            )
            vector_store.upsert_vectors(vector_store.client(), [
                {"uid": uid, "label": label, "embedding": vector,
                 "name_embedding": name_vector, "embedded_text": _embedding_text(label, item)[:400],
                 "embedded_model": embedding_model}
                for uid, item, vector, name_vector in zip(uids, items, vectors, name_vectors)
            ], collection=collection)

    facts_written = 0
    facts_rejected = 0
    for fact in extraction.facts:
        if not evidence_in_chunk(fact.evidence, chunk.text):
            facts_rejected += 1
            logger.info(
                "  rejected fact (evidence not in chunk): (%s) %r -%s-> (%s) %r  evidence=%r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
                fact.evidence,
            )
            drops.append(_drop(DropReason.EVIDENCE_NOT_IN_CHUNK, fact, detail=fact.evidence))
            continue

        direction = axioms.resolve_direction(fact.subject_kind, fact.relation, fact.object_kind)
        if direction is None:
            facts_rejected += 1
            logger.info(
                "  rejected fact (relation not allowed): (%s) %r -%s-> (%s) %r",
                fact.subject_kind, fact.subject_name, fact.relation, fact.object_kind, fact.object_name,
            )
            # Neither direction is in the ontology. The triple is kept here
            # with its evidence rather than written as some vague catch-all
            # relation: an unnamed relation is honest, an invented one is an
            # assertion nobody made.
            #
            # The evidence is the whole point of keeping it. Without the
            # sentence, a reviewer sees `Term GA -APPLIES_TO-> System Nilus`
            # and cannot tell whether the ontology is too narrow or the model
            # was wrong -- which is the one judgement this row exists to
            # support. (This argument was missing at first: 490 rows were
            # stored with no evidence at all.)
            drops.append(_drop(DropReason.RELATION_NOT_ALLOWED, fact, detail=fact.evidence))
            # ...and counted as a gap in the vocabulary, so "the ontology is
            # too narrow" becomes a number someone can act on rather than a
            # suspicion. A term seen once is noise; the same one seen forty
            # times is a missing relation.
            ledger.record_miss(
                "relation_type",
                f"{fact.subject_kind} -{fact.relation}-> {fact.object_kind}",
                example=f"{fact.subject_name} -> {fact.object_name}",
            )
            continue

        # The ontology says this relation runs the other way. Swap the
        # endpoints and say so -- never silently.
        subject_kind, subject_name = fact.subject_kind, fact.subject_name
        object_kind, object_name = fact.object_kind, fact.object_name
        if direction == SWAPPED:
            subject_kind, object_kind = object_kind, subject_kind
            subject_name, object_name = object_name, subject_name
            logger.info(
                "  direction corrected: (%s) %r -%s-> (%s) %r  [as extracted: %s -> %s]",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
                fact.subject_kind, fact.object_kind,
            )
            drops.append(_drop(
                DropReason.DIRECTION_CORRECTED, fact,
                detail=f"written as ({subject_kind}) -{fact.relation}-> ({object_kind})",
            ))

        subject_uid = _resolve_endpoint(
            graph, subject_kind, subject_name,
            primary_uid, record_own_kind, semantic_uids,
        )
        object_uid = _resolve_endpoint(
            graph, object_kind, object_name,
            primary_uid, record_own_kind, semantic_uids,
        )
        if subject_uid is None or object_uid is None:
            facts_rejected += 1
            logger.info(
                "  rejected fact (endpoint unresolved): (%s) %r -%s-> (%s) %r",
                subject_kind, subject_name, fact.relation, object_kind, object_name,
            )
            drops.append(_drop(
                DropReason.ENDPOINT_UNRESOLVED, fact,
                detail=("subject" if subject_uid is None else "") +
                       ("+object" if object_uid is None else ""),
            ))
            continue
        w.upsert_fact_edges(graph, fact.relation, subject_kind, object_kind, [{
            "from_uid": subject_uid, "to_uid": object_uid, "source_record_keys": [chunk.record_key],
            "evidence": fact.evidence, "extraction_method": "llm", "confidence": 0.9,
            "chunk_id": chunk.chunk_id,
            "chunk_hash": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
            "extractor_version": profile_name,
            "model": extraction_model,
            "valid_at": source_time,
            "direction_corrected": direction == SWAPPED,
            # Stamped only when this triple entered the vocabulary through an
            # adoption. It is what makes the batch revertible: `unadopt` drops
            # the axiom rows, then deletes exactly the edges they let in --
            # edges from the seeded vocabulary carry NULL and are never touched.
            "adopted_batch": axioms.adopted_batch_for(subject_kind, fact.relation, object_kind),
        }])
        edges_supported.append(RecordEdgeRef(fact.relation, subject_uid, object_uid))
        facts_written += 1
        logger.info(
            "  wrote fact: (%s) %r -%s-> (%s) %r  evidence=%r",
            subject_kind, subject_name, fact.relation, object_kind, object_name, fact.evidence,
        )

    if edges_supported:
        ledger.record_edges_batch(chunk.record_key, edges_supported)
    # Always written, even when empty: a chunk re-extracted after an ontology
    # change must clear the drops its previous run recorded, or the counts
    # describe a system that no longer exists.
    ledger.record_drops(chunk.record_key, chunk.chunk_id, drops)
    return entities_written, facts_written, facts_rejected


def _call_llm(client: OpenAI, model: str, chunk: PendingChunk):
    """The only part of a chunk's processing that's safe to run concurrently:
    a pure network round-trip with no graph/ledger side effects."""
    profile = profile_for_record_key(chunk.record_key)
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": profile.instructions},
            {"role": "user", "content": chunk.text},
        ],
        text_format=profile.schema,
    )
    return profile, response


def run_semantic_pass(
    graph: Graph,
    ledger: ConnectorLedger,
    *,
    budget: int | None = None,
    client: OpenAI | None = None,
    model: str | None = None,
    record_prefix: str | None = None,
    on_progress: Callable[[int, int, str, SemanticPassResult], None] | None = None,
    max_concurrency: int | None = None,
    collection: str = vector_store.COLLECTION,
) -> SemanticPassResult:
    """Process up to `budget` pending chunks (default: $LLM_BUDGET_PER_RUN).
    A chunk that fails its LLM call is left 'pending' and retried on a later
    run rather than dropped — no retry-count/backoff yet (known v1 gap: a
    persistently failing chunk keeps consuming one budget slot per run).

    LLM calls run concurrently (`LLM_CONCURRENCY`, default 6) -- per-chunk
    latency is 30-50s and almost entirely network wait, so this is the actual
    lever on wall-clock time (a smaller/denser chunk still costs about the
    same latency per call; only the number of *sequential* round-trips does).
    Every write -- the write-time similarity dedup in `_write_extraction`, the
    ledger commit -- stays on the main thread, one chunk at a time, in
    completion order: two chunks racing to decide "is this a new Decision or
    an existing one" concurrently could each conclude "new" and create a
    duplicate, since the first one's node/vector isn't visible to the second
    until it's actually written.
    """
    client = client or OpenAI()
    model = model or os.getenv("LLM_MODEL", "gpt-5.6-luna")
    embedding_model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    budget = budget if budget is not None else int(os.getenv("LLM_BUDGET_PER_RUN", "200"))
    max_concurrency = max_concurrency or int(os.getenv("LLM_CONCURRENCY", "6"))
    # Read the vocabulary once per run, not once per fact: it is per-graph
    # data now (seeded from code on first use), so it can differ between
    # graphs and can be edited without a redeploy.
    axioms = load_axioms(ledger)

    result = SemanticPassResult()
    touched_records: set[str] = set()

    chunks = ledger.pending_chunks(budget, record_prefix=record_prefix)
    total_chunks = len(chunks)

    runnable: list[tuple[PendingChunk, object]] = []
    for chunk in chunks:
        entry = ledger.get(chunk.record_key)
        if entry is None or entry.primary_node_uid is None:
            logger.warning("no primary_node_uid for %s, skipping chunk", chunk.record_key)
            continue
        runnable.append((chunk, entry))

    completed = 0
    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {pool.submit(_call_llm, client, model, chunk): (chunk, entry) for chunk, entry in runnable}
        for future in as_completed(futures):
            chunk, entry = futures[future]
            result.llm_calls += 1
            try:
                profile, response = future.result()
            except Exception:
                logger.exception("LLM extraction failed for chunk %s of %s", chunk.chunk_id, chunk.record_key)
                continue
            result.token_usage.add(response.usage)
            extraction: WorkManagementExtraction = response.output_parsed

            entities, facts, rejected = _write_extraction(
                graph, ledger, chunk, extraction, entry.primary_node_uid,
                _record_own_kind(chunk.record_key), client, embedding_model, model, profile.name,
                result.token_usage, collection=collection, axioms=axioms,
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
            completed += 1
            if on_progress is not None:
                on_progress(completed, total_chunks, chunk.record_key, result)

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
                "MATCH (n {uid: $uid}) RETURN n.search_text, n.name",
                params={"uid": entry.primary_node_uid},
            ).result_set
            search_text = rows[0][0] if rows else None
            node_name = (rows[0][1] if rows else None) or search_text
            if search_text:
                vectors = _embed(
                    client, embedding_model, [search_text, node_name], result.token_usage,
                )
                vector_store.upsert_vectors(vector_store.client(), [{
                    "uid": entry.primary_node_uid, "label": own_label,
                    "embedding": vectors[0], "name_embedding": vectors[1],
                    "embedded_text": search_text[:400], "embedded_model": embedding_model,
                }], collection=collection)

    logger.info(
        "semantic pass done: %d chunks, %d llm calls, %d entities, %d facts (%d rejected), %d records completed",
        result.chunks_processed, result.llm_calls, result.entities_written,
        result.facts_written, result.facts_rejected, result.records_completed,
    )
    return result
