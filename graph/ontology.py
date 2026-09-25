"""Shared semantic ontology — the entity/edge types an LLM extracts from free
text (plan.md §2.2 "Semantic" bucket: Decision, Term, System). Structural
entities (Project, WorkItem, Person, Repository, ...) are never LLM-extracted
— they come straight from provider API fields in each connector's
deterministic pass (plan.md §3 Pass A) and are plain dataclasses/dicts, not
Pydantic models here.

These models are the actual schema handed to the OpenAI structured-output
call (plan.md §7a: Pydantic at the LLM boundary, not the hot path). Class
docstrings are prompt engineering, not documentation — write them for a
reader who has only seen one paragraph of source text.

Ported from graphiti_context_explorer/graph/ontology.py. Three changes from
the original, all deliberate:
  - `CatalogEntity` dropped — that was a data-catalog concept from the old
    business-metrics POC, not part of this project's schema (plan.md §2.2).
  - Every docstring's *examples* were rewritten for this project's domain
    (software/project-management knowledge, not revenue metrics) — the
    positive/negative-example pattern was kept, but keeping the literal old
    examples (MRR, glossary.mrr, subscriptions_snapshot) would have actively
    misled extraction here. `Person` itself was NOT ported as an extractable
    type: it is structural in this project (built from Jira/GitHub/Notion API
    fields, plan.md §2.2), so the LLM never creates a Person node — it only
    references one by name as an edge endpoint, which `RELATION_TYPE_MAP`
    below accounts for.
  - Every entity model gets a required `name` field, which the original did
    NOT have ("Graphiti owns name/uuid/group_id on every node; redeclaring
    collides"). There is no Graphiti here — without a `name`, `graph.writer`
    has nothing to call `make_uid()` on, so this project's extractor must
    supply it explicitly.

Rules that still matter (unchanged from the source):
  - Every field Optional with a default. The extractor leaves most blank on
    most chunks, and a required field turns "didn't mention it" into a
    validation failure that kills the whole chunk's extraction.
  - Keep the set small. A handful of types the LLM can reliably tell apart
    beats many it guesses between — ambiguous types are the #1 cause of a
    noisy graph.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------- entities


class Term(BaseModel):
    """A named concept with an agreed meaning that the team refers to
    repeatedly — a domain word, an acronym, a named threshold or policy: rate
    limit, SLA, the ingestion window, blast radius, error budget.

    Do NOT extract sentence fragments, document titles, or vague phrases:
    "the new approach", "Q3 planning doc", "performance considerations",
    "the usual workflow"."""

    name: str = Field(description="The concept's canonical name, as short as the text allows.")
    definition: str | None = Field(
        None, description="How the text defines this concept, in one sentence."
    )
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names used for the same concept in this text.",
    )


class Decision(BaseModel):
    """A choice that was made, with a stated reason. Extract whenever the text
    says chose / decided / we will now / switched to / deprecated ... because.
    Name it as a short noun phrase: "use Redis sliding window for rate
    limiting", "deprecate the v1 export endpoint", "require code review before
    merge".

    This is the type that carries the *why*. Do not fold a choice+reason into
    a CAVEAT_OF on the affected system/term instead. Do not extract a mere
    description of behavior where no choice was stated."""

    name: str = Field(description="Short noun phrase naming the decision, e.g. 'use Redis sliding window for rate limiting'.")
    statement: str | None = Field(None, description="The decision in one sentence, active voice.")
    rationale: str | None = Field(
        None, description="The stated reason. Blank if the text gives none — do not invent one."
    )
    status: Literal["proposed", "accepted", "rejected", "superseded", "unknown"] | None = None


class System(BaseModel):
    """A named software platform, service, or tool — Redis, Postgres, the
    Auth Service, the CI pipeline, Typesense. Not a generic domain noun
    (subscription, billing, pipeline-as-a-concept) and not a data asset
    inside one (a specific table or dashboard)."""

    name: str = Field(description="The system's proper name, e.g. 'Redis', 'the Auth Service'.")
    purpose: str | None = Field(None, description="What it is used for here, if stated.")


class Api(BaseModel):
    """A named, versioned software interface such as Auth API v1. Keep
    versions distinct when the source distinguishes them; do not collapse v1
    and v2 into one entity."""

    name: str = Field(description="Canonical API name including version when stated.")
    version: str | None = None
    status: Literal["active", "deprecated", "removed", "planned", "unknown"] | None = None


class Endpoint(BaseModel):
    """A concrete callable API route, including its HTTP method when known."""

    name: str = Field(description="Canonical endpoint, e.g. 'POST /v1/auth'.")
    method: str | None = None
    path: str | None = None


ENTITY_TYPES: dict[str, type[BaseModel]] = {
    "Term": Term,
    "Decision": Decision,
    "System": System,
    "Api": Api,
    "Endpoint": Endpoint,
}

EXTRACTION_INSTRUCTIONS = """\
Prefer a few well-typed entities over many. Skip sentence fragments.
If the text states a choice and a reason, emit a Decision and DECIDED_BY / APPLIES_TO.
Never invent an entity or edge type that is not in the provided list.

