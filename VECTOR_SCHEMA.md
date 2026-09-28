# Neuron vector and source schema

## Embedding contract

Neuron loads `BAAI/bge-m3` once inside the main backend through
SentenceTransformers. The process-wide lazy model is shared by query,
ingestion, semantic-pass, story-evidence, and rebuild paths.

| Property | Value |
|---|---|
| Dense dimension | 1024 |
| Similarity | cosine |
| Normalization | L2 normalized |
| Maximum input | 8192 tokens |
| Entity vector channels | `content`, `name` |
| Projection schema version | 2 |

All embedding paths call `graph.embeddings.embed_texts`. OpenAI remains an
LLM provider; it is no longer an embedding provider.

## What the vector store contains

Vectors are **ID-wise current projections**, not version-wise history. Each
graph entity `uid` owns one point/row. Re-embedding that UID overwrites both
named vectors. Source history remains in Postgres `record_versions`; temporal
fact history remains in FalkorDB and `fact_ledger`.

| Field | Meaning |
|---|---|
| `uid` | Stable graph entity and vector point identity |
| `label` | Entity kind/filter |
| `content` | Embedding of searchable content |
| `name` | Embedding of short name/path/title |
| `namespace_uid` | Workspace/project identity scope, when applicable |
| `embedded_model` | `BAAI/bge-m3` |
| `embedding_dimension` | 1024 (Qdrant payload; pgvector type enforces it) |
| `embedding_schema_version` | Current projection format, `2` |
| `embedded_content_hash` | SHA-256 of the content input |
| `embedded_text` | Short diagnostic preview, not source of truth |

Qdrant stores two named vectors on one point. Postgres uses the equivalent
primary key `(collection, uid)` with `content_embedding vector(1024)` and
`name_embedding vector(1024)`. Embedded labels are `WorkItem`, `Document`,
`Decision`, `Term`, `Api`, `Endpoint`, `PullRequest`, `Commit`, `SourceFile`,
`Finding`, and `Wisdom`.

## Canonical source record

Every connector emits the same `SourceRecord` before graph writes:

| Field | Purpose |
|---|---|
| `provider` | Jira, GitHub, Bitbucket, or Notion |
| `connection_id` | OAuth/App connection scope |
| `entity_type` | Provider-neutral record kind |
| `external_id` | Stable provider object identity |
| `name`, `content`, `url` | Canonical searchable record |
| `parent_external_id`, `breadcrumbs` | Source hierarchy |
| `created_at`, `updated_at` | Source timestamps |
| `mime_type`, `language` | Content hints |
| `metadata` | Provider-specific non-secret fields |
| `access` | Public flag, principals, policy version |

The record key is
`provider:connection_id:entity_type:external_id`. Content changes append a
`record_versions` row and update the current source/entity projection.

| Source | Incoming kinds | Graph nodes | Main relations |
|---|---|---|---|
| Jira | project, work item | Project, WorkItem, Person | BELONGS_TO, ASSIGNED_TO, REPORTED_BY, hierarchy/blocking facts |
| GitHub | repository, file, commit | Repository, SourceFile, Commit, Person | CONTAINS, AUTHORED_BY, MODIFIES |
| Bitbucket | repository, file, commit, pull request | Repository, SourceFile, Commit, PullRequest, Person | CONTAINS, AUTHORED_BY, MODIFIES |
| Notion | workspace, page | Workspace, Document | CONTAINS |

Free text can add evidence-backed `Decision`, `Term`, `System`, `Api`, and
`Endpoint` nodes. Their provenance still resolves through `SourceRecord`, so
ACL and lifecycle state are never trusted from vector metadata.

| Store | Responsibility |
|---|---|
| Postgres `source_records` | Current canonical source state |
| Postgres `record_versions` | Immutable source versions |
| Postgres `source_chunks` | Chunk state and 1024-dimensional evidence vectors |
| FalkorDB | Entities, facts, provenance, temporal traversal, ACL joins |
| Qdrant / `entity_embeddings` | Rebuildable current semantic projection |
| SQLite connector ledger | Incremental sync/checkpoint/review state |

OpenAI embedding thresholds are not portable to BGE-M3. Keep automated
resolution/relevance decisions in shadow or suggest mode until thresholds
are re-swept after re-ingestion.
