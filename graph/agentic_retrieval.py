"""Bounded LLM query decomposition for Phase 7.5.

The planner never selects tools, writes graph data, or answers the user.  It
may only produce a small list of follow-up searches after ordinary retrieval
and Laya's graph-hop recovery have both failed to find enough direct evidence.
Every returned candidate still passes through the normal ACL filters, Laya
relevance gate, path validation, token pack, and grounded answer prompt.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field

from graph.token_usage import TokenUsage


class RetrievalPlan(BaseModel):
    needs_followup: bool
    subqueries: list[str] = Field(default_factory=list, max_length=3)


_SYSTEM = """You plan bounded searches over a company knowledge graph.
The first retrieval pass did not find enough direct evidence. Decompose the
question only when another search could recover a missing entity, intermediate
hop, synonym, date-specific state, repository/file clue, or causal link.
Return at most the requested number of short standalone search queries. Use
candidate clues as vocabulary, but do not answer the question, invent facts,
or request external/web knowledge. If no useful decomposition exists, set
needs_followup=false and return no subqueries."""


def plan_subqueries(
    client: OpenAI,
    question: str,
    candidate_clues: list[dict[str, Any]],
    *,
    max_subqueries: int = 2,
    token_usage: TokenUsage | None = None,
    model: str | None = None,
) -> list[str]:
    """Return a deterministic-order, de-duplicated bounded search plan."""
    maximum = max(1, min(3, int(max_subqueries)))
    payload = {
        "question": question,
        "max_subqueries": maximum,
        "candidate_clues": candidate_clues[:8],
    }
    response = client.responses.parse(
        model=model or os.getenv("NEURON_AGENTIC_MODEL", "gpt-5.6-luna"),
        input=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text_format=RetrievalPlan,
    )
    if token_usage is not None:
        token_usage.add(response.usage)
    plan: RetrievalPlan = response.output_parsed
    if not plan.needs_followup:
        return []
    original = " ".join(question.casefold().split())
    seen: set[str] = set()
    output: list[str] = []
    for raw in plan.subqueries:
        query = " ".join(str(raw).split()).strip()
        key = query.casefold()
        if not query or key == original or key in seen:
            continue
        seen.add(key)
        output.append(query)
        if len(output) >= maximum:
            break
    return output
