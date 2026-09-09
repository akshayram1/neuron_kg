"""Grounded chat (plan.md §6, Phase 5). Retrieve via hybrid search, gather
each hit's live facts + provenance, then ask an LLM to answer using ONLY that
evidence — with citations back to the actual source records, not invented
ones. This is the "why is this answer true" product value plain vector RAG
doesn't give you (plan.md §6, quoting the old `graph/ask.py`'s own framing).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from falkordb import Graph
from openai import OpenAI

from graph.search import SearchHit, hybrid_search

SYSTEM_PROMPT = """\
You answer questions about a company's Jira/GitHub/Notion knowledge graph
using ONLY the evidence provided below. Every claim you make must be
traceable to one of the given facts — if the evidence doesn't support an
answer, say so plainly instead of guessing or using outside knowledge.

Cite sources inline using the record names given, e.g. "(HERA-101)". Keep
the answer concise — a few sentences, not an essay."""


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


def _live_facts(graph: Graph, uid: str) -> tuple[list[dict], list[str]]:
    """Live (non-superseded) facts touching this node, both directions.
    Two separate queries rather than one with two OPTIONAL MATCHes — avoids
    a cartesian blow-up between independent out/in edge sets on nodes with
    many of both."""
    facts: list[dict] = []
    edge_ids: list[str] = []

    out_rows = graph.query(
        """
        MATCH (n {uid: $uid})-[r]->(other)
        WHERE type(r) <> 'MENTIONED_IN' AND r.invalid_at IS NULL
        RETURN type(r), n.name, other.name, r.evidence, r.source_record_keys, other.uid
        """,
        params={"uid": uid},
    ).result_set
    for rel, subj, obj, evidence, record_keys, other_uid in out_rows:
        facts.append({"subject": subj, "relation": rel, "object": obj, "evidence": evidence, "record_keys": record_keys or []})
        edge_ids.append(f"{uid}:{rel}:{other_uid}")

    in_rows = graph.query(
        """
        MATCH (other)-[r]->(n {uid: $uid})
        WHERE type(r) <> 'MENTIONED_IN' AND r.invalid_at IS NULL
        RETURN type(r), other.name, n.name, r.evidence, r.source_record_keys, other.uid
        """,
        params={"uid": uid},
    ).result_set
    for rel, subj, obj, evidence, record_keys, other_uid in in_rows:
        facts.append({"subject": subj, "relation": rel, "object": obj, "evidence": evidence, "record_keys": record_keys or []})
        edge_ids.append(f"{other_uid}:{rel}:{uid}")

    return facts, edge_ids


def _resolve_records(graph: Graph, record_keys: set[str]) -> dict[str, Citation]:
    if not record_keys:
        return {}
    rows = graph.query(
        "MATCH (s:SourceRecord) WHERE s.record_key IN $keys RETURN s.record_key, s.name, s.url",
        params={"keys": list(record_keys)},
    ).result_set
    return {row[0]: Citation(row[0], row[1], row[2]) for row in rows}


def run_chat_turn(
    graph: Graph, client: OpenAI, question: str, *, model: str = "gpt-5.6-sol",
    search_limit: int = 6, providers: list[str] | None = None,
) -> ChatResult:
    hits: list[SearchHit] = hybrid_search(
        graph, client, question, limit=search_limit, providers=providers,
    )
    if not hits:
        return ChatResult(
            answer="I don't have any information about that in the graph yet.",
            citations=[],
        )

    all_facts: list[dict] = []
    all_edge_ids: list[str] = []
    all_record_keys: set[str] = set()
    highlighted_nodes = [hit.uid for hit in hits]

    context_blocks = []
    for hit in hits:
        facts, edge_ids = _live_facts(graph, hit.uid)
        all_facts.extend(facts)
        all_edge_ids.extend(edge_ids)
        for fact in facts:
            all_record_keys.update(fact["record_keys"])
        fact_lines = "\n".join(
            f"  - {f['subject']} --{f['relation']}--> {f['object']}"
            + (f'  (evidence: "{f["evidence"]}")' if f.get("evidence") else "")
            for f in facts
        ) or "  (no recorded facts)"
        context_blocks.append(f"[{hit.label}] {hit.name}\n{hit.summary}\n{fact_lines}")

    citations_by_key = _resolve_records(graph, all_record_keys)
    context = "\n\n".join(context_blocks)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"EVIDENCE:\n{context}\n\nQUESTION: {question}"},
        ],
    )
    answer = response.choices[0].message.content or ""

    return ChatResult(
        answer=answer,
        citations=list(citations_by_key.values()),
        highlighted_nodes=highlighted_nodes,
        highlighted_edges=all_edge_ids,
    )
