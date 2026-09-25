"""Grounded chat (plan.md §6, Phase 5). Retrieve via hybrid search, gather
each hit's live facts + provenance, then ask an LLM to answer using ONLY that
evidence — with citations back to the actual source records, not invented
ones. This is the "why is this answer true" product value plain vector RAG
doesn't give you (plan.md §6, quoting the old `graph/ask.py`'s own framing).
"""

from __future__ import annotations

import importlib.util
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from falkordb import Graph
from openai import OpenAI
from pydantic import BaseModel, Field

# Load the repository .env before any retrieval settings are read. Chat is
# imported by several entry points, so relying on each caller's import order
# made the web app silently keep the default ``off`` mode while the benchmark
# (which exported its environment explicitly) used Laya.
from util import paths as _paths  # noqa: F401

from graph import vector_store
from graph.access import AccessScope
from graph.bridge.anchors import commit_shas, jira_keys, pull_request_refs, repository_names
from graph.entity import fetch_entity_detail
from graph.expand import MAX_SEEDS, TIER_ORDER, expand_neighbors, tier_for_extraction_method
from graph.rerank import (
    LayaReranker,
    RerankCandidate,
    RerankDecision,
    Reranker,
    RetrievalRole,
    assign_roles,
)
from graph.search import SearchHit, embed_query, hybrid_search
from graph.structured_query import (
    find_named_persons,
    find_window_activity,
    resolve_structured,
)
from graph.text_window import best_window
from graph.time_axis import infer_query_window
from graph.token_usage import TokenUsage

logger = logging.getLogger("neuron.chat")

# One INFO line per retrieval stage. A wrong answer is almost never "the LLM
# hallucinated" -- it is the wrong evidence reaching it, and until these lines
# existed the only way to tell an empty window from an empty graph was to
# re-run the query by hand in a REPL.
_HIT_PREVIEW = 8

# plan.md §1.1/§1.2/§1.3 knobs. Every one is read once at import time (same
# convention as `graph/search.py`'s RRF_K/VECTOR_LEG_WEIGHT) and overridable
# via env var for tuning without a code change.
#
# Wide candidate pool (§1.1): `retrieve`'s no-knowledge-layer branch fetches
# this many hits so expansion (§1.3) has real seeds and the harness can
# measure candidate recall@40 -- the OLD `search_limit` cut still happens,
# just later (see `run_chat_turn`), so default chat behaviour is unchanged
# until a caller asks for more than `search_limit` or NEURON_RERANK exists
# (plan.md §1.1: "the final cut is candidates[:search_limit] as today").
POOL_SIZE = int(os.getenv("NEURON_POOL_SIZE", "40"))
# Per-block context window (§1.2): `best_window` is asked for at most this
# many tokens of a hit's summary.
NEURON_BLOCK_TOKENS = int(os.getenv("NEURON_BLOCK_TOKENS", "500"))
# Facts shown per block (§1.2), asserted before derived.
NEURON_FACTS_PER_BLOCK = int(os.getenv("NEURON_FACTS_PER_BLOCK", "25"))
# Total evidence budget (§1.2) -- real token count via the same cl100k_base
# encoder `graph/text_window.py` uses (`graph.vector_store._encoding`), not
# chars/4. Reserves room for the system prompt, the question, and the
# answer itself; see NEURON_ANSWER_RESERVE_TOKENS.
NEURON_CONTEXT_TOKENS = int(os.getenv("NEURON_CONTEXT_TOKENS", "10000"))
# Headroom left in NEURON_CONTEXT_TOKENS for the model's own answer. Not
# named in the plan text verbatim, but the plan explicitly requires "leaving
# explicit room for ... answer" -- this is that room, made an explicit,
# tunable constant rather than an unstated fudge factor.
NEURON_ANSWER_RESERVE_TOKENS = int(os.getenv("NEURON_ANSWER_RESERVE_TOKENS", "2000"))
# Below this many tokens, shrinking a block's text window further stops
# being useful evidence -- squeeze facts instead (see `_pack_context`).
_MIN_WINDOW_TOKENS = 40

# Laya retrieval policy. Thresholds are intentionally configurable and must
# be tuned on the grouped dev split; these defaults match the conservative
# starting point used by the sibling Laya graph experiment. Bridge candidates
# never reach the answer model unless a later hop is independently accepted.
NEURON_RERANK_MIN_P = float(os.getenv("NEURON_RERANK_MIN_P", "0.50"))
NEURON_RERANK_BRIDGE_MIN_P = float(os.getenv("NEURON_RERANK_BRIDGE_MIN_P", "0.20"))
NEURON_RERANK_BRIDGE_LIMIT = int(os.getenv("NEURON_RERANK_BRIDGE_LIMIT", "4"))
NEURON_RERANK_MIN_DIRECT = int(os.getenv("NEURON_RERANK_MIN_DIRECT", "2"))
NEURON_RERANK_MAX_KEEP = int(os.getenv("NEURON_RERANK_MAX_KEEP", "12"))
NEURON_RERANK_MAX_ROUNDS = int(os.getenv("NEURON_RERANK_MAX_ROUNDS", "2"))
NEURON_RERANK_FACTS = int(os.getenv("NEURON_RERANK_FACTS", "6"))

_laya_reranker: LayaReranker | None = None
_laya_reranker_config: tuple[str | None, str] | None = None
_reranker_enabled_override: bool | None = None

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

A block whose header carries "GRAPH EXPANSION TIER" was not directly matched
by search — it was pulled in because it is graph-adjacent to a matched node.
`primary` is as trustworthy as directly matched evidence; `secondary` came
from an LLM extraction; `derived` and `unknown` are weaker and should be
treated with more caution, especially if they conflict with a block that has
no such tier line (which was matched directly).

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
class PackedBlockInfo:
    """Per-block packing honesty (plan.md §1.2). Always populated -- there is
    no established "eval mode" signal into `run_chat_turn` today (see the
    module docstring note above `_pack_context`), so rather than gate this
    behind a new parameter, it always rides on `ChatResult` where
    `scripts/evaluate_retrieval.py` (or any other caller) can read it without
    the harness needing a new argument threaded through.

    `gold_support_preserved` is NOT computed against any golden/expected-uid
    data -- `run_chat_turn` has no access to a question's gold chain. It is a
    conservative proxy: True only when nothing about this block's evidence
    was cut (neither the summary text nor its facts). A caller that DOES have
    gold data (the eval harness) can combine this with its own gold_uids to
    get a true "was the gold support preserved" signal; this field alone only
    promises "this block's evidence reached the prompt intact or it didn't."
    """

    uid: str
    label: str
    name: str
    window_tokens: int
    packed_tokens: int
    truncated: bool
    gold_support_preserved: bool
    facts_included: int
    facts_dropped: int


