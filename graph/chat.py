"""Grounded chat (plan.md §6, Phase 5). Retrieve via hybrid search, gather
each hit's live facts + provenance, then ask an LLM to answer using ONLY that
evidence — with citations back to the actual source records, not invented
ones. This is the "why is this answer true" product value plain vector RAG
doesn't give you (plan.md §6, quoting the old `graph/ask.py`'s own framing).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from falkordb import Graph
from openai import OpenAI
from pydantic import BaseModel, Field

from graph import vector_store
from graph.access import AccessScope
from graph.entity import fetch_entity_detail
from graph.search import SearchHit, embed_query, hybrid_search
from graph.structured_query import (
    find_named_persons,
    find_window_activity,
    resolve_structured,
)
from graph.time_axis import infer_query_window
from graph.token_usage import TokenUsage

logger = logging.getLogger("neuron.chat")

# One INFO line per retrieval stage. A wrong answer is almost never "the LLM
# hallucinated" -- it is the wrong evidence reaching it, and until these lines
# existed the only way to tell an empty window from an empty graph was to
# re-run the query by hand in a REPL.
_HIT_PREVIEW = 8

SYSTEM_PROMPT = """\
You answer questions about a company's Jira/GitHub/Bitbucket/Notion
knowledge graph using ONLY the evidence provided below. Every claim you make
must be traceable to one of the given facts — if the evidence doesn't
support an answer, say so plainly instead of guessing or using outside
knowledge.

The graph has TWO independent time axes. They are never the same question:
- World time (`at`) — when something was true in the world. "Who was
  assignee in March 2026" is world time.
- Record time (`as_of`) — when we came to believe it. "What did we know
  before the Notion sync" is record time.
The evidence has already been filtered to the clocks named in the header.
State dates in the answer. `undated` means no stated start; `ended, date
unknown` means the text said it ended but not when — that is NOT the same
as still holding.

Facts marked [inferred] were derived (parent lift, shared term, verified
email). Say they are inferred and name the premise. An asserted Jira/GitHub
fact always beats an inferred one if they disagree.

Source authority for implementation state: current Bitbucket/GitHub source
code is authoritative for what production code calls. Jira and Notion may
state a plan or completion claim, but they do not prove the code changed.
When claim time and code-confirmation time differ, report both explicitly and
never backdate the implementation change to the earlier documentation claim.
A commit/merged PR date confirms the code transition; a document date only
confirms when the claim was recorded.

Wisdom and Finding blocks are first-class graph knowledge, not text to infer
again. Their explicit STATUS fields are authoritative: `active` means reviewed
and approved, `proposed` means awaiting review, and `rejected` must not guide
the answer. Use active Wisdom silently as guidance for the answer. Do not
announce that Wisdom was retrieved, its status, internal relationship names
such as DERIVED_FROM, or its Finding provenance unless the user explicitly
asks about wisdom, approval status, findings, provenance, lineage, or evidence.
For an ordinary advice question, give the advice directly and let
`used_sources` drive the separate graph-knowledge/source drawer. If the user
does explicitly ask, report the stored status and provenance accurately.

Cite sources inline using the record names given, e.g. "(HERA-101)".

Format the answer as short markdown: `###` headings per source or topic,
blank lines between sections, and bullet lists. Do not write one unbroken
paragraph. Never emit a bare `#`, `##`, or `###`; every heading must have a
descriptive title.

If the evidence starts with STRUCTURED RESULT, that list is complete for
the question. Do not add tickets or commits that appear only inside
Document or SourceFile text. A WorkItem with no live ASSIGNED_TO is
unassigned; an ended historical ASSIGNED_TO is not a current assignee.
Commit AUTHORED_BY is not a Jira assignment. Text on the commit block
itself (message, Files: paths, dates) is evidence — those paths are the
provider's full change list. `--MODIFIES-->` is only HEAD .py/.md still
on the graph.

