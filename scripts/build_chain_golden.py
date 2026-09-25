"""Chain-generated golden set — 25-plan.md, Phase 0, "Chain-generated category"
(§0.1 "Chain-generated golden set" / §0.3 as the caller's brief names it).

Example:
    uv run python -m scripts.build_chain_golden --graph story-20260917-050343-5d26 --sample 10
    uv run python -m scripts.build_chain_golden --graph story-20260917-050343-5d26 --full

What this does, in order (plan.md steps 1-7):
  1. Sample 2-3 hop chains from ASSERTED edges only (`r.derived = false`,
     `r.invalid_at IS NULL`) along relations that carry meaning, one chain per
     start node per relation pattern (`sample_chains`).
  2. Ask the cheap/"luna"-tier chat model for two question variants per chain
     -- "indirect" (never names any chain node) and "anchored" (a realistic,
     user-style phrasing that still avoids literal identifiers) -- with a
     single LLM call per chain (`generate_variants`).
  3. Reject a variant if any chain node's `name`/`issue_key`/`path`/`sha`/
     `pr_ref` literally appears in its text (`leakage_free`).
  4. `--sample N` for a small run, `--full` for everything the graph offers.
  5. Write `eval/chain_golden.jsonl`.
  6. Row shape: id, question, answer_uid, chain_uids, chain_relations, hops,
     generator, plus `split` (dev/test) and `variant` (which of the two
     generated questions was kept) -- extra fields beyond the plan's example
     row are tolerated the same way `scripts/evaluate_retrieval.py` tolerates
     them on read.
  7. Split dev/test by a project/workspace grouping key so near-duplicate
     chain nodes from the same project never straddle the boundary
     (`assign_splits`).

Node-label/relation names below are verified against the live schema
(`graph/schema.py` ENTITY_LABELS, `graph/axioms.py` _STRUCTURAL, `graph/
ontology.py` RELATION_TYPE_MAP) rather than assumed from the plan's prose
examples -- see the module docstring notes on each `ChainPattern` for where a
plan example had zero live matches and what was substituted, and this run's
final report's QUERIES section for the human-facing version of the same
notes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from falkordb import Graph
from openai import OpenAI
from pydantic import BaseModel

from graph import multigraph
from graph.falkor_client import get_graph
from util import paths as _paths  # noqa: F401 -- load repo .env
from util.paths import DATA_DIR

logger = logging.getLogger("neuron.build_chain_golden")

# Hub labels excluded everywhere a chain node could land (start, middle, or
# answer) -- plan.md: "Exclude Repository, Project, Workspace, SourceRecord
# as intermediate (hub) nodes." A hub is just as uninformative as a start or
# an answer, so the patterns below simply never mention these labels.
HUB_LABELS = {"Repository", "Project", "Workspace", "SourceRecord"}

# Node properties the deterministic leakage filter checks verbatim against
# generated question text (plan.md step 3).
LEAK_FIELDS = ("name", "issue_key", "path", "sha", "pr_ref")
MIN_LEAK_FIELD_LEN = 3  # skip near-empty values that would false-positive on common words
SHA_PREFIX_LEN = 8

DEV_FRACTION = 0.7


# --------------------------------------------------------------- chain shapes


@dataclass(frozen=True)
class ChainPattern:
    """One traversal shape. `cypher` returns one row per match: `node_count`
    ordered `uid` columns (n0..nk, walked start-to-end) -- `relations` names
    the edge type of each hop in that same order, so `len(relations) ==
    node_count - 1`."""

    pattern_id: str
    cypher: str
    relations: tuple[str, ...]
    node_count: int


def _asserted(*aliases: str) -> str:
    """`r.derived = false AND r.invalid_at IS NULL` for every given edge alias
    -- plan.md step 1: "asserted edges only"."""
    return " AND ".join(f"{a}.derived = false AND {a}.invalid_at IS NULL" for a in aliases)


# Verified against the live graph (`neuron__story-20260917-050343-5d26`, the
# only populated graph in this FalkorDB whose labels/relations match this
# project's schema -- see this run's QUERIES note). Each pattern is annotated
# with which plan.md example it realizes, or what it substitutes and why.
PATTERNS: list[ChainPattern] = [
    # plan.md example 1, verbatim (3-hop): live match count 17.
    # Document -DOCUMENTS-> WorkItem <-IMPLEMENTS- Commit -MODIFIES-> SourceFile
    ChainPattern(
        "doc_wi_commit_sf",
        """
        MATCH (n0:Document)-[r1:DOCUMENTS]->(n1:WorkItem)<-[r2:IMPLEMENTS]-(n2:Commit)-[r3:MODIFIES]->(n3:SourceFile)
        WHERE %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid, n3.uid
        """ % _asserted("r1", "r2", "r3"),
        ("DOCUMENTS", "IMPLEMENTS", "MODIFIES"),
        4,
    ),
    # plan.md example 3, verbatim (2-hop): live match count 12.
    # Decision -APPLIES_TO-> System <-APPLIES_TO- Decision
    ChainPattern(
        "decision_system_decision",
        """
        MATCH (n0:Decision)-[r1:APPLIES_TO]->(n1:System)<-[r2:APPLIES_TO]-(n2:Decision)
        WHERE n0.uid <> n2.uid AND %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("APPLIES_TO", "APPLIES_TO"),
        3,
    ),
    # plan.md example 2, verbatim (2-hop): live match count 0 -- the live
    # graph's only PARENT_OF edges are Document->Document (Notion page
    # hierarchy), never WorkItem->WorkItem. Kept in the list (harmless no-op
    # today, self-activating if real WorkItem/subtask data lands later) and
    # covered by the two substitutes directly below, which reuse the same
    # named relations (IMPLEMENTS, ASSIGNED_TO) the plan called out.
    ChainPattern(
        "wi_parent_wi_assigned_person",
        """
        MATCH (n0:WorkItem)-[r1:PARENT_OF]->(n1:WorkItem)-[r2:ASSIGNED_TO]->(n2:Person)
        WHERE %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("PARENT_OF", "ASSIGNED_TO"),
        3,
    ),
    # Substitute for the pattern above: live match count 9.
    # Commit -IMPLEMENTS-> WorkItem -ASSIGNED_TO-> Person
    ChainPattern(
        "commit_wi_person",
        """
        MATCH (n0:Commit)-[r1:IMPLEMENTS]->(n1:WorkItem)-[r2:ASSIGNED_TO]->(n2:Person)
        WHERE %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("IMPLEMENTS", "ASSIGNED_TO"),
        3,
    ),
    # Same substitute family via PullRequest: live match count 8.
    ChainPattern(
        "pr_wi_person",
        """
        MATCH (n0:PullRequest)-[r1:IMPLEMENTS]->(n1:WorkItem)-[r2:ASSIGNED_TO]->(n2:Person)
        WHERE %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("IMPLEMENTS", "ASSIGNED_TO"),
        3,
    ),
    # Extension (2-hop, same relation family as example 1): live match count 6.
    # PullRequest -IMPLEMENTS-> WorkItem <-IMPLEMENTS- Commit
    ChainPattern(
        "pr_wi_commit",
        """
        MATCH (n0:PullRequest)-[r1:IMPLEMENTS]->(n1:WorkItem)<-[r2:IMPLEMENTS]-(n2:Commit)
        WHERE %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("IMPLEMENTS", "IMPLEMENTS"),
        3,
    ),
    # Extension (2-hop): live match count 9.
    # Commit -MODIFIES-> SourceFile <-MODIFIES- Commit (distinct commits)
    ChainPattern(
        "commit_sf_commit",
        """
        MATCH (n0:Commit)-[r1:MODIFIES]->(n1:SourceFile)<-[r2:MODIFIES]-(n2:Commit)
        WHERE n0.uid <> n2.uid AND %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("MODIFIES", "MODIFIES"),
        3,
    ),
    # Extension (2-hop, same relation family as example 3): live match count 5.
    # Decision -APPLIES_TO-> Api <-APPLIES_TO- Decision
    ChainPattern(
        "decision_api_decision",
        """
        MATCH (n0:Decision)-[r1:APPLIES_TO]->(n1:Api)<-[r2:APPLIES_TO]-(n2:Decision)
        WHERE n0.uid <> n2.uid AND %s
        RETURN DISTINCT n0.uid, n1.uid, n2.uid
        """ % _asserted("r1", "r2"),
        ("APPLIES_TO", "APPLIES_TO"),
        3,
    ),
]


# --------------------------------------------------------------- sampling


@dataclass
class ChainCandidate:
    pattern_id: str
    node_uids: list[str]
    relations: list[str]


def sample_chains(graph: Graph, rng: random.Random, patterns: list[ChainPattern] = PATTERNS) -> list[ChainCandidate]:
    """One chain per start node per relation pattern (plan.md step 1), so a
    single popular hub-adjacent node cannot dominate the sample."""
    candidates: list[ChainCandidate] = []
    for pattern in patterns:
        rows = graph.query(pattern.cypher).result_set
        rng.shuffle(rows)
        seen_start: set[str] = set()
        for row in rows:
            uids = [row[i] for i in range(pattern.node_count)]
            if any(u is None for u in uids):
                continue
            start = uids[0]
            if start in seen_start:
                continue
            seen_start.add(start)
            candidates.append(ChainCandidate(pattern.pattern_id, uids, list(pattern.relations)))
    rng.shuffle(candidates)
    return candidates


def fetch_nodes(graph: Graph, uids: list[str]) -> dict[str, dict[str, Any]]:
    """Node properties needed for (a) prompting the LLM and (b) the leakage
    filter -- the same field set `graph/entity.py`'s `_entity` reads."""
    if not uids:
        return {}
    rows = graph.query(
        """
        UNWIND $uids AS uid
        MATCH (n {uid: uid})
        RETURN n.uid, labels(n)[0], n.name, n.issue_key, n.path, n.sha, n.pr_ref,
               n.search_text, n.definition, n.statement, n.purpose
        """,
        params={"uids": uids},
    ).result_set
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid, label, name, issue_key, path, sha, pr_ref, search_text, definition, statement, purpose = row
        content = next((v for v in (definition, statement, purpose, search_text) if v), "") or ""
        out[uid] = {
            "uid": uid,
            "label": label,
            "name": name or "",
            "issue_key": issue_key or "",
            "path": path or "",
            "sha": sha or "",
            "pr_ref": pr_ref or "",
            "content": content[:400],
        }
    return out


def _group_key(graph: Graph, node_uids: list[str]) -> str:
    """Project/workspace grouping key for the dev/test split (plan.md step
    7), using structural edges that already exist on these nodes:
    WorkItem-BELONGS_TO->Project, Repository-HAS_REPOSITORY<-Project (via
    CONTAINS to SourceFile/Commit), Document<-HAS_DOCUMENT-Project. Decision/
    System/Person/Api nodes have no such structural link in this schema, so
    they fall back to the provider+connection_id prefix of their
    MENTIONED_IN SourceRecord (the same two segments a record_key always
    starts with, e.g. "jira:<site-id>:...") -- still a real, already-present
    schema field (`SourceRecord.record_key`), not an invented one."""
    rows = graph.query(
        """
        UNWIND $uids AS uid
        MATCH (n {uid: uid})-[:BELONGS_TO|HAS_REPOSITORY|HAS_DOCUMENT|CONTAINS*1..2]-(p:Project)
        RETURN DISTINCT p.uid LIMIT 1
        """,
        params={"uids": node_uids},
    ).result_set
    if rows and rows[0][0]:
        return f"project:{rows[0][0]}"
    rows = graph.query(
        """
        UNWIND $uids AS uid
        MATCH (n {uid: uid})-[:MENTIONED_IN]->(sr:SourceRecord)
        WHERE sr.record_key IS NOT NULL
        RETURN sr.record_key LIMIT 1
        """,
        params={"uids": node_uids},
    ).result_set
    if rows and rows[0][0]:
        parts = str(rows[0][0]).split(":")
        return "record:" + ":".join(parts[:2])
    return "ungrouped"


# --------------------------------------------------------------- anchor/answer


def select_anchor_answer(node_count: int, rng: random.Random) -> tuple[int, int]:
    """Pick a (anchor, answer) index pair on the linear chain with
    `abs(anchor - answer) >= 2` -- plan.md: "answering must require at least
    two hops" -- and, across many chains, not always the chain's last node
    (plan.md: "the answer is exactly one node on the chain (not necessarily
    the last one)"). Anchor is what the question already grounds the reader
    in; answer is the node the question must identify."""
    pairs = [(i, j) for i in range(node_count) for j in range(node_count) if abs(i - j) >= 2]
    if not pairs:
        raise ValueError(f"no valid (anchor, answer) pair for a {node_count}-node chain")
    return rng.choice(pairs)


def trim_subpath(candidate: ChainCandidate, anchor_idx: int, answer_idx: int) -> tuple[list[str], list[str]]:
    """The reasoning path actually needed for the question: just the slice
    between anchor and answer, walked start(anchor)->end(answer). Relation
    names are direction-agnostic (an edge type doesn't flip when walked
    backwards), so no reversal is needed for `relations`, only for the uid
    order when anchor is after answer in the original sampled path."""
    lo, hi = min(anchor_idx, answer_idx), max(anchor_idx, answer_idx)
    uids = candidate.node_uids[lo : hi + 1]
    relations = candidate.relations[lo:hi]
    if anchor_idx > answer_idx:
        uids = list(reversed(uids))
        relations = list(reversed(relations))
    return uids, relations


# --------------------------------------------------------------- leakage filter


def _leak_terms(nodes: list[dict[str, Any]]) -> list[str]:
    terms: list[str] = []
    for node in nodes:
        for field in LEAK_FIELDS:
            value = str(node.get(field) or "").strip()
            if len(value) >= MIN_LEAK_FIELD_LEN:
                terms.append(value)
        sha = str(node.get("sha") or "")
        if len(sha) >= SHA_PREFIX_LEN:
            terms.append(sha[:SHA_PREFIX_LEN])
    return terms


def leakage_free(question: str, nodes: list[dict[str, Any]]) -> bool:
    """plan.md step 3: reject if any chain node's name/issue_key/path/sha
    prefix/pr_ref literally appears in the question text (case-insensitive
    substring containment)."""
    q = question.lower()
    return not any(term.lower() in q for term in _leak_terms(nodes))


# --------------------------------------------------------------- LLM generation


class ChainQuestionVariants(BaseModel):
    indirect_question: str
    anchored_question: str


SYSTEM_PROMPT = """\
You write evaluation questions for a knowledge-graph retrieval system, following the \
CoEvoKG chain-question method. You are given a short reasoning path through connected \
project records: an ANCHOR record the reader already knows about, one or more relations, \
and a TARGET record that is the one and only correct answer.

Write TWO question variants. Both must require walking the ENTIRE path (at least two \
hops) to answer -- a reader must not be able to answer from the anchor alone or from \
only the first relation. Both must have exactly the same answer: the target record.

1. "indirect_question": a generic, role/attribute-based question. Never repeat the \
anchor's or target's exact name, ticket key, file path, commit SHA, or PR reference -- \
describe the anchor only by its kind, topic, or role (e.g. "the ticket documented by our \
onboarding guide", never its literal title).
2. "anchored_question": a more realistic question, phrased the way a real user might \
type it. It may reference the anchor's topic or purpose in a few descriptive words, but \
must still NOT quote the anchor's or target's exact name, ticket key, file path, commit \
SHA, or PR reference verbatim -- paraphrase instead of repeating the literal string.

Never reveal the target's name, ticket key, file path, SHA, or PR reference in either \
question -- that would give away the answer. Keep each question to one sentence.
"""


def _describe_node(role: str, node: dict[str, Any]) -> str:
    bits = [f"kind={node['label']}"]
    if node.get("name"):
        bits.append(f"name={node['name']!r}")
    if node.get("content"):
        bits.append(f"content={node['content']!r}")
    return f"[{role}] " + ", ".join(bits)


def format_chain_prompt(nodes: list[dict[str, Any]], relations: list[str]) -> str:
    lines = ["Reasoning path, anchor to target:"]
    for i, node in enumerate(nodes):
        if i == 0:
            role = "ANCHOR (already known to the reader)"
        elif i == len(nodes) - 1:
            role = "TARGET (the answer -- never reveal its identifier)"
        else:
            role = "intermediate (never reveal its identifier)"
        lines.append(f"{i + 1}. {_describe_node(role, node)}")
        if i < len(relations):
            lines.append(f"   --{relations[i]}-->")
    return "\n".join(lines)


def generate_variants(client: OpenAI, model: str, nodes: list[dict[str, Any]], relations: list[str]) -> tuple[ChainQuestionVariants, Any]:
    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": format_chain_prompt(nodes, relations)},
        ],
        text_format=ChainQuestionVariants,
    )
    return response.output_parsed, response