@dataclass
class ChatResult:
    answer: str
    citations: list[Citation]
    knowledge_citations: list[KnowledgeCitation] = field(default_factory=list)
    highlighted_nodes: list[str] = field(default_factory=list)
    highlighted_edges: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    packed_blocks: list[PackedBlockInfo] = field(default_factory=list)
    dropped_evidence_uids: list[str] = field(default_factory=list)
    retrieval_trace: RetrievalTrace | None = None


@dataclass
class RetrievalTrace:
    """Stage boundary data for evaluation and retrieval diagnostics."""

    initial_candidate_uids: list[str] = field(default_factory=list)
    expanded_candidate_uids: list[str] = field(default_factory=list)
    bridge_uids: list[str] = field(default_factory=list)
    final_uids: list[str] = field(default_factory=list)
    expansion_rounds: int = 0
    reranker: str | None = None
    fallback: bool = False
    fallback_reason: str | None = None


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


def _count_tokens(text: str, encoder=None) -> int:
    """Real token count, `graph/text_window.py`'s cl100k_base convention
    (`graph.vector_store._encoding`) -- not chars/4. There is no registered
    tiktoken encoding for this repo's own chat model ids (`gpt-5.6-sol` /
    `gpt-5.6-luna` are not real OpenAI models), so this is the same stand-in
    `best_window` itself already uses as "the answer model's tokenizer"."""
    if not text:
        return 0
    encoder = encoder or vector_store._encoding
    return len(encoder.encode(text))


def _cap_facts(facts: list[dict], max_facts: int) -> tuple[list[dict], int]:
    """Asserted facts before derived (plan.md §1.2), capped to `max_facts`.
    Same `derived` bool convention `graph/expand.py` uses per fact/edge."""
    asserted = [fact for fact in facts if not fact.get("derived")]
    derived = [fact for fact in facts if fact.get("derived")]
    ordered = asserted + derived
    capped = ordered[:max_facts]
    return capped, len(ordered) - len(capped)


def _fact_edge_ids(facts: list[dict]) -> list[str]:
    return [
        f"{fact['fromUid']}:{fact['relation']}:{fact['toUid']}"
        for fact in facts if fact.get("fromUid") and fact.get("toUid")
    ]


def _fact_record_keys(facts: list[dict]) -> set[str]:
    keys: set[str] = set()
    for fact in facts:
        keys.update(fact.get("recordKeys") or [])
    return keys


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


# --------------------------------------------------------------------------
# 1.7 — two-entity lane
# --------------------------------------------------------------------------

# File-path anchor, alongside `graph/bridge/anchors.py`'s jira_keys/
# commit_shas/pull_request_refs/repository_names regexes (that module has no
# file-path pattern of its own). Extension allowlist rather than a bare
# "contains a dot" match -- a bare dot-match also fires on "e.g.", "v1.0",
# etc. Matches `SourceFile.name`, which the Bitbucket/GitHub pipelines set
# to the file's repo-relative path verbatim (`graph/bitbucket_pipeline.py`,
# `graph/github_pipeline.py`: `Name: {file.path}`).
_FILE_PATH_RE = re.compile(
    r"\b[\w][\w./-]*\.(?:py|js|jsx|ts|tsx|go|rs|java|kt|rb|php|c|cpp|h|hpp|cs|"
    r"md|mdx|json|ya?ml|sql|sh|css|scss|html|txt|toml|ini|cfg)\b",
    re.IGNORECASE,
)


def _file_path_candidates(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0) for match in _FILE_PATH_RE.finditer(text)))


