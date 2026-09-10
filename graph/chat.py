"""Grounded chat (plan.md §6, Phase 5). Retrieve via hybrid search, gather
each hit's live facts + provenance, then ask an LLM to answer using ONLY that
evidence — with citations back to the actual source records, not invented
ones. This is the "why is this answer true" product value plain vector RAG
doesn't give you (plan.md §6, quoting the old `graph/ask.py`'s own framing).
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field

from falkordb import Graph
from openai import OpenAI
from pydantic import BaseModel, Field

from graph.search import SearchHit, hybrid_search
from graph.access import AccessScope
from graph.entity import fetch_entity_detail
from graph.time_axis import infer_query_clocks
from graph.token_usage import TokenUsage

_WORD_RE = re.compile(r"[A-Za-z]+")

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

Cite sources inline using the record names given, e.g. "(HERA-101)".

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
class ChatResult:
    answer: str
    citations: list[Citation]
    highlighted_nodes: list[str] = field(default_factory=list)
    highlighted_edges: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)


def _format_fact(fact: dict) -> str:
    inferred = " [inferred]" if fact.get("derived") else ""
    interval = f"  ({fact['interval']})" if fact.get("interval") else ""
    evidence = f'  (evidence: "{fact["evidence"]}")' if fact.get("evidence") else ""
    return (
        f"  - {fact['source']} --{fact['relation']}--> {fact['target']}"
        f"{inferred}{interval}{evidence}"
    )


def _entity_evidence(
    graph: Graph, uid: str, scope: AccessScope, providers: list[str] | None,
    *, at: str | None, as_of: str | None,
) -> tuple[list[dict], list[str], set[str]]:
    detail = fetch_entity_detail(graph, scope, uid, at=at, as_of=as_of, providers=providers)
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
    """Fuzzy-match a Person the question is asking about.

    Why this exists: hybrid_search's top-K is tuned to find the single best
    matching fact, not to enumerate everything about a named entity. Verified
    on real data: "what did Soumadip do in argus" only surfaced 2 of the 29
    WorkItems he's ASSIGNED_TO/REPORTED_BY on, because the other 27 simply
    didn't score in the top 8 -- they're not more/less relevant, there just
    isn't room. A person question needs "all of this entity's edges", which
    is a graph traversal, not a similarity ranking.

    Matching is fuzzy, not exact-substring: verified case had the question
    spell the name "Soumyadip" against the real "Soumadip De" -- a one-letter
    typo an exact/substring match would miss entirely.
    """
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    acl, acl_params = scope.cypher("sr", "person_name_acl")
    rows = graph.query(
        f"""
        MATCH (p:Person)-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT p.uid, p.name
        """,
        params={**acl_params, **({"providers": providers} if providers else {})},
    ).result_set
    if not rows:
        return None

    query_words = [w.lower() for w in _WORD_RE.findall(question) if len(w) >= 3]
    if not query_words:
        return None

    best: tuple[float, str, str] | None = None  # (ratio, uid, name)
    for uid, name in rows:
        for part in _WORD_RE.findall(name or ""):
            if len(part) < 3:
                continue
            part_lower = part.lower()
            for word in query_words:
                ratio = difflib.SequenceMatcher(None, part_lower, word).ratio()
                if ratio >= 0.82 and (best is None or ratio > best[0]):
                    best = (ratio, uid, name)

    if best is None:
        return None
    _, uid, name = best
    return SearchHit(uid=uid, label="Person", name=name, summary="", score=0.0, methods=["named_entity"])


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
        used.update(normalized.get(name.strip(), set()))
    return used


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


def run_chat_turn(
    graph: Graph, client: OpenAI, question: str, *, model: str | None = None,
    search_limit: int = 6, providers: list[str] | None = None,
    scope: AccessScope, at: str | None = None, as_of: str | None = None,
) -> ChatResult:
    # Deliberately NOT the same model/env var as semantic_pass's extraction
    # call. Extraction needs gpt-5.6-sol's reliability at filling typed
    # Pydantic fields; this call only summarizes evidence that's already
    # been extracted into prose, where that reliability buys nothing.
    # Measured against a real question: sol took 40.5s (and 2.2s on a
    # different run -- its latency is inconsistent), luna took 3.9s with
    # equal or better answer quality (it included a limit value sol's
    # answer omitted). Defaulting to luna here, not to $LLM_MODEL.
    model = model or os.getenv("CHAT_MODEL", "gpt-5.6-luna")
    token_usage = TokenUsage()
    inferred_at, inferred_as_of = infer_query_clocks(question)
    at = at or inferred_at
    as_of = as_of or inferred_as_of
    hits: list[SearchHit] = hybrid_search(
        graph, client, question, limit=search_limit, providers=providers, scope=scope,
        token_usage=token_usage,
    )

    # A person the question names by name gets ALL their live facts, not just
    # whatever made hybrid_search's top-K -- see `_find_named_person`'s
    # docstring for the real case this fixes.
    named_person = _find_named_person(graph, question, scope, providers)
    if named_person is not None and not any(hit.uid == named_person.uid for hit in hits):
        hits = [named_person, *hits]

    if not hits:
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
            graph, hit.uid, scope, providers, at=at, as_of=as_of,
        )
        all_facts.extend(facts)
        all_edge_ids.extend(edge_ids)
        source_keys = record_keys_by_source.setdefault(hit.name.strip(), set())
        source_keys.update(record_keys)
        fact_lines = "\n".join(_format_fact(fact) for fact in facts) or "  (no recorded facts)"
        context_blocks.append(f"[{hit.label}] {hit.name}\n{hit.summary}\n{fact_lines}")

    clock = []
    if at:
        clock.append(f"world time at={at}")
    if as_of:
        clock.append(f"record time as_of={as_of}")
    header = ("CLOCKS: " + "; ".join(clock) + "\n\n") if clock else ""
    context = header + "\n\n".join(context_blocks)

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

    used_record_keys = _used_record_keys(parsed.used_sources, record_keys_by_source)
    citations_by_key = _resolve_records(graph, used_record_keys, scope)

    return ChatResult(
        answer=parsed.answer,
        citations=list(citations_by_key.values()),
        highlighted_nodes=highlighted_nodes,
        highlighted_edges=all_edge_ids,
        token_usage=token_usage,
    )
