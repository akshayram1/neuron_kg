"""Findings-grounded wisdom proposal generation.

The ingestion path establishes facts and findings.  This module runs later,
over a compact cluster of findings plus their evidence lineage, and may only
produce a reviewable proposal.  It never promotes organisational wisdom by
itself.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from openai import OpenAI
from pydantic import BaseModel, Field

from graph.token_usage import TokenUsage


class WisdomApplicability(BaseModel):
    event_types: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    implementation_authority: list[str] = Field(default_factory=list)
    declaration_sources: list[str] = Field(default_factory=list)


class WisdomProposalExtraction(BaseModel):
    action: Literal[
        "create", "strengthen_existing", "weaken_existing",
        "supersede_existing", "insufficient_evidence",
    ] = "create"
    wisdom_type: Literal["Policy", "Principle", "Pattern", "AntiPattern", "Playbook", "Heuristic"]
    topic_key: str = Field(description="Stable kebab-case identifier for the reusable lesson.")
    title: str
    statement: str
    rationale: str
    recommended_action: str
    applicability: WisdomApplicability = Field(default_factory=WisdomApplicability)
    confidence: float = Field(ge=0.0, le=1.0)
    promotion: Literal["human_review", "insufficient_evidence"] = "human_review"
    review_reason: str


WISDOM_AGGREGATION_PROMPT = """\
You create reviewable organisational wisdom from FINDINGS, not directly from
raw source text. Findings are the primary input; their facts, excerpts,
timestamps and source authority are grounding and counter-evidence.

Generalise only the recurring or high-impact lesson supported by the supplied
cluster. Do not turn a project-specific event into a universal rule unless the
applicability section narrows its scope. Preserve counter-evidence in your
confidence and rationale. Existing wisdom candidates are references: choose
strengthen_existing, weaken_existing or supersede_existing only when the input
clearly supports it. Otherwise create a new proposal or return
insufficient_evidence.

The output is a WisdomProposal requiring human review. It is never active
wisdom merely because you produced it. Prefer a precise, testable statement
and an actionable recommendation. Do not invent finding IDs, sources, facts,
dates, affected systems or evidence beyond the supplied input.
"""


def generate_wisdom_proposal(
    client: OpenAI,
    *,
    pattern_key: str,
    findings: list[dict[str, Any]],
    existing_wisdom: list[dict[str, Any]],
    model: str | None = None,
) -> tuple[WisdomProposalExtraction, TokenUsage]:
    """Run the separate aggregation call over a bounded findings cluster."""
    payload = {
        "pattern_key": pattern_key,
        "findings_with_lineage": findings,
        "existing_wisdom_candidates": existing_wisdom,
    }
    response = client.responses.parse(
        model=model or os.getenv("WISDOM_LLM_MODEL") or os.getenv("LLM_MODEL", "gpt-5.6-luna"),
        input=[
            {"role": "system", "content": WISDOM_AGGREGATION_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ],
        text_format=WisdomProposalExtraction,
    )
    usage = TokenUsage()
    usage.add(response.usage)
    return response.output_parsed, usage


def migration_claim_fallback() -> WisdomProposalExtraction:
    """Safe demo fallback when the aggregation provider is unavailable."""
    return WisdomProposalExtraction(
        action="create",
        wisdom_type="Policy",
        topic_key="code-confirmed-api-migration",
        title="Verify API migrations from current code",
        statement=(
            "An API migration must not be considered complete until current "
            "implementation evidence confirms that the previous API version is no longer used."
        ),
        rationale=(
            "A migration completion claim conflicted with the current production-branch "
            "implementation and was confirmed only after the code changed."
        ),
        recommended_action=(
            "Keep migration findings open until current repository evidence confirms "
            "removal of the previous API dependency."
        ),
        applicability=WisdomApplicability(
            event_types=["api_migration", "api_deprecation", "api_removal"],
            entity_types=["Api", "Endpoint", "Project", "Repository"],
            domains=["api-migration"],
            implementation_authority=["bitbucket", "github"],
            declaration_sources=["jira", "notion"],
        ),
        confidence=0.82,
        promotion="human_review",
        review_reason="This proposal introduces an organisation-wide migration completion policy.",
    )


def removal_readiness_fallback() -> WisdomProposalExtraction:
    """Grounded fallback for a removal that still has a live code consumer."""
    return WisdomProposalExtraction(
        action="create",
        wisdom_type="Playbook",
        topic_key="consumer-verified-api-removal",
        title="Gate API removal on verified consumer readiness",
        statement=(
            "A deprecated API must not be removed while any known consumer's current "
            "implementation still depends on it."
        ),
        rationale=(
            "A previously identified consumer remained on the deprecated endpoint when "
            "the provider removed it, turning a predicted blast-radius risk into a failure."
        ),
        recommended_action=(
            "Before removal, verify every dependency-graph consumer against current code "
            "and block the change until no live calls to the deprecated endpoint remain."
        ),
        applicability=WisdomApplicability(
            event_types=["api_deprecation", "api_removal", "breaking_change"],
            entity_types=["Api", "Endpoint", "Project", "Repository", "WorkItem"],
            domains=["api-lifecycle", "change-readiness"],
            implementation_authority=["bitbucket", "github"],
            declaration_sources=["jira", "notion"],
        ),
        confidence=0.92,
        promotion="human_review",
        review_reason="This introduces an organisation-wide destructive-change readiness gate.",
    )