def _pair_anchor_candidates(
    graph: Graph, question: str, scope: AccessScope, providers: list[str] | None,
) -> list[tuple[str, str, str]]:
    """Resolve named anchors in `question` to real graph node uids, for the
    two-entity lane (plan.md §1.7: "two ticket keys, a key and a person, a
    key and a file path"). Read-time resolution of the identical identifier
    shapes `graph/resolver.py`'s write-time `_targets` links exact-anchor
    edges from -- reuses the same `graph/bridge/anchors.py` regexes rather
    than inventing new detection.

    Returns (uid, label, name) tuples, ordered by anchor kind (Jira key >
    commit sha > PR ref > repository name > file path > fuzzy person name,
    the last being the least precise) and deduplicated by uid. The caller
    uses the first two distinct anchors found.
    """
    keys = sorted(jira_keys(question))
    shas = sorted(commit_shas(question))
    refs = sorted(pull_request_refs(question))
    names = sorted(repository_names(question))
    paths = _file_path_candidates(question)

    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    # Deferred until at least one text-level anchor is actually present, so
    # a question with none of these shapes (the common case) never touches
    # the graph at all here -- only the `find_named_persons` fallback below
    # does, and only that scope/provider path needs to be live in that case.
    # `acl`/`provider_filter`/`base_params`/`_add` are only ever referenced
    # below inside an `if keys/shas/refs/names/paths:` block, each of which
    # can only be true when this same guard was also true.
    if keys or shas or refs or names or paths:
        acl, acl_params = scope.cypher("sr", "pair_anchor_acl")
        provider_filter = "AND sr.provider IN $providers" if providers else ""
        base_params = {**acl_params, **({"providers": providers} if providers else {})}

        def _add(rows: list, label: str) -> None:
            for uid, name in rows:
                if uid in seen:
                    continue
                seen.add(uid)
                found.append((uid, label, name or uid))

    if keys:
        rows = graph.query(
            f"""MATCH (n:WorkItem)-[:MENTIONED_IN]->(sr:SourceRecord)
                WHERE n.issue_key IN $keys AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                RETURN DISTINCT n.uid, n.name""",
            params={**base_params, "keys": keys},
        ).result_set
        _add(rows, "WorkItem")

    if shas:
        rows = graph.query(
            f"""MATCH (n:Commit)-[:MENTIONED_IN]->(sr:SourceRecord)
                WHERE any(v IN $shas WHERE toLower(n.sha) STARTS WITH v)
                  AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                RETURN DISTINCT n.uid, n.name""",
            params={**base_params, "shas": shas},
        ).result_set
        _add(rows, "Commit")

    if refs:
        qualified = [ref for ref in refs if not ref.startswith("#")]
        bare = [ref for ref in refs if ref.startswith("#")]
        if qualified:
            rows = graph.query(
                f"""MATCH (n:PullRequest)-[:MENTIONED_IN]->(sr:SourceRecord)
                    WHERE toLower(n.pr_ref) IN $refs AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                    RETURN DISTINCT n.uid, n.name""",
                params={**base_params, "refs": qualified},
            ).result_set
            _add(rows, "PullRequest")
        if bare:
            rows = graph.query(
                f"""MATCH (n:PullRequest)-[:MENTIONED_IN]->(sr:SourceRecord)
                    WHERE any(v IN $refs WHERE toLower(n.pr_ref) ENDS WITH v)
                      AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                    RETURN DISTINCT n.uid, n.name""",
                params={**base_params, "refs": bare},
            ).result_set
            _add(rows, "PullRequest")

    if names:
        rows = graph.query(
            f"""MATCH (n:Repository)-[:MENTIONED_IN]->(sr:SourceRecord)
                WHERE toLower(n.name) IN $names AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                RETURN DISTINCT n.uid, n.name""",
            params={**base_params, "names": names},
        ).result_set
        _add(rows, "Repository")

    if paths:
        lowered = [path.lower() for path in paths]
        rows = graph.query(
            f"""MATCH (n:SourceFile)-[:MENTIONED_IN]->(sr:SourceRecord)
                WHERE any(v IN $paths WHERE toLower(n.name) = v OR toLower(n.name) ENDS WITH '/' + v)
                  AND sr.deleted_at IS NULL AND {acl} {provider_filter}
                RETURN DISTINCT n.uid, n.name""",
            params={**base_params, "paths": lowered},
        ).result_set
        _add(rows, "SourceFile")

    if len(found) < 2:
        for hit in find_named_persons(graph, question, scope, providers):
            if hit.uid in seen:
                continue
            seen.add(hit.uid)
            found.append((hit.uid, "Person", hit.name))

    return found


def _two_entity_lane(
    graph: Graph, question: str, scope: AccessScope, providers: list[str] | None,
) -> list[SearchHit]:
    """plan.md §1.7: when the question names two resolvable anchors, the
    claims *about the pair* are what both nodes are jointly `MENTIONED_IN`,
    plus any direct edge between them.

    `SourceRecord` nodes carry no raw content and no `uid` (they're keyed by
    `record_key` -- see `graph/writer.py`'s `upsert_source_records`), so they
    cannot themselves become uid-keyed `SearchHit` candidates the rest of
    this module's block-building pipeline (`_entity_evidence`,
    `_mentioned_record_keys`) expects. Candidates are therefore the two
    anchor NODES, tagged `methods=["pair"]`: building their evidence blocks
    via `_entity_evidence` already surfaces any direct edge between them (it
    walks every live in/out edge of the node), and each block's own summary
    is prefixed with the names of the SourceRecords that mention both, as
    the closest available stand-in for "the claims about the pair" text.
    """
    anchors = _pair_anchor_candidates(graph, question, scope, providers)
    if len(anchors) < 2:
        return []
    (uid_a, label_a, name_a), (uid_b, label_b, name_b) = anchors[0], anchors[1]

    acl, acl_params = scope.cypher("sr", "pair_lane_acl")
    provider_filter = "AND sr.provider IN $providers" if providers else ""
    shared_rows = graph.query(
        f"""
        MATCH (a {{uid: $a}})-[:MENTIONED_IN]->(sr:SourceRecord)<-[:MENTIONED_IN]-(b {{uid: $b}})
        WHERE sr.deleted_at IS NULL AND {acl} {provider_filter}
        RETURN DISTINCT sr.name
        """,
        params={"a": uid_a, "b": uid_b, **acl_params, **({"providers": providers} if providers else {})},
    ).result_set
    shared_names = sorted({row[0] for row in shared_rows if row[0]})
    shared_note = "; ".join(shared_names) if shared_names else "(no shared SourceRecord)"

    text_rows = graph.query(
        "MATCH (n) WHERE n.uid IN $uids RETURN n.uid, n.search_text",
        params={"uids": [uid_a, uid_b]},
    ).result_set
    text_by_uid = {row[0]: (row[1] or "") for row in text_rows}

    def _summary(own_uid: str, other_label: str, other_name: str) -> str:
        note = f"PAIR EVIDENCE: co-mentioned with [{other_label}] {other_name} in: {shared_note}"
        body = text_by_uid.get(own_uid, "")
        return f"{note}\n\n{body}" if body else note

    return [
        SearchHit(uid_a, label_a, name_a, _summary(uid_a, label_b, name_b), 0.0, ["pair"]),
        SearchHit(uid_b, label_b, name_b, _summary(uid_b, label_a, name_a), 0.0, ["pair"]),
    ]


# --------------------------------------------------------------------------
# 1.2 — context windowing and budget
# --------------------------------------------------------------------------

@dataclass
class _PackedContext:
    blocks: list[str]
    all_facts: list[dict]
    all_edge_ids: list[str]
    record_keys_by_source: dict[str, set[str]]
    packed_blocks: list[PackedBlockInfo]
    dropped_uids: list[str]


def _render_block(hit: SearchHit, text: str, metadata_section: str, facts: list[dict]) -> str:
    fact_lines = "\n".join(_format_fact(fact) for fact in facts) or "  (no recorded facts)"
    return f"[{hit.label}] {hit.name}\n{text}{metadata_section}\n{fact_lines}"


