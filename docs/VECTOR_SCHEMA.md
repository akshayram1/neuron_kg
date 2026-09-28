# Neuron embedding and source schema

## Embedding contract

Neuron uses `BAAI/bge-m3` through a long-running Hugging Face Text Embeddings
Inference (TEI) Docker service. It exposes an OpenAI-compatible endpoint at
`http://localhost:8090/v1/embeddings`. An explicit
`EMBEDDING_BACKEND=local` fallback can load the same model in-process.

| Property | Value |
|---|---|
| Dense dimension | 1024 |
| Similarity | cosine |
| Normalization | L2 normalized at encode time |
| Maximum model input | 8192 tokens |
| Channels per entity | `content`, `name` |
| Vector schema version | 2 |

Every ingestion, semantic extraction, story-evidence, rebuild, and query path
calls `graph.embeddings.embed_texts`, which batches requests to that service.
OpenAI is used for extraction and answer synthesis, but no longer for embeddings.

Start only the embedding service with:

```bash
docker compose -f compose.embedder.yml up -d
```

The model cache is persisted in the `neuron-bge-m3-data` Docker volume. On an
x86_64 host set `TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`.

## What one vector record represents

The vector store is an **ID-wise current projection**, not a version store.
There is one point per graph entity `uid`. Re-embedding the same `uid`
overwrites its two vectors. Historical source versions remain in Postgres
`record_versions` and temporal facts remain in the graph/fact ledger; they do
not create another vector point unless a distinct historical entity UID is
created deliberately.

Each point/row contains:

| Field | Meaning |
|---|---|
| `uid` | Stable graph entity identity and vector point ID |
| `label` | Entity kind, used as a search filter |
| `content` vector | Embedding of the entity's searchable content |
| `name` vector | Embedding of its short name/path/title |
| `namespace_uid` | Workspace/project identity scope when applicable |
| `embedded_model` | `BAAI/bge-m3` |
| `embedding_dimension` | `1024` (Qdrant payload; fixed by pgvector type in Postgres) |
| `embedding_schema_version` | Projection schema version, currently `2` |
| `embedded_content_hash` | SHA-256 of the exact content-channel input |
| `embedded_text` | Small preview for diagnosis; it is not the source of truth |

Qdrant uses two named vectors on the point. Postgres uses the equivalent
`entity_embeddings(collection, uid)` primary key with
`content_embedding vector(1024)` and `name_embedding vector(1024)`.

Embedded labels are `WorkItem`, `Document`, `Decision`, `Term`, `Api`,
`Endpoint`, `PullRequest`, `Commit`, `SourceFile`, `Finding`, and `Wisdom`.
Structural nodes such as `SourceRecord`, `Person`, `Project`, `Repository`,
`Workspace`, and `System` remain searchable/traversable in the graph but do
not receive entity vectors.

## Canonical source record

All connectors first produce the same `SourceRecord` shape:

| Field | Type / purpose |
|---|---|
| `provider` | `jira`, `github`, `bitbucket`, or `notion` |
| `connection_id` | OAuth/App connection scope |
| `entity_type` | Provider-neutral record type |
| `external_id` | Provider's stable object identity |
| `name` | Human-readable title/path |
| `content` | Canonical text used for hashing/chunking/search |
| `url` | Original object URL, optional |
| `parent_external_id` | Parent object identity, optional |
| `breadcrumbs` | Ordered source hierarchy |
| `created_at`, `updated_at` | Source timestamps, optional |
| `mime_type`, `language` | Content hints, optional |
| `metadata` | Provider-specific non-secret fields |
| `access` | Public flag, principals, and policy version |

Its deterministic identity is
`provider:connection_id:entity_type:external_id`. Content changes create a
new Postgres `record_versions` row while the current `source_records` row and
the entity vector for its UID are updated.

## Provider mappings

| Source | Incoming entity types | Main graph nodes | Main structural relations |
|---|---|---|---|
| Jira | `project`, `work_item` | `Project`, `WorkItem`, `Person` | `BELONGS_TO`, `ASSIGNED_TO`, `REPORTED_BY`, hierarchy/blocking facts |
| GitHub | `repository`, `source_file`, `commit` | `Repository`, `SourceFile`, `Commit`, `Person` | `CONTAINS`, `AUTHORED_BY`, `MODIFIES` |
| Bitbucket | `repository`, `source_file`, `commit`, `pull_request` | `Repository`, `SourceFile`, `Commit`, `PullRequest`, `Person` | `CONTAINS`, `AUTHORED_BY`, `MODIFIES` |
| Notion | `workspace`, `page` | `Workspace`, `Document` | `CONTAINS` |

Free text can additionally produce evidence-backed `Decision`, `Term`,
`System`, `Api`, and `Endpoint` nodes and fact edges. These derived entities
retain links to their `SourceRecord`, so ACL and lifecycle checks still use
the authoritative source record rather than duplicated vector metadata.

## Storage responsibility

| Store | Responsibility |
|---|---|
| Postgres `source_records` | Current canonical record |
| Postgres `record_versions` | Immutable source-content history |
| Postgres `source_chunks` | Chunk state and 1024-dense evidence vectors |
| FalkorDB | Entities, facts, provenance, temporal traversal, ACL joins |
| Qdrant or Postgres `entity_embeddings` | Rebuildable current semantic-search projection |
| SQLite connector ledger | Incremental sync/checkpoint/review state |

Because vectors are a projection, changing the embedding model or dimension
requires clearing/recreating vector collections and re-ingesting or running
`scripts/rebuild_vectors.py --recreate`.