Coreference resolution: if an entity is mentioned multiple times in this text
under different names, aliases, or pronouns, treat all mentions as the SAME
entity and use its most complete identifier consistently. Do not create a
separate entity per mention.

Do not add outside knowledge. Every field you fill (rationale, definition,
severity, etc.) must be traceable to what this text actually says — if the
text doesn't state it, leave the field blank rather than inferring it.
  - Good: "switched to Redis because Postgres locking caused timeouts" -> rationale: "Postgres locking caused timeouts"
  - Bad: rationale: "likely for better performance" (invented, not stated)

Dates: never invent a date the text does not state. A clock time without a
timezone stays a calendar date — do not guess a zone. If the text says a
relation ended but not when ("former assignee", "stepped down", "no longer"),
put that in `when` as "unknown" rather than implying it still holds. A
commit, a resolution, or "resolved on DATE" is a moment, not an open interval.
Do not collapse "partners A, B and C" into one fact — one fact per named party.
"""


# ------------------------------------------------------------------------ edges


class DEFINES(BaseModel):
    """The source states the meaning of the target."""

    scope: str | None = Field(None, description="Where this definition applies, if narrowed.")


class APPLIES_TO(BaseModel):
    """The source is implemented by, governs, or is measured on the target."""


class CAVEAT_OF(BaseModel):
    """The source is a limitation, gotcha, or 'do not use for' about the
    target. Use for warnings. If the text also states a choice and a reason,
    emit a Decision as well — do not replace the Decision with only this edge."""

    severity: Literal["info", "warning", "blocker"] | None = None


class DECIDED_BY(BaseModel):
    """The source decision was made or approved by the target person/team."""

    when: str | None = Field(None, description="Date or period as the text states it.")


class SUPERSEDES(BaseModel):
    """The source replaces or overrides the target. This is for a replacement
    the text states outright, not automatic contradiction detection (this
    project has no Graphiti-style automatic fact invalidation — supersession
    must be either this explicit edge or the deterministic temporal diff in
    graph/writer.py's `supersede_fact_edges`)."""

    reason: str | None = Field(None, description="Why it was replaced, if stated.")


class OWNS(BaseModel):
    """The source person/team is accountable for the target. Never from a
    System or a population/role noun (e.g. "the on-call team" as a generic
    concept) to anything — only a named person or a specifically named team."""


class PROVIDES_API(BaseModel):
    """The subject explicitly exposes or owns the target API."""


class CONSUMES_API(BaseModel):
    """The subject explicitly depends on or calls the target API."""


class EXPOSES_ENDPOINT(BaseModel):
    """The API makes the concrete target endpoint available."""


class CALLS_ENDPOINT(BaseModel):
    """The subject code or project explicitly calls the target endpoint."""


class CHANGES(BaseModel):
    """The subject change modifies the target system, API, or endpoint."""


class DEPRECATES(BaseModel):
    """The subject explicitly deprecates or schedules removal of the target."""


class MIGRATES_TO(BaseModel):
    """The subject explicitly moves from an older interface to the target."""


EDGE_TYPES: dict[str, type[BaseModel]] = {
    "DEFINES": DEFINES,
    "APPLIES_TO": APPLIES_TO,
    "CAVEAT_OF": CAVEAT_OF,
    "DECIDED_BY": DECIDED_BY,
    "SUPERSEDES": SUPERSEDES,
    "OWNS": OWNS,
    "PROVIDES_API": PROVIDES_API,
    "CONSUMES_API": CONSUMES_API,
    "EXPOSES_ENDPOINT": EXPOSES_ENDPOINT,
    "CALLS_ENDPOINT": CALLS_ENDPOINT,
    "CHANGES": CHANGES,
    "DEPRECATES": DEPRECATES,
    "MIGRATES_TO": MIGRATES_TO,
}

RelationName = Literal[
    "DEFINES", "APPLIES_TO", "CAVEAT_OF", "DECIDED_BY", "SUPERSEDES", "OWNS",
    "PROVIDES_API", "CONSUMES_API", "EXPOSES_ENDPOINT", "CALLS_ENDPOINT",
    "CHANGES", "DEPRECATES", "MIGRATES_TO",
]

# Which (subject_kind, object_kind) pairs may carry which relations. `Person`,
# `WorkItem`, `Project` etc. are structural kinds the LLM never creates but
# may reference by name as an edge endpoint (plan.md non-negotiable #4:
# "Unknown/unlisted relation types are rejected, not stored" — this is the
# table that enforcement checks against, ported from the source project's
# `EDGE_TYPE_MAP` with CatalogEntity rows dropped and WorkItem/Project added
# so a Decision can attach to the work item or project it concerns).
RELATION_TYPE_MAP: dict[tuple[str, str], list[RelationName]] = {
    ("Term", "Term"): ["DEFINES", "SUPERSEDES", "CAVEAT_OF"],
    ("Decision", "Term"): ["APPLIES_TO", "CAVEAT_OF", "DEFINES"],
    ("Decision", "System"): ["APPLIES_TO", "CAVEAT_OF"],
    ("Decision", "WorkItem"): ["APPLIES_TO"],
    ("Decision", "Repository"): ["APPLIES_TO"],
    ("Decision", "SourceFile"): ["APPLIES_TO"],
    ("Decision", "Commit"): ["APPLIES_TO"],
    ("Decision", "PullRequest"): ["APPLIES_TO"],
    ("Decision", "Document"): ["APPLIES_TO"],
    ("Decision", "Workspace"): ["APPLIES_TO"],
    ("Decision", "Decision"): ["SUPERSEDES"],
    ("Decision", "Person"): ["DECIDED_BY"],
    ("Person", "Term"): ["OWNS"],
    ("Person", "Decision"): ["OWNS"],
    ("System", "System"): ["APPLIES_TO"],
    ("Document", "Term"): ["DEFINES"],
    ("SourceFile", "Term"): ["DEFINES"],
    ("Project", "Api"): ["PROVIDES_API", "CONSUMES_API", "MIGRATES_TO"],
    ("System", "Api"): ["PROVIDES_API", "CONSUMES_API", "MIGRATES_TO"],
    ("Api", "Endpoint"): ["EXPOSES_ENDPOINT"],
    ("SourceFile", "Endpoint"): ["CALLS_ENDPOINT"],
    ("Project", "Endpoint"): ["CALLS_ENDPOINT", "MIGRATES_TO"],
    ("WorkItem", "Api"): ["CHANGES", "DEPRECATES", "MIGRATES_TO"],
    ("WorkItem", "Endpoint"): ["CHANGES", "DEPRECATES", "MIGRATES_TO"],
    ("Decision", "Api"): ["APPLIES_TO", "CHANGES", "DEPRECATES", "MIGRATES_TO"],
    ("Decision", "Endpoint"): ["APPLIES_TO", "CHANGES", "DEPRECATES", "MIGRATES_TO"],
    ("PullRequest", "Api"): ["CHANGES", "MIGRATES_TO"],
    ("PullRequest", "Endpoint"): ["CHANGES", "MIGRATES_TO"],
    ("Commit", "Api"): ["CHANGES", "MIGRATES_TO"],
    ("Commit", "Endpoint"): ["CHANGES", "MIGRATES_TO"],
    ("Document", "Api"): ["DEFINES", "APPLIES_TO"],
    ("Document", "Endpoint"): ["DEFINES", "APPLIES_TO"],
}


def is_relation_allowed(subject_kind: str, relation: str, object_kind: str) -> bool:
    """plan.md non-negotiable: unknown/unlisted relation types are rejected,
    not stored. Callers (Block 7's extraction validation, Tier-3 adjudication)
    must check this before writing an edge."""
    return relation in RELATION_TYPE_MAP.get((subject_kind, object_kind), [])


AS_IS = "as_is"
SWAPPED = "swapped"


def resolve_direction(subject_kind: str, relation: str, object_kind: str) -> str | None:
    """Which way round this triple is allowed to be written, if either.

    Returns `AS_IS`, `SWAPPED`, or None when neither direction is in the
    ontology.

    Why a swap instead of a rejection: argument order is an encoding
    convention of the relation, not a claim about the world, and English
    invites the model to state it backwards -- "X is an employee of Y" reads
    as naturally as the declared `employee (organization -> person)`. The map
    already knows which endpoint kinds each relation takes, so when the
    reverse triple is valid and the stated one is not, the model got the
    direction wrong, not the fact. Salvaging it is strictly better than
    discarding a true statement, PROVIDED it is never silent: callers record
    a `direction_corrected` trace.

    Utopia reached the same conclusion after three rounds of prompt tuning
    failed to suppress the reversal
    (`utopia/docs/decisions/0012-the-ontology-is-a-contract-not-a-suggestion.md`).
    """
    if is_relation_allowed(subject_kind, relation, object_kind):
        return AS_IS
    if is_relation_allowed(object_kind, relation, subject_kind):
        return SWAPPED
    return None