def _expansion_tier(graph: Graph, hit: SearchHit) -> str | None:
    """Best-effort read-time authority tier (plan.md §1.4) for a hit that
    came from `graph/expand.py`'s one-hop expansion, so the answer model
    sees how trustworthy that evidence is -- not just that it exists.

    `expand_neighbors`'s `SearchHit` carries `methods=["graph:<REL>"]` but
    not which seed or edge produced it (that association is not part of its
    public return shape, and `graph/expand.py` is not this task's file to
    change), so this is an approximation: the best (highest) tier among ANY
    live edge of that relation type touching the node, not a replay of
    `expand_neighbors`'s exact per-seed match. It is built entirely from
    `graph/expand.py`'s own public `tier_for_extraction_method` lookup and
    `TIER_ORDER`, not a re-implementation of tier semantics.
    """
    graph_methods = [method for method in hit.methods if method.startswith("graph:")]
    if not graph_methods:
        return None
    rel = graph_methods[0].split(":", 1)[1]
    rows = graph.query(
        "MATCH (n {uid: $uid})-[r]-(m) WHERE type(r) = $rel AND r.invalid_at IS NULL "
        "RETURN r.extraction_method",
        params={"uid": hit.uid, "rel": rel},
    ).result_set
    tiers = [tier_for_extraction_method(row[0]) for row in rows]
    if not tiers:
        return None
    return max(tiers, key=TIER_ORDER.index)


def _pack_context(
    graph: Graph,
    hits: list[SearchHit],
    question: str,
    *,
    structured: Any,
    scope: AccessScope,
    providers: list[str] | None,
    at: str | None,
    at_end: str | None,
    as_of: str | None,
    evidence_budget: int,
    encoder=None,
) -> _PackedContext:
    """Build evidence blocks within `evidence_budget` real tokens (plan.md
    §1.2), packing spans/facts rather than accepting or dropping only whole
    blocks.

    For each hit, in the given (already-priority-ordered) order: fetch facts,
    cap them (asserted before derived) at `NEURON_FACTS_PER_BLOCK`, and window
    the summary text to `NEURON_BLOCK_TOKENS` via `best_window`. If the
    resulting block does not fit in what is left of `evidence_budget`, the
    text is windowed down further (still via `best_window`, now against the
    remaining space) before any fact is dropped for space; if even a minimal
    window plus zero facts does not fit, the whole block is dropped and
    logged -- this is the last resort, not the first one.

    Deterministic: a fixed input order and a single greedy left-to-right pass
    with no randomness or unordered-collection iteration.
    """
    encoder = encoder or vector_store._encoding
    remaining = max(0, evidence_budget)
    blocks: list[str] = []
    all_facts: list[dict] = []
    all_edge_ids: list[str] = []
    record_keys_by_source: dict[str, set[str]] = {}
    packed_blocks: list[PackedBlockInfo] = []
    dropped_uids: list[str] = []

    for hit in hits:
        facts, _edge_ids, record_keys = _entity_evidence(
            graph, hit.uid, scope, providers, at=at, at_end=at_end, as_of=as_of,
        )
        if structured is not None and structured.kind in {"unassigned", "assigned_to"}:
            facts, _edge_ids = _assignment_facts(facts)
            record_keys = _mentioned_record_keys(graph, hit.uid, scope, providers)
        elif structured is not None:
            record_keys = _mentioned_record_keys(graph, hit.uid, scope, providers)

        facts_capped, facts_dropped = _cap_facts(facts, NEURON_FACTS_PER_BLOCK)
        # `record_keys` for the structured branches is deterministic
        # (`_mentioned_record_keys`, unaffected by fact capping); for the
        # ordinary path it is recomputed from whatever facts actually end up
        # packed (below, after any further budget-driven squeeze) so a
        # citation can never point at a fact that was cut for space.
        structured_record_keys = record_keys if structured is not None else None

        full_tokens = _count_tokens(hit.summary, encoder)
        window_text = best_window(question, hit.summary, NEURON_BLOCK_TOKENS, encoder)
        window_tokens = _count_tokens(window_text, encoder)

        metadata = _knowledge_metadata(graph, hit)
        tier = _expansion_tier(graph, hit)
        if tier:
            metadata = f"{metadata}\nGRAPH EXPANSION TIER: {tier}" if metadata else f"GRAPH EXPANSION TIER: {tier}"
        metadata_section = f"\n{metadata}" if metadata else ""

        packed_text = window_text
        packed_facts = facts_capped
        block_text = _render_block(hit, packed_text, metadata_section, packed_facts)
        block_tokens = _count_tokens(block_text, encoder)
        squeezed = False

        if block_tokens > remaining:
            # Fixed overhead (header + metadata + current facts) is roughly
            # constant regardless of how much of the summary text survives,
            # so back it out to size the text-only squeeze.
            overhead = block_tokens - window_tokens
            available_for_text = remaining - overhead
            fit = False
            if available_for_text >= _MIN_WINDOW_TOKENS:
                candidate_text = best_window(question, window_text, available_for_text, encoder)
                candidate_block = _render_block(hit, candidate_text, metadata_section, packed_facts)
                candidate_tokens = _count_tokens(candidate_block, encoder)
                if candidate_tokens <= remaining:
                    packed_text, block_text, block_tokens = candidate_text, candidate_block, candidate_tokens
                    squeezed = True
                    fit = True

            if not fit:
                # Text alone can't be squeezed enough (or is already at the
                # floor) -- drop the lowest-value tail facts next. Facts are
                # already asserted-before-derived, so trimming from the end
                # drops derived facts first.
                trimmed = list(packed_facts)
                while trimmed:
                    trimmed = trimmed[:-1]
                    candidate_block = _render_block(hit, packed_text, metadata_section, trimmed)
                    candidate_tokens = _count_tokens(candidate_block, encoder)
                    if candidate_tokens <= remaining:
                        facts_dropped += len(packed_facts) - len(trimmed)
                        packed_facts, block_text, block_tokens = trimmed, candidate_block, candidate_tokens
                        squeezed = True
                        fit = True
                        break

            if not fit:
                dropped_uids.append(hit.uid)
                logger.info(
                    "  evidence       [%s] %s DROPPED (evidence budget exhausted, remaining=%d tokens)",
                    hit.label, hit.name[:48], remaining,
                )
                continue

        remaining -= block_tokens
        blocks.append(block_text)
        all_facts.extend(packed_facts)
        all_edge_ids.extend(_fact_edge_ids(packed_facts))
        source_keys = record_keys_by_source.setdefault(hit.name.strip(), set())
        source_keys.update(
            structured_record_keys if structured_record_keys is not None else _fact_record_keys(packed_facts)
        )

        truncated = squeezed or (window_tokens < full_tokens) or facts_dropped > 0
        info = PackedBlockInfo(
            uid=hit.uid, label=hit.label, name=hit.name,
            window_tokens=window_tokens, packed_tokens=_count_tokens(packed_text, encoder),
            truncated=truncated, gold_support_preserved=not truncated,
            facts_included=len(packed_facts), facts_dropped=facts_dropped,
        )
        packed_blocks.append(info)
        # Per block, because one block routinely dominates: a merge commit
        # touching 400 files contributed 46% of a 54k-token context while
        # twelve blocks looked evenly sized from the outside. window_tokens/
        # packed_tokens/truncated/gold_support_preserved are always attached
        # (see PackedBlockInfo's docstring for why -- no eval-mode signal
        # exists to gate on, and these are cheap to compute).
        logger.info(
            "  evidence       [%s] %s facts=%d records=%d chars=%d "
            "window_tokens=%d packed_tokens=%d truncated=%s gold_support_preserved=%s",
            hit.label, hit.name[:48], len(packed_facts), len(source_keys), len(block_text),
            info.window_tokens, info.packed_tokens, truncated, info.gold_support_preserved,
        )

    return _PackedContext(blocks, all_facts, all_edge_ids, record_keys_by_source, packed_blocks, dropped_uids)