# --------------------------------------------------------------- split


def assign_splits(group_keys: list[str], rng: random.Random, dev_fraction: float = DEV_FRACTION) -> dict[str, str]:
    """Whole groups go to dev or test -- never split a group across the
    boundary (plan.md step 7: "a dev/test split doesn't leak near-duplicate
    chain nodes across the boundary"). Greedy bin-packing by row count,
    shuffled so it isn't always the same groups in dev."""
    counts: dict[str, int] = {}
    for key in group_keys:
        counts[key] = counts.get(key, 0) + 1
    unique_groups = list(counts)
    rng.shuffle(unique_groups)
    total = len(group_keys)
    target_dev = round(total * dev_fraction)
    assignment: dict[str, str] = {}
    dev_count = 0
    for key in unique_groups:
        if dev_count < target_dev:
            assignment[key] = "dev"
            dev_count += counts[key]
        else:
            assignment[key] = "test"
    # A single group (or a target_dev of 0) would otherwise put everything in
    # one split; fall back to putting at least one group in test so the split
    # is never degenerate when more than one group exists.
    if len(unique_groups) > 1 and all(v == "dev" for v in assignment.values()):
        assignment[unique_groups[-1]] = "test"
    return assignment


# --------------------------------------------------------------- orchestration


def build_rows(
    graph: Graph,
    client: OpenAI,
    model: str,
    candidates: list[ChainCandidate],
    rng: random.Random,
    *,
    limit: int | None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Returns (rows, llm_calls, rejected_leakage)."""
    rows: list[dict[str, Any]] = []
    llm_calls = 0
    rejected = 0
    group_cache: dict[tuple[str, ...], str] = {}

    for candidate in candidates:
        if limit is not None and len(rows) >= limit:
            break
        try:
            anchor_idx, answer_idx = select_anchor_answer(len(candidate.node_uids), rng)
        except ValueError:
            continue
        sub_uids, sub_relations = trim_subpath(candidate, anchor_idx, answer_idx)
        node_info = fetch_nodes(graph, candidate.node_uids)
        sub_nodes = [node_info[u] for u in sub_uids if u in node_info]
        if len(sub_nodes) != len(sub_uids):
            logger.warning("skipping chain %s: node lookup incomplete", candidate.pattern_id)
            continue

        try:
            variants, response = generate_variants(client, model, sub_nodes, sub_relations)
        except Exception:
            logger.exception("LLM call failed for chain %s (start=%s)", candidate.pattern_id, candidate.node_uids[0])
            continue
        llm_calls += 1

        leak_nodes = list(node_info.values())  # check against every sampled node, not just the trimmed subpath
        chosen_question, chosen_variant = None, None
        if leakage_free(variants.anchored_question, leak_nodes):
            chosen_question, chosen_variant = variants.anchored_question, "anchored"
        elif leakage_free(variants.indirect_question, leak_nodes):
            chosen_question, chosen_variant = variants.indirect_question, "indirect"
        else:
            rejected += 1
            logger.info("dropped chain %s (start=%s): both variants leaked", candidate.pattern_id, candidate.node_uids[0])
            continue

        key = tuple(sorted(candidate.node_uids))
        if key not in group_cache:
            group_cache[key] = _group_key(graph, candidate.node_uids)

        rows.append({
            "question": chosen_question,
            "question_indirect": variants.indirect_question,
            "question_anchored": variants.anchored_question,
            "variant": chosen_variant,
            "answer_uid": sub_uids[-1],
            "chain_uids": sub_uids,
            "chain_relations": sub_relations,
            "hops": len(sub_relations),
            "generator": "chain-v1",
            "pattern": candidate.pattern_id,
            "group_key": group_cache[key],
            "llm_usage": {
                "input_tokens": getattr(response.usage, "input_tokens", None),
                "output_tokens": getattr(response.usage, "output_tokens", None),
            },
        })

    return rows, llm_calls, rejected


def finalize_rows(rows: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    split_by_group = assign_splits([r["group_key"] for r in rows], rng)
    out = []
    for i, row in enumerate(rows, start=1):
        row = dict(row)
        row["id"] = f"c-{i:04d}"
        row["split"] = split_by_group[row.pop("group_key")]
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", default=multigraph.DEFAULT_GRAPH_NAME, help="graph_name slug (multigraph.resolve)")
    parser.add_argument("--sample", type=int, default=10, help="cap on chains to attempt (small-scale run)")
    parser.add_argument("--full", action="store_true", help="attempt every sampled chain, ignoring --sample")
    parser.add_argument("--output", type=Path, default=Path("eval/chain_golden.jsonl"))
    parser.add_argument("--model", default=None, help="override CHAIN_GOLDEN_MODEL/LLM_MODEL")
    parser.add_argument("--seed", type=int, default=20260925, help="RNG seed, for a reproducible sample")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    rng = random.Random(args.seed)
    target = multigraph.resolve(
        args.graph, data_dir=DATA_DIR,
        base_falkor_name=os.getenv("FALKOR_GRAPH", "neuron"),
        base_collection=os.getenv("QDRANT_COLLECTION", "neuron_entities"),
    )
    graph = get_graph(name=target.falkor_name)
    client = OpenAI()
    model = args.model or os.getenv("CHAIN_GOLDEN_MODEL") or os.getenv("LLM_MODEL", "gpt-5.6-luna")

    candidates = sample_chains(graph, rng)
    logger.info("sampled %d candidate chains across %d patterns", len(candidates), len(PATTERNS))
    limit = None if args.full else args.sample

    rows, llm_calls, rejected = build_rows(graph, client, model, candidates, rng, limit=limit)
    rows = finalize_rows(rows, rng)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    dev = sum(1 for r in rows if r["split"] == "dev")
    test = sum(1 for r in rows if r["split"] == "test")
    print(f"wrote {len(rows)} rows to {args.output} (dev={dev}, test={test})")
    print(f"llm_calls={llm_calls} rejected_leakage={rejected} candidates_seen={len(candidates)} model={model}")


if __name__ == "__main__":
    main()
