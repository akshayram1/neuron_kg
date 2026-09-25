"""Per-source extraction profiles (plan.md §3.1). A profile packages: which
chunk policy applies, the extraction instructions, and the structured-output
Pydantic schema an LLM call for that source's free text must produce.

Jira, GitHub and Notion each have a source-specific prompt while sharing the
same small Decision/Term/System output schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from connectors.core.chunking.models import ChunkPolicy
from connectors.core.models import SourceRecord
from graph.ontology import (
    EXTRACTION_INSTRUCTIONS,
    Api,
    Decision,
    Endpoint,
    RelationName,
    System,
    Term,
)

# Structural kinds are never LLM-created (plan.md §2.2) — the extractor may
# only *reference* one by name as a fact's subject/object, using the name as
# it appears in the chunk's source header (issue key, assignee name, project
# name) so the deterministic pass (Block 6) can resolve it back to a real uid.
StructuralKind = Literal[
    "WorkItem", "Person", "Project", "Repository", "SourceFile", "Commit",
    "PullRequest", "Document", "Workspace",
]
SemanticKind = Literal["Decision", "Term", "System", "Api", "Endpoint"]


class ExtractedFact(BaseModel):
    """One subject-relation-object claim, with verbatim evidence. Validate
    every instance against `graph.ontology.is_relation_allowed` before
    writing it — an LLM producing a syntactically valid but unlisted
    (kind, relation, kind) triple must be rejected, not stored (plan.md
    non-negotiable #4)."""

    subject_name: str
    subject_kind: StructuralKind | SemanticKind
    relation: RelationName
    object_name: str
    object_kind: StructuralKind | SemanticKind
    subject_candidate_uid: str | None = Field(
        None, description="Retrieved candidate UID when attaching the subject to an existing node."
    )
    object_candidate_uid: str | None = Field(
        None, description="Retrieved candidate UID when attaching the object to an existing node."
    )
    evidence: str = Field(description="Verbatim span from the text supporting this fact.")
    severity: Literal["info", "warning", "blocker"] | None = None
    when: str | None = None
    reason: str | None = None
    scope: str | None = None


class IngestionAssessment(BaseModel):
    """A user-visible judgement made only from new evidence versus retrieved history."""

    action: Literal[
        "addition", "update", "contradiction", "architecture_change", "review"
    ]
    topic_key: str = Field(
        description="Stable kebab-case subject key, e.g. auth-api-v2-migration. Use the same key across sources describing the same change."
    )
    should_flag: bool = False
    severity: Literal["info", "warning", "high", "critical"] = "info"
    title: str
    summary: str
    reasoning: str
    evidence: str = Field(description="Verbatim span from the NEW SOURCE only.")
    related_candidate_uids: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class WorkManagementExtraction(BaseModel):
    """Structured output for one `work_management` chunk — the whole schema
    handed to the OpenAI call in Block 7."""

    terms: list[Term] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    systems: list[System] = Field(default_factory=list)
    apis: list[Api] = Field(default_factory=list)
    endpoints: list[Endpoint] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    assessments: list[IngestionAssessment] = Field(default_factory=list)


@dataclass(frozen=True)
class ExtractionProfile:
    name: str
    schema: type[BaseModel]
    instructions: str
    chunk_policy: ChunkPolicy


_WORK_MANAGEMENT_ADDENDUM = """\
This text is a Jira project or issue. Its structural facts — assignee,
reporter, status, project membership, blocking/dependency links — are already
captured deterministically from the API and must NOT be re-extracted here.
Only extract what adds semantic value beyond that: decisions made in the
description or comments, domain terms defined or used, systems named, and
caveats/warnings stated about them.

A fact's subject or object may be the work item itself, its assignee, or its
project — refer to them exactly as named in the [SOURCE] header of this
chunk (e.g. the issue key, or the person's name as written), not by pronoun.

Every term/decision/system you list MUST also appear as the subject or object
of at least one fact in `facts` — an entity with no connecting fact is
discarded before it reaches the graph, so naming one without a fact wastes
the extraction. If you cannot state a real relationship for something, do
not list it as an entity at all.
"""

_ADJUDICATION_ADDENDUM = """\
This call receives ONLY evidence that deterministic Pass 1 could not fully
connect. A bounded [RELATED EXISTING EVIDENCE + CANDIDATE NODES] section may follow it.
Candidate UIDs are untrusted references, not evidence. You may use a candidate
UID on a fact only when it is explicitly listed and its kind matches.

Also classify material implications in `assessments`: addition, update,
contradiction, architecture_change, or review. Set `should_flag=true` only for
a contradiction, a broad architecture/dependency change, or genuinely
ambiguous evidence needing a human. Ordinary additions and updates normally
remain unflagged. Every assessment must quote verbatim evidence from NEW
SOURCE; never quote retrieved history as new evidence.
Use one stable `topic_key` for the same real-world change across Jira, Notion,
commit, pull-request, and source-file evidence so their findings consolidate.
"""

WORK_MANAGEMENT = ExtractionProfile(
    name="work_management",
    schema=WorkManagementExtraction,
    instructions=EXTRACTION_INSTRUCTIONS + "\n" + _WORK_MANAGEMENT_ADDENDUM + "\n" + _ADJUDICATION_ADDENDUM,
    chunk_policy=ChunkPolicy(target_tokens=1500, hard_max_tokens=3500, overlap_tokens=0),
)

_SOFTWARE_KNOWLEDGE_ADDENDUM = """\
This text is from a source file, commit, or pull request on GitHub or
Bitbucket. Repository membership, file paths, authors and timestamps are
already captured deterministically from the provider API and must NOT be
re-extracted. Extract only durable knowledge expressed by the content:
architectural or implementation decisions, domain terms, named systems, and
explicit caveats. A pull request's title/description is human-written prose
explaining *why* a change was made -- often the highest-signal source in a
repository for this kind of knowledge, unlike a source file's raw code.

Do not treat imports, variable names, ordinary functions, code syntax, or a
commit title by itself as a domain entity. Every term/decision/system you list
MUST participate in at least one allowed fact; disconnected entities are
discarded.
"""

SOFTWARE_KNOWLEDGE = ExtractionProfile(
    name="software_knowledge",
    schema=WorkManagementExtraction,
    instructions=EXTRACTION_INSTRUCTIONS + "\n" + _SOFTWARE_KNOWLEDGE_ADDENDUM + "\n" + _ADJUDICATION_ADDENDUM,
    chunk_policy=ChunkPolicy(target_tokens=1700, hard_max_tokens=3500, overlap_tokens=0),
)

_BUSINESS_DOCUMENT_ADDENDUM = """\
This text is a Notion document. Its title, workspace and parent-page hierarchy
are already captured deterministically. Extract only durable knowledge in the
body: decisions, explicitly defined terms, named systems, and caveats.

The document itself may be referenced exactly as named in the [SOURCE] header.
Every term/decision/system you list MUST participate in at least one allowed
fact; disconnected entities are discarded.
"""

BUSINESS_DOCUMENT = ExtractionProfile(
    name="business_document",
    schema=WorkManagementExtraction,
    instructions=EXTRACTION_INSTRUCTIONS + "\n" + _BUSINESS_DOCUMENT_ADDENDUM + "\n" + _ADJUDICATION_ADDENDUM,
    chunk_policy=ChunkPolicy(target_tokens=1800, hard_max_tokens=3800, overlap_tokens=0),
)

PROFILES: dict[str, ExtractionProfile] = {
    "work_management": WORK_MANAGEMENT,
    "software_knowledge": SOFTWARE_KNOWLEDGE,
    "business_document": BUSINESS_DOCUMENT,
}


def get_profile(name: str) -> ExtractionProfile:
    return PROFILES[name]


def profile_for_record(record: SourceRecord) -> ExtractionProfile:
    """Route a canonical record to its extraction profile (ingest.md §11
    pattern, reduced to the one provider currently implemented). Extend this
    per-provider as GitHub (`software_knowledge`) and Notion
    (`business_document`) are added — see plan.md §1 build order."""
    if record.provider == "jira": return WORK_MANAGEMENT
    if record.provider in ("github", "bitbucket"): return SOFTWARE_KNOWLEDGE
    if record.provider == "notion": return BUSINESS_DOCUMENT
    raise ValueError(f"no extraction profile for provider={record.provider!r}")


def profile_for_record_key(record_key: str) -> ExtractionProfile:
    provider = record_key.split(":", 1)[0].lower()
    if provider == "jira": return WORK_MANAGEMENT
    if provider in ("github", "bitbucket"): return SOFTWARE_KNOWLEDGE
    if provider == "notion": return BUSINESS_DOCUMENT
    raise ValueError(f"no extraction profile for record_key={record_key!r}")