def _reranker_mode() -> str:
    if _reranker_enabled_override is not None:
        return "laya" if _reranker_enabled_override else "off"
    return os.getenv("NEURON_RERANK", "off").strip().lower()


def set_reranker_enabled(enabled: bool | None) -> dict[str, Any]:
    """Set the process-wide runtime switch; ``None`` restores the env value."""
    global _reranker_enabled_override, _laya_reranker, _laya_reranker_config
    _reranker_enabled_override = enabled
    if enabled is False:
        # Drop Neuron's reference so an idle disabled worker need not retain
        # the model. An in-flight request keeps its own scorer reference.
        _laya_reranker = None
        _laya_reranker_config = None
    return reranker_status()


def reranker_status() -> dict[str, Any]:
    """Report configuration readiness without loading the 421M model."""
    mode = _reranker_mode()
    model_dir_value = os.getenv("LAYA_MODEL_DIR")
    model_dir = Path(model_dir_value).expanduser() if model_dir_value else None
    required_files = ("model.safetensors", "questions.json", "rl_agent_config.json")
    package_available = importlib.util.find_spec("laya") is not None
    checkpoint_ready = bool(
        model_dir and model_dir.is_dir()
        and all((model_dir / name).is_file() for name in required_files)
    )
    enabled = mode == "laya"
    if mode in {"", "off", "none"}:
        reason = "disabled"
    elif mode != "laya":
        reason = f"unsupported mode: {mode}"
    elif not package_available:
        reason = "laya package is unavailable"
    elif not checkpoint_ready:
        reason = "checkpoint is missing required files"
    else:
        reason = None
    return {
        "mode": mode or "off",
        "enabled": enabled,
        "available": package_available and checkpoint_ready,
        "ready": enabled and reason is None,
        "source": "runtime" if _reranker_enabled_override is not None else "environment",
        "modelDir": str(model_dir) if model_dir else None,
        "device": os.getenv("LAYA_DEVICE", "cpu"),
        "packageAvailable": package_available,
        "checkpointReady": checkpoint_ready,
        "reason": reason,
    }


def _configured_reranker() -> Reranker | None:
    """Return the scorer selected by the current runtime environment."""
    global _laya_reranker, _laya_reranker_config
    mode = _reranker_mode()
    if mode in {"", "off", "none"}:
        return None
    if mode != "laya":
        raise ValueError(f"unsupported NEURON_RERANK={mode!r}; expected off or laya")
    config = (os.getenv("LAYA_MODEL_DIR"), os.getenv("LAYA_DEVICE", "cpu"))
    if _laya_reranker is None or _laya_reranker_config != config:
        _laya_reranker = LayaReranker(model_dir=config[0], device=config[1])
        _laya_reranker_config = config
    return _laya_reranker


def _rerank_candidates(
    graph: Graph,
    question: str,
    hits: list[SearchHit],
    *,
    scope: AccessScope,
    providers: list[str] | None,
    at: str | None,
    at_end: str | None,
    as_of: str | None,
) -> list[RerankCandidate]:
    """Serialize node text plus visible linked/temporal facts for Laya.

    Scoring only ``SearchHit.summary`` would hide the exact information the
    second pass is meant to exploit: a weak textual hit can still carry the
    edge or historical interval leading to the answer. The same ACL-aware
    entity reader used by context packing supplies a small fact preview here.
    The final text is frozen to the Laya candidate token budget.
    """
    candidates: list[RerankCandidate] = []
    for hit in hits:
        facts, _edge_ids, _record_keys = _entity_evidence(
            graph, hit.uid, scope, providers, at=at, at_end=at_end, as_of=as_of,
        )
        fact_lines = [_format_fact(fact) for fact in facts[:NEURON_RERANK_FACTS]]
        parts = [f"[{hit.label}] {hit.name}", hit.summary]
        if fact_lines:
            parts.append("LINKED AND TEMPORAL FACTS:\n" + "\n".join(fact_lines))
        serialized = "\n".join(part for part in parts if part)
        candidates.append(RerankCandidate(
            uid=hit.uid,
            # `best_window` has no default `encoder` -- match the same
            # `vector_store._encoding` convention every other call site in
            # this file uses (see `_count_tokens`, `_pack_context`).
            window=best_window(question, serialized, 300, vector_store._encoding),
            label=hit.label,
            name=hit.name,
            methods=list(hit.methods),
        ))
    return candidates