The evidence is organized into blocks, each headed "[Label] Name" (e.g.
"[WorkItem] DATAOS-4191 ..."). In `used_sources`, list the exact Name of every
block whose facts you actually drew a claim from — omit a block's name if it
was shown to you but you did not end up using it in the answer. This is what
drives the citation list shown to the user, so it must be precise: naming an
unused block pollutes their sources with irrelevant links."""


class _ChatAnswer(BaseModel):
    answer: str
    used_sources: list[str] = Field(
        description="Exact 'Name' (as given after the [Label] tag) of each evidence "
        "block a claim in the answer actually depended on. Do not include a block "
        "you were shown but didn't use."
    )


@dataclass
class Citation:
    record_key: str
    name: str
    url: str | None


@dataclass
class KnowledgeCitation:
    uid: str
    label: str
    name: str
    status: str | None
    severity: str | None
    created_at: str | None
    stale_at: str | None


@dataclass
class ChatResult:
    answer: str
    citations: list[Citation]
    knowledge_citations: list[KnowledgeCitation] = field(default_factory=list)
    highlighted_nodes: list[str] = field(default_factory=list)
    highlighted_edges: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)


def _log_hits(stage: str, hits: list[SearchHit]) -> None:
    """Names + scores, not just a count: two hits at 0.25 and 0.03 is a very
    different retrieval from two at 0.25 and 0.24."""
    if not hits:
        logger.info("  %-14s none", stage)
        return
    shown = ", ".join(
        f"[{hit.label}] {hit.name[:44]}={hit.score:.4f}"
        f"{'/' + '+'.join(hit.methods) if hit.methods else ''}"
        for hit in hits[:_HIT_PREVIEW]
    )
    more = f" (+{len(hits) - _HIT_PREVIEW} more)" if len(hits) > _HIT_PREVIEW else ""
    logger.info("  %-14s %d: %s%s", stage, len(hits), shown, more)


_WISDOM_INTENT = (
    "wisdom", "lesson", "principle", "policy", "playbook", "heuristic",
    "organizational", "organisation", "best practice", "should we follow",
)
_FINDING_INTENT = (
    "finding", "impact", "risk", "contradiction", "mismatch", "conflict",
    "breaking change", "stale", "blast radius", "affect", "affected",
)


def _requested_knowledge_layers(question: str) -> tuple[bool, bool]:
    lowered = question.lower()
    wisdom = any(term in lowered for term in _WISDOM_INTENT)
    findings = wisdom or any(term in lowered for term in _FINDING_INTENT)
    return wisdom, findings


def _linked_finding_hits(
    graph: Graph, wisdom_hits: list[SearchHit], scope: AccessScope,
    providers: list[str] | None,
) -> list[SearchHit]:
    """Resolve Wisdom provenance deterministically instead of hoping its
    supporting Finding independently survives a global text-search cutoff."""
    if not wisdom_hits:
        return []
    acl, acl_params = scope.cypher("sr", "wisdom_lineage_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    rows = graph.query(
        f"""
        MATCH (wisdom:Wisdom)-[:DERIVED_FROM]->(finding:Finding)
        WHERE wisdom.uid IN $wisdom_uids
        MATCH (finding)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT finding.uid, finding.name, finding.search_text
        """,
        params={
            "wisdom_uids": [hit.uid for hit in wisdom_hits],
            **acl_params, **({"providers": providers} if providers else {}),
        },
    ).result_set
    return [
        SearchHit(row[0], "Finding", row[1] or row[0], row[2] or "", 1.0, ["wisdom-lineage"])
        for row in rows
    ]


def _actionable_wisdom_hits(graph: Graph, hits: list[SearchHit]) -> list[SearchHit]:
    """Rejected/superseded proposals remain auditable but cannot guide chat."""
    if not hits:
        return []
    rows = graph.query(
        "MATCH (wisdom:Wisdom) WHERE wisdom.uid IN $uids "
        "AND wisdom.status IN ['active', 'proposed'] RETURN wisdom.uid",
        params={"uids": [hit.uid for hit in hits]},
    ).result_set
    allowed = {row[0] for row in rows}
    return [hit for hit in hits if hit.uid in allowed]


def _knowledge_metadata(graph: Graph, hit: SearchHit) -> str:
    if hit.label not in {"Wisdom", "Finding"}:
        return ""
    rows = graph.query(
        """
        MATCH (node {uid: $uid})
        RETURN node.status, node.statement, node.rationale,
               node.recommended_action, node.severity, node.created_at,
               node.updated_at, node.stale_at, node.stale_reason
        """,
        params={"uid": hit.uid},
    ).result_set
    if not rows:
        return ""
    status, statement, rationale, action, severity, created_at, updated_at, stale_at, stale_reason = rows[0]
    fields = [f"STATUS: {status or 'unknown'}"]
    if severity:
        fields.append(f"SEVERITY: {severity}")
    if statement:
        fields.append(f"STATEMENT: {statement}")
    if rationale:
        fields.append(f"RATIONALE: {rationale}")
    if action:
        fields.append(f"RECOMMENDED ACTION: {action}")
    if created_at:
        fields.append(f"CREATED AT: {created_at}")
    if updated_at:
        fields.append(f"UPDATED AT: {updated_at}")
    if stale_at:
        fields.append(f"STALE AT: {stale_at}")
    if stale_reason:
        fields.append(f"STALE REASON: {stale_reason}")
    return "\n".join(fields)


def _format_fact(fact: dict) -> str:
    inferred = " [inferred]" if fact.get("derived") else ""
    interval = f"  ({fact['interval']})" if fact.get("interval") else ""
    evidence = f'  (evidence: "{fact["evidence"]}")' if fact.get("evidence") else ""
    return (
        f"  - {fact['source']} --{fact['relation']}--> {fact['target']}"
        f"{inferred}{interval}{evidence}"
    )


def _mentioned_record_keys(
    graph: Graph, uid: str, scope: AccessScope, providers: list[str] | None,
) -> set[str]:
    """SourceRecords this node itself is MENTIONED_IN — not neighbors'.

    Neighborhood fact `recordKeys` follow the edge writer. A parent's
    PARENT_OF edge is often written from the child's Jira record, so
    citing DATAOS-3833's facts pulled DATAOS-3839 / 4191 / 4151 even
    when those tickets were assigned and not part of the answer.
    """
    acl, acl_params = scope.cypher("sr", "mentioned_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    rows = graph.query(
        f"""
        MATCH (n {{uid: $uid}})-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT sr.record_key
        """,
        params={"uid": uid, **acl_params, **({"providers": providers} if providers else {})},
    ).result_set
    return {row[0] for row in rows if row[0]}


def _assignment_facts(facts: list[dict]) -> tuple[list[dict], list[str]]:
    slim = [fact for fact in facts if fact.get("relation") == "ASSIGNED_TO"]
    edge_ids = [
        f"{fact['fromUid']}:{fact['relation']}:{fact['toUid']}"
        for fact in slim if fact.get("fromUid") and fact.get("toUid")
    ]
    return slim, edge_ids


def _entity_evidence(
    graph: Graph, uid: str, scope: AccessScope, providers: list[str] | None,
    *, at: str | None, at_end: str | None, as_of: str | None,
) -> tuple[list[dict], list[str], set[str]]:
    detail = fetch_entity_detail(
        graph, scope, uid, at=at, at_end=at_end, as_of=as_of, providers=providers,
    )
    if detail is None:
        return [], [], set()
    facts = detail["facts"] + detail["past"] + detail["derived"]
    edge_ids = [
        f"{fact['fromUid']}:{fact['relation']}:{fact['toUid']}"
        for fact in facts if fact.get("fromUid") and fact.get("toUid")
    ]
    record_keys: set[str] = set()
    for fact in facts:
        record_keys.update(fact.get("recordKeys") or [])
    return facts, edge_ids, record_keys


def _live_facts(
    graph: Graph, uid: str, scope: AccessScope, providers: list[str] | None,
) -> tuple[list[dict], list[str]]:
    """Live (non-superseded) facts touching this node, both directions.
    Two separate queries rather than one with two OPTIONAL MATCHes — avoids
    a cartesian blow-up between independent out/in edge sets on nodes with
    many of both."""
    facts: list[dict] = []
    edge_ids: list[str] = []

    out_acl, out_params = scope.cypher("sr", "out_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    # NOTE: these are f-strings (the ACL clause is interpolated), so every
    # Cypher map literal must double its braces — `{{uid: $uid}}`. Writing
    # them singly makes Python read `{uid: $uid}` as a replacement field with
    # format spec `$uid` and raise at call time, not at import.
    out_rows = graph.query(
        f"""
        MATCH (n {{uid: $uid}})-[r]->(other)
        WHERE type(r) <> 'MENTIONED_IN' AND r.invalid_at IS NULL
        UNWIND coalesce(r.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {out_acl} {provider_filter}
        RETURN type(r), n.name, other.name, r.evidence,
               collect(DISTINCT sr.record_key), other.uid
        """,
        params={"uid": uid, **out_params, **({"providers": providers} if providers else {})},
    ).result_set
    for rel, subj, obj, evidence, record_keys, other_uid in out_rows:
        facts.append({"subject": subj, "relation": rel, "object": obj, "evidence": evidence, "record_keys": record_keys or []})
        edge_ids.append(f"{uid}:{rel}:{other_uid}")

    in_acl, in_params = scope.cypher("sr", "in_acl")
    in_rows = graph.query(
        f"""
        MATCH (other)-[r]->(n {{uid: $uid}})
        WHERE type(r) <> 'MENTIONED_IN' AND r.invalid_at IS NULL
        UNWIND coalesce(r.source_record_keys, []) AS source_key
        MATCH (sr:SourceRecord {{record_key: source_key}})
        WHERE sr.deleted_at IS NULL AND {in_acl} {provider_filter}
        RETURN type(r), other.name, n.name, r.evidence,
               collect(DISTINCT sr.record_key), other.uid
        """,
        params={"uid": uid, **in_params, **({"providers": providers} if providers else {})},
    ).result_set
    for rel, subj, obj, evidence, record_keys, other_uid in in_rows:
        facts.append({"subject": subj, "relation": rel, "object": obj, "evidence": evidence, "record_keys": record_keys or []})
        edge_ids.append(f"{other_uid}:{rel}:{uid}")

    return facts, edge_ids


def _find_named_person(
    graph: Graph, question: str, scope: AccessScope, providers: list[str] | None,
) -> SearchHit | None:
    """First SAME_AS cluster member. Prefer `find_named_persons`."""
    people = find_named_persons(graph, question, scope, providers)
    return people[0] if people else None


def _used_record_keys(
    used_sources: list[str], record_keys_by_source: dict[str, set[str]],
) -> set[str]:
    """Map the model's reported `used_sources` names back to record keys.

    Both sides are whitespace-normalized before matching: a source's `name`
    can carry incidental leading/trailing whitespace from the original data
    (verified real case: a Jira summary stored as "...for Argus " with a
    trailing space), and a model naturally trims that when echoing the name
    back. An exact-string match against the untouched name then silently
    drops a citation the model explicitly said it used.
    """
    normalized = {name.strip(): keys for name, keys in record_keys_by_source.items()}
    used: set[str] = set()
    for name in used_sources:
        stripped = name.strip()
        if stripped in normalized:
            used.update(normalized[stripped])
            continue
        # Block name is often just "DATAOS-3833"; the model echoes the
        # SourceRecord title "DATAOS-3833 — …". Require a separator so
        # "DATAOS-38" cannot steal "DATAOS-3833".
        for block_name, keys in normalized.items():
            if (
                stripped.startswith(block_name + " ")
                or stripped.startswith(block_name + "—")
                or stripped.startswith(block_name + "–")
                or block_name.startswith(stripped + " ")
                or block_name.startswith(stripped + "—")
                or block_name.startswith(stripped + "–")
            ):
                used.update(keys)
    return used


def _name_was_used(name: str, used_sources: list[str]) -> bool:
    block_name = name.strip()
    for used_name in used_sources:
        stripped = used_name.strip()
        if stripped == block_name:
            return True
        if (
            stripped.startswith(block_name + " ")
            or stripped.startswith(block_name + "—")
            or stripped.startswith(block_name + "–")
            or block_name.startswith(stripped + " ")
            or block_name.startswith(stripped + "—")
            or block_name.startswith(stripped + "–")
        ):
            return True
    return False


def _resolve_knowledge_citations(
    graph: Graph, hits: list[SearchHit], used_sources: list[str],
    *, include_labels: set[str] | None = None,
) -> list[KnowledgeCitation]:
    include_labels = include_labels or set()
    selected = [
        hit for hit in hits
        if hit.label in {"Wisdom", "Finding"}
        and (hit.label in include_labels or _name_was_used(hit.name, used_sources))
    ]
    if not selected:
        return []
    rows = graph.query(
        """
        MATCH (node)
        WHERE node.uid IN $uids
        RETURN node.uid, labels(node)[0], node.name, node.status,
               node.severity, node.created_at, node.stale_at
        """,
        params={"uids": [hit.uid for hit in selected]},
    ).result_set
    by_uid = {row[0]: row for row in rows}
    citations: list[KnowledgeCitation] = []
    for hit in selected:
        row = by_uid.get(hit.uid)
        if not row:
            continue
        citations.append(KnowledgeCitation(
            uid=row[0], label=row[1], name=row[2] or hit.name,
            status=row[3], severity=row[4], created_at=row[5], stale_at=row[6],
        ))
    return citations


def _resolve_records(
    graph: Graph, record_keys: set[str], scope: AccessScope,
) -> dict[str, Citation]:
    if not record_keys:
        return {}
    acl, acl_params = scope.cypher("s", "citation_acl")
    rows = graph.query(
        f"MATCH (s:SourceRecord) WHERE s.record_key IN $keys AND {acl} "
        "RETURN s.record_key, s.name, s.url",
        params={"keys": list(record_keys), **acl_params},
    ).result_set
    return {row[0]: Citation(row[0], row[1], row[2]) for row in rows}


def retrieve(
    graph: Graph, client: OpenAI, question: str, *,
    limit: int, providers: list[str] | None, scope: AccessScope,
    token_usage: TokenUsage | None = None,
    collection: str = vector_store.COLLECTION,
    at: str | None = None, at_end: str | None = None,
) -> tuple[Any, list[SearchHit]]:
    """Everything the product does to turn a question into candidate hits.

    Extracted so the evaluation harness measures THIS and not `hybrid_search`
    alone. It used to call the ranker directly, which is a narrower path than
    the product: three golden cases scored 0 while working perfectly in chat
    ("DS-1237" and "what is DS-1026 about" resolve structurally, "what has
    Darpan Vyas been working on" through the name matcher). An instrument that
    measures less than the product reports losses that are not real, and can
    miss ones that are.

    Returns `(structured_result_or_None, hits)` — the caller needs the first to
    know whether the hit list is already the complete answer.
    """
    structured = resolve_structured(graph, question, scope, providers)
    if structured is not None:
        logger.info("  structured     kind=%s hits=%d", structured.kind, len(structured.hits))
        return structured, structured.hits

    wants_wisdom, wants_findings = _requested_knowledge_layers(question)
    if wants_wisdom or wants_findings:
        # One query embedding feeds every retrieval lane. Wisdom and Findings
        # get reserved slots so a dense graph of ordinary entities cannot
        # crowd them out of the final top-K.
        query_embedding = embed_query(client, question, token_usage=token_usage)
        general_hits = hybrid_search(
            graph, client, question, limit=limit, providers=providers, scope=scope,
            token_usage=token_usage, collection=collection,
            query_embedding=query_embedding,
        )
        wisdom_hits = _actionable_wisdom_hits(
            graph,
            hybrid_search(
                graph, client, question, labels=["Wisdom"], limit=2,
                providers=providers, scope=scope, token_usage=token_usage,
                collection=collection, query_embedding=query_embedding,
            ) if wants_wisdom else [],
        )
        lineage_hits = _linked_finding_hits(graph, wisdom_hits, scope, providers)
        finding_hits = hybrid_search(
            graph, client, question, labels=["Finding"], limit=2,
            providers=providers, scope=scope, token_usage=token_usage,
            collection=collection, query_embedding=query_embedding,
        ) if wants_findings else []
        _log_hits("wisdom-lane", wisdom_hits)
        _log_hits("finding-lineage", lineage_hits)
        _log_hits("finding-lane", finding_hits)

        layer_hits: list[SearchHit] = []
        seen: set[str] = set()
        for hit in wisdom_hits + lineage_hits + finding_hits:
            if hit.uid not in seen:
                layer_hits.append(hit)
                seen.add(hit.uid)
        general_budget = max(2, limit - len(layer_hits))
        hits = layer_hits + [hit for hit in general_hits if hit.uid not in seen][:general_budget]
    else:
        hits = hybrid_search(
            graph, client, question, limit=limit, providers=providers, scope=scope,
            token_usage=token_usage, collection=collection,
        )
    _log_hits("hybrid", hits)

    # Inject the whole SAME_AS cluster, not the first equal-ratio name.
    # Bitbucket Aashish and Jira Aashish score the same; first-wins hid
    # the four live ASSIGNED_TO edges on the Jira node.
    seen = {hit.uid for hit in hits}
    extras = [p for p in find_named_persons(graph, question, scope, providers) if p.uid not in seen]
    if extras:
        _log_hits("named-person", extras)
        hits = extras + hits

    # A window is a filter the text index cannot express: an August commit
    # is not lexically closer to "what was worked on in August" than a July
    # one. Ask the graph for the window directly, or the dated question is
    # answered from whatever happened to rank well.
    if at and at_end:
        seen = {hit.uid for hit in hits}
        in_window = [
            w for w in find_window_activity(
                graph, scope, providers, at=at, at_end=at_end, limit=limit,
            )
            if w.uid not in seen
        ]
        if in_window:
            _log_hits("time-window", in_window)
            hits = in_window + hits
    return None, hits


def run_chat_turn(
    graph: Graph, client: OpenAI, question: str, *, model: str | None = None,
    search_limit: int = 6, providers: list[str] | None = None,
    scope: AccessScope, at: str | None = None, as_of: str | None = None,
    at_end: str | None = None, collection: str = vector_store.COLLECTION,
) -> ChatResult:
    # Chat uses sol by default for stronger evidence reconciliation. Keep a
    # dedicated CHAT_MODEL override so deployments may tune chat separately
    # from semantic extraction and wisdom aggregation.
    model = model or os.getenv("CHAT_MODEL", "gpt-5.6-sol")
    token_usage = TokenUsage()
    started = time.monotonic()
    logger.info(
        "chat q=%r model=%s collection=%s limit=%d providers=%s",
        question[:160], model, collection, search_limit, providers or "all",
    )
    inferred_at, inferred_at_end, inferred_as_of = infer_query_window(question)
    # Only inherit the inferred end when the caller did not pin `at` itself:
    # a caller-supplied instant must not silently acquire a month's width.
    if at is None:
        at, at_end = inferred_at, at_end or inferred_at_end
    at = at or inferred_at
    as_of = as_of or inferred_as_of
    structured, hits = retrieve(
        graph, client, question, limit=search_limit, providers=providers, scope=scope,
        token_usage=token_usage, collection=collection, at=at, at_end=at_end,
    )

    if not hits and structured is None:
        return ChatResult(
            answer="I don't have any information about that in the graph yet.",
            citations=[],
            token_usage=token_usage,
        )

    all_facts: list[dict] = []
    all_edge_ids: list[str] = []
    # Record keys grouped per evidence block, keyed by a whitespace-normalized
    # name so the model's citation ("used_sources") reliably matches even when
    # the underlying node name carries incidental whitespace -- verified on
    # real data: a Jira summary stored with a trailing space ("...for Argus ")
    # made the model's (correctly trimmed) echo of that name silently fail an
    # exact-string lookup, dropping a citation the model had explicitly named.
    # This is ONLY for matching the model's response back to a block; the
    # `Citation.name` shown to the user still comes from `_resolve_records`,
    # which reads the untouched `SourceRecord.name` from the graph.
    record_keys_by_source: dict[str, set[str]] = {}
    highlighted_nodes = [hit.uid for hit in hits]

    context_blocks = []
    for hit in hits:
        facts, edge_ids, record_keys = _entity_evidence(
            graph, hit.uid, scope, providers, at=at, at_end=at_end, as_of=as_of,
        )
        if structured is not None and structured.kind in {"unassigned", "assigned_to"}:
            facts, edge_ids = _assignment_facts(facts)
            record_keys = _mentioned_record_keys(graph, hit.uid, scope, providers)
        elif structured is not None:
            record_keys = _mentioned_record_keys(graph, hit.uid, scope, providers)
        all_facts.extend(facts)
        all_edge_ids.extend(edge_ids)
        source_keys = record_keys_by_source.setdefault(hit.name.strip(), set())
        source_keys.update(record_keys)
        fact_lines = "\n".join(_format_fact(fact) for fact in facts) or "  (no recorded facts)"
        metadata = _knowledge_metadata(graph, hit)
        metadata_section = f"\n{metadata}" if metadata else ""
        block = f"[{hit.label}] {hit.name}\n{hit.summary}{metadata_section}\n{fact_lines}"
        context_blocks.append(block)
        # Per block, because one block routinely dominates: a merge commit
        # touching 400 files contributed 46% of a 54k-token context while
        # twelve blocks looked evenly sized from the outside.
        logger.info(
            "  evidence       [%s] %s facts=%d records=%d chars=%d",
            hit.label, hit.name[:48], len(facts), len(record_keys), len(block),
        )

    clock = []
    if at and at_end:
        # Name the interval, not just its start: the model previously read a
        # month's opening instant as the whole question and answered "nothing
        # had happened yet", which is true and useless.
        clock.append(f"world time window [{at}, {at_end})")
    elif at:
        clock.append(f"world time at={at}")
    if as_of:
        clock.append(f"record time as_of={as_of}")
    header = ("CLOCKS: " + "; ".join(clock) + "\n\n") if clock else ""
    if structured is not None:
        header += structured.preamble + "\n\n"
    context = header + "\n\n".join(context_blocks)
    # Chars, not an estimated token count: chars/4 read 40k against a real
    # 54,835 here. The exact figure arrives on the `answer` line a few
    # seconds later, so a wrong guess would only be something to unlearn.
    logger.info(
        "  context        blocks=%d facts=%d chars=%d",
        len(context_blocks), len(all_facts), len(context),
    )

    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"EVIDENCE:\n{context}\n\nQUESTION: {question}"},
        ],
        text_format=_ChatAnswer,
    )
    token_usage.add(response.usage)
    parsed = response.output_parsed

    if structured is not None:
        # The structured list is the answer. Citing via used_sources would
        # still be wrong if a block's neighborhood keys leaked assigned
        # children (DATAOS-3833 PARENT_OF → 3839 / 4151). Own records only.
        used_record_keys = set()
        for keys in record_keys_by_source.values():
            used_record_keys.update(keys)
    else:
        used_record_keys = _used_record_keys(parsed.used_sources, record_keys_by_source)
    citations_by_key = _resolve_records(graph, used_record_keys, scope)
    wants_wisdom, wants_findings = _requested_knowledge_layers(question)
    include_labels = set()
    if wants_wisdom:
        include_labels.add("Wisdom")
    if wants_findings:
        include_labels.add("Finding")
    knowledge_citations = _resolve_knowledge_citations(
        graph, hits, parsed.used_sources, include_labels=include_labels,
    )
    logger.info(
        "  answer         cited_blocks=%d citations=%d answer_chars=%d "
        "tokens_in=%d tokens_out=%d %.2fs",
        len(parsed.used_sources), len(citations_by_key), len(parsed.answer),
        token_usage.input_tokens, token_usage.output_tokens,
        time.monotonic() - started,
    )
    if parsed.used_sources:
        logger.info("  cited          %s", ", ".join(parsed.used_sources)[:400])

    return ChatResult(
        answer=parsed.answer,
        citations=list(citations_by_key.values()),
        knowledge_citations=knowledge_citations,
        highlighted_nodes=highlighted_nodes,
        highlighted_edges=all_edge_ids,
        token_usage=token_usage,
    )