def _score_roles(
    reranker: Reranker,
    graph: Graph,
    question: str,
    hits: list[SearchHit],
    *,
    scope: AccessScope,
    providers: list[str] | None,
    at: str | None,
    at_end: str | None,
    as_of: str | None,
) -> list[RerankDecision]:
    candidates = _rerank_candidates(
        graph, question, hits, scope=scope, providers=providers,
        at=at, at_end=at_end, as_of=as_of,
    )
    scores = reranker.score(question, candidates)
    decisions = assign_roles(
        scores, candidates,
        direct_threshold=NEURON_RERANK_MIN_P,
        bridge_threshold=NEURON_RERANK_BRIDGE_MIN_P,
        bridge_limit=NEURON_RERANK_BRIDGE_LIMIT,
    )
    role_counts = {
        role.value: sum(decision.role == role for decision in decisions)
        for role in RetrievalRole
    }
    logger.info("  laya roles     %s", role_counts)
    return decisions


def _merge_unique_hits(*groups: list[SearchHit]) -> list[SearchHit]:
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for group in groups:
        for hit in group:
            if hit.uid in seen:
                continue
            seen.add(hit.uid)
            merged.append(hit)
    return merged


def _laya_selected_hits(
    hits: list[SearchHit], decisions: list[RerankDecision], *, max_keep: int,
) -> list[SearchHit]:
    """Return answer evidence only; bridge candidates never leak to chat."""
    by_uid = {hit.uid: hit for hit in hits}
    accepted = [
        decision for decision in decisions
        if decision.role in {RetrievalRole.DIRECT_EVIDENCE, RetrievalRole.TEMPORAL_CONTEXT}
    ]

    def priority(decision: RerankDecision) -> tuple[int, float, str]:
        hit = by_uid[decision.uid]
        reserved = bool(set(hit.methods) & {"named_entity", "pair", "time_window"})
        return (0 if reserved else 1, -decision.score, decision.uid)

    selected: list[SearchHit] = []
    for decision in sorted(accepted, key=priority)[:max_keep]:
        hit = by_uid.get(decision.uid)
        if hit is None:
            continue
        selected.append(SearchHit(
            uid=hit.uid, label=hit.label, name=hit.name, summary=hit.summary,
            score=decision.score,
            methods=list(dict.fromkeys(hit.methods + [f"laya:{decision.role.value}"])),
        ))
    return selected


def _laya_two_pass_search(
    reranker: Reranker,
    graph: Graph,
    question: str,
    hits: list[SearchHit],
    *,
    scope: AccessScope,
    providers: list[str] | None,
    at: str | None,
    at_end: str | None,
    as_of: str | None,
    exclude_edges: frozenset[tuple[str, str, str]],
    trace: RetrievalTrace | None = None,
) -> list[SearchHit]:
    """Score broadly, then expand rejected bridges only when evidence is thin."""
    all_hits = _merge_unique_hits(hits)
    if trace is not None:
        trace.initial_candidate_uids = [hit.uid for hit in all_hits]
        trace.reranker = "laya"
    decisions = _score_roles(
        reranker, graph, question, all_hits, scope=scope, providers=providers,
        at=at, at_end=at_end, as_of=as_of,
    )
    decision_by_uid = {decision.uid: decision for decision in decisions}
    frontier = decisions

    def accepted_count() -> int:
        return sum(
            decision.role in {RetrievalRole.DIRECT_EVIDENCE, RetrievalRole.TEMPORAL_CONTEXT}
            for decision in decision_by_uid.values()
        )

    rounds = 0
    while accepted_count() < NEURON_RERANK_MIN_DIRECT and rounds < NEURON_RERANK_MAX_ROUNDS:
        seed_uids = [
            decision.uid for decision in frontier
            if decision.role in {
                RetrievalRole.DIRECT_EVIDENCE,
                RetrievalRole.TEMPORAL_CONTEXT,
                RetrievalRole.BRIDGE_CANDIDATE,
            }
        ][:MAX_SEEDS]
        if not seed_uids:
            break
        neighbor_hits = expand_neighbors(
            graph, seed_uids, scope, providers, min_tier="derived",
            exclude_edges=exclude_edges,
        )
        known = {hit.uid for hit in all_hits}
        new_neighbors = [hit for hit in neighbor_hits if hit.uid not in known]
        if not new_neighbors:
            break
        rounds += 1
        _log_hits(f"laya-hop-{rounds}", new_neighbors)
        new_decisions = _score_roles(
            reranker, graph, question, new_neighbors, scope=scope, providers=providers,
            at=at, at_end=at_end, as_of=as_of,
        )
        all_hits.extend(new_neighbors)
        if trace is not None:
            trace.expanded_candidate_uids.extend(hit.uid for hit in new_neighbors)
        for decision in new_decisions:
            decision_by_uid[decision.uid] = decision
        frontier = new_decisions

    selected = _laya_selected_hits(
        all_hits, list(decision_by_uid.values()), max_keep=NEURON_RERANK_MAX_KEEP,
    )
    if trace is not None:
        trace.bridge_uids = [
            decision.uid for decision in decision_by_uid.values()
            if decision.role == RetrievalRole.BRIDGE_CANDIDATE
        ]
        trace.final_uids = [hit.uid for hit in selected]
        trace.expansion_rounds = rounds
    logger.info(
        "  laya search    pool=%d rounds=%d accepted=%d final=%d",
        len(hits), rounds, accepted_count(), len(selected),
    )
    return selected


def retrieve(
    graph: Graph, client: OpenAI, question: str, *,
    limit: int, providers: list[str] | None, scope: AccessScope,
    token_usage: TokenUsage | None = None,
    collection: str = vector_store.COLLECTION,
    at: str | None = None, at_end: str | None = None,
    exclude_edges: frozenset[tuple[str, str, str]] = frozenset(),
    as_of: str | None = None,
    reranker: Reranker | None = None,
    trace: RetrievalTrace | None = None,
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
    know whether the hit list is already the complete answer. When the second
    element is a candidate pool rather than a complete structured answer
    (i.e. the first element is `None`), it is NOT yet cut to `limit` (plan.md
    §1.1) — the caller (`run_chat_turn`) applies `candidates[:search_limit]`
    itself, after this function's own pool-widening (§1.1) and one-hop
    expansion (§1.3) have both had a chance to add candidates. This mirrors
    plan.md §1.1's own phrasing verbatim: "the final cut is
    candidates[:search_limit] as today, so behaviour does not change unless
    NEURON_RERANK is set."
    """
    structured = resolve_structured(graph, question, scope, providers)
    if structured is not None:
        logger.info("  structured     kind=%s hits=%d", structured.kind, len(structured.hits))
        if trace is not None:
            trace.reranker = "structured"
            trace.initial_candidate_uids = [hit.uid for hit in structured.hits]
            trace.final_uids = [hit.uid for hit in structured.hits]
        return structured, structured.hits

    active_reranker = reranker or _configured_reranker()

    wants_wisdom, wants_findings = _requested_knowledge_layers(question)
    if active_reranker is not None:
        # With a relevance gate, every generally useful lane can contribute
        # without crowding a fixed top-k. One embedding is reused by all
        # semantic lanes; deterministic pair/person/time lanes join below.
        query_embedding = embed_query(client, question, token_usage=token_usage)
        general_hits = hybrid_search(
            graph, client, question, limit=POOL_SIZE, providers=providers, scope=scope,
            token_usage=token_usage, collection=collection,
            query_embedding=query_embedding,
        )
        wisdom_hits = _actionable_wisdom_hits(
            graph,
            hybrid_search(
                graph, client, question, labels=["Wisdom"], limit=5,
                providers=providers, scope=scope, token_usage=token_usage,
                collection=collection, query_embedding=query_embedding,
            ),
        )
        lineage_hits = _linked_finding_hits(graph, wisdom_hits, scope, providers)
        finding_hits = hybrid_search(
            graph, client, question, labels=["Finding"], limit=5,
            providers=providers, scope=scope, token_usage=token_usage,
            collection=collection, query_embedding=query_embedding,
        )
        hits = _merge_unique_hits(wisdom_hits, lineage_hits, finding_hits, general_hits)
        if wants_wisdom or wants_findings:
            requested_wisdom = wisdom_hits[:2] if wants_wisdom else []
            requested_lineage = _linked_finding_hits(
                graph, requested_wisdom, scope, providers,
            )
            requested_findings = finding_hits[:2] if wants_findings else []
            requested_layers = _merge_unique_hits(
                requested_wisdom, requested_lineage, requested_findings,
            )
            requested_uids = {hit.uid for hit in requested_layers}
            general_budget = max(2, limit - len(requested_layers))
            fallback_hits = requested_layers + [
                hit for hit in general_hits if hit.uid not in requested_uids
            ][:general_budget]
        else:
            fallback_hits = general_hits
        _log_hits("wisdom-lane", wisdom_hits)
        _log_hits("finding-lineage", lineage_hits)
        _log_hits("finding-lane", finding_hits)
    elif wants_wisdom or wants_findings:
        # One query embedding feeds every retrieval lane. Wisdom and Findings
        # get reserved slots so a dense graph of ordinary entities cannot
        # crowd them out of the final top-K. NOT widened to POOL_SIZE (§1.1
        # only names "the else branch, no knowledge-layer intent") -- the
        # reserved-slot budget above is already sized against `limit`.
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
        # plan.md §1.1: widen the candidate POOL, not the final answer -- the
        # caller still cuts to `limit`/`search_limit` after expansion below.
        # `search.py` needs no change; `hybrid_search` already returns
        # `ranked[:limit]`, so asking for POOL_SIZE just asks it for more.
        hits = hybrid_search(
            graph, client, question, limit=POOL_SIZE, providers=providers, scope=scope,
            token_usage=token_usage, collection=collection,
        )
    _log_hits("hybrid", hits)
    if trace is not None:
        trace.initial_candidate_uids = [hit.uid for hit in hits]

    if active_reranker is not None:
        # Run every deterministic lane that is applicable to this query before
        # Laya. No model-based tool router can accidentally suppress an exact
        # person/pair/time signal; Laya only decides which semantic evidence
        # reaches the answer and which candidates may seed a bounded next hop.
        pair_hits = _two_entity_lane(graph, question, scope, providers)
        named_hits = find_named_persons(graph, question, scope, providers)
        window_hits = (
            find_window_activity(graph, scope, providers, at=at, at_end=at_end, limit=limit)
            if at and at_end else []
        )
        _log_hits("pair-lane", pair_hits)
        _log_hits("named-person", named_hits)
        _log_hits("time-window", window_hits)
        hits = _merge_unique_hits(pair_hits, named_hits, window_hits, hits)
        try:
            return None, _laya_two_pass_search(
                active_reranker, graph, question, hits,
                scope=scope, providers=providers, at=at, at_end=at_end,
                as_of=as_of, exclude_edges=exclude_edges, trace=trace,
            )
        except Exception as exc:
            # A model/package/checkpoint failure must not take chat down. The
            # original RRF + bounded-expansion path below remains the fallback.
            logger.exception("  laya fallback  scoring failed; using RRF order")
            if trace is not None:
                trace.fallback = True
                trace.fallback_reason = f"{type(exc).__name__}: {exc}"
                trace.reranker = "rrf"
            hits = fallback_hits

    pool_before_expansion = len(hits)

    # plan.md §1.7: two-entity lane. Alongside the wide-pool/expansion logic
    # (prepended, same pattern as the named-person `extras` injection below)
    # so a pair anchor can also seed expansion just below.
    seen = {hit.uid for hit in hits}
    pair_hits = [hit for hit in _two_entity_lane(graph, question, scope, providers) if hit.uid not in seen]
    if pair_hits:
        _log_hits("pair-lane", pair_hits)
        hits = pair_hits + hits

    # plan.md §1.3/§1.4: one-hop expansion around the pool's own seeds.
    # `min_tier`: "primary for lookup questions (structured-ish, named key),
    # derived otherwise." `structured` is always `None` at this point in the
    # function -- a non-`None` `resolve_structured` result already returned
    # above, before any of this runs -- so the "primary for lookup" branch
    # can never actually trigger in this control flow today. Kept as an
    # explicit conditional (not a bare "derived" constant) so it activates
    # correctly if a future non-structured lookup signal is ever added,
    # rather than silently staying "derived" forever with no visible reason
    # why. Flagged in the task report as a QUERY for the repo owner to
    # confirm this reading is intended.
    min_tier = "primary" if structured is not None else "derived"
    seed_uids = [hit.uid for hit in hits][:MAX_SEEDS]
    neighbor_hits = expand_neighbors(
        graph, seed_uids, scope, providers, min_tier=min_tier, exclude_edges=exclude_edges,
    )
    seen = {hit.uid for hit in hits}
    new_neighbors = [hit for hit in neighbor_hits if hit.uid not in seen]
    if new_neighbors:
        _log_hits("expansion", new_neighbors)
        hits = hits + new_neighbors
        if trace is not None:
            trace.expanded_candidate_uids.extend(hit.uid for hit in new_neighbors)
    if len(hits) != pool_before_expansion:
        # Router honesty (DICE): a wrong answer is almost never "the LLM
        # hallucinated" -- log when the pool actually changed shape, not just
        # that expansion ran.
        logger.info(
            "  pool           %d -> %d candidates after pair-lane/expansion",
            pool_before_expansion, len(hits),
        )

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
    if trace is not None:
        trace.reranker = trace.reranker or "rrf"
        trace.final_uids = [hit.uid for hit in hits]
    return None, hits


def run_chat_turn(
    graph: Graph, client: OpenAI, question: str, *, model: str | None = None,
    search_limit: int = 6, providers: list[str] | None = None,
    scope: AccessScope, at: str | None = None, as_of: str | None = None,
    at_end: str | None = None, collection: str = vector_store.COLLECTION,
    exclude_edges: frozenset[tuple[str, str, str]] = frozenset(),
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
    retrieval_trace = RetrievalTrace()
    structured, hits = retrieve(
        graph, client, question, limit=search_limit, providers=providers, scope=scope,
        token_usage=token_usage, collection=collection, at=at, at_end=at_end,
        as_of=as_of, exclude_edges=exclude_edges, trace=retrieval_trace,
    )

    # plan.md §1.1's pool-then-cut boundary: `retrieve`'s non-structured path
    # now returns a WIDE pool (POOL_SIZE, plus §1.3 expansion candidates) so
    # expansion has real seeds and the harness can measure candidate
    # recall@40. The final cut to the caller's own `search_limit` -- "the
    # final cut is candidates[:search_limit] as today" -- happens HERE,
    # after expansion has already had its chance to add candidates, not
    # inside `retrieve` itself. A structured result is already the complete,
    # non-sampled answer ("STRUCTURED RESULT — complete list, not a sample")
    # and must never be cut.
    pool_size = len(hits)
    laya_selected = any(
        method.startswith("laya:") for hit in hits for method in hit.methods
    )
    if structured is None and not laya_selected:
        hits = hits[:search_limit]
        if pool_size > len(hits):
            logger.info(
                "  cut            pool=%d -> search_limit=%d", pool_size, len(hits),
            )
    retrieval_trace.final_uids = [hit.uid for hit in hits]

    if not hits and structured is None:
        return ChatResult(
            answer="I don't have any information about that in the graph yet.",
            citations=[],
            token_usage=token_usage,
            retrieval_trace=retrieval_trace,
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

    # plan.md §1.2: real token budget, not chars/4. Reserves room for the
    # system prompt, the question itself, and the header above the evidence
    # budget so `NEURON_CONTEXT_TOKENS` bounds the WHOLE prompt, not just the
    # evidence blocks in isolation.
    reserved_tokens = (
        _count_tokens(SYSTEM_PROMPT) + _count_tokens(question)
        + _count_tokens(header) + NEURON_ANSWER_RESERVE_TOKENS
    )
    evidence_budget = max(0, NEURON_CONTEXT_TOKENS - reserved_tokens)
    packed = _pack_context(
        graph, hits, question, structured=structured, scope=scope, providers=providers,
        at=at, at_end=at_end, as_of=as_of, evidence_budget=evidence_budget,
    )
    context = header + "\n\n".join(packed.blocks)
    # Chars, not an estimated token count: chars/4 read 40k against a real
    # 54,835 here. The exact figure arrives on the `answer` line a few
    # seconds later, so a wrong guess would only be something to unlearn.
    # Kept as-is (plan.md §1.2 says so explicitly) alongside a real count --
    # the packing decisions above already used real tokens throughout, this
    # line is only a post-hoc sanity check against the budget.
    logger.info(
        "  context        blocks=%d facts=%d chars=%d",
        len(packed.blocks), len(packed.all_facts), len(context),
    )
    logger.info(
        "  packing        evidence_budget=%d context_tokens=%d blocks_kept=%d blocks_dropped=%d",
        evidence_budget, _count_tokens(context), len(packed.blocks), len(packed.dropped_uids),
    )

    # Highlighted nodes now reflect what actually survived packing, not the
    # full pre-packing candidate list -- once budget-driven drops exist, a
    # dropped candidate's evidence never reached the model, so it should not
    # be reported as part of the answer's evidence either. (Matches
    # `scripts/evaluate_retrieval.py`'s own `stage_case_metrics` docstring,
    # which already anticipated this: "today packed_uids equals the chat
    # turn's highlighted_nodes. It will start differing once budget-driven
    # drops exist.")
    highlighted_nodes = [info.uid for info in packed.packed_blocks]

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
        for keys in packed.record_keys_by_source.values():
            used_record_keys.update(keys)
    else:
        used_record_keys = _used_record_keys(parsed.used_sources, packed.record_keys_by_source)
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
        highlighted_edges=packed.all_edge_ids,
        token_usage=token_usage,
        packed_blocks=packed.packed_blocks,
        dropped_evidence_uids=packed.dropped_uids,
        retrieval_trace=retrieval_trace,
    )
