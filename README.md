# Neuron

Neuron builds **one incremental company knowledge graph** from Jira, Bitbucket, GitHub and Notion, and answers questions over it with citations.

Short version: [`simple_readme.md`](simple_readme.md).

- **Graph:** FalkorDB.
- **Provenance:** every fact keeps its source records, its verbatim evidence and how it was derived.
- **Time:** every fact knows when it was true in the world and when Neuron learned it (two clocks).
- **Retrieval:** hybrid BM25 + dense vectors (local BGE-M3 on pgvector), an optional Laya reranker, and grounded LLM answers.
- **UI:** a FastAPI backend and a React/Cytoscape frontend to connect sources, browse the graph, review uncertain decisions and chat.

> **State of this document.** This README describes the code **as it is on disk on 2026-09-29, including the uncommitted working tree**. The last commit is `d021f41` (2026-09-28). On top of it, an uncommitted refactor moved the flat `graph/*.py` modules into `graph/{ingestion,resolution,retrieval,semantics,storage}/`. It also merged `triage.py` + `entity_resolution.py` + `fact_update_classifier.py` into `graph/ingestion/laya.py`, and re-enabled the LLM semantic pass for Jira and Bitbucket syncs. Many older docs in `docs/` still use the old flat paths (see [§17](#17-docs-index-and-which-ones-are-stale)).

## Glossary

Short definitions for project-specific words. Code-level detail lives in [§4 Core concepts](#4-core-concepts-read-this-first).

| Term | Meaning |
|---|---|
| **Neuron** | This system: ingest work tools → one company knowledge graph → cite answers. Also the default FalkorDB graph name (`neuron`). |
| **Laya** | A **local** trained classifier (not an LLM, no API cost). Used for chunk triage, same-entity checks, fact-update classification, retrieval reranking, and relation typing. Checkpoint usually under `model/laya-ingest/`. See [§6](#6-laya-the-local-classifier). |
| **Ledger** | Per-graph SQL source of truth beside FalkorDB: record hashes, chunks, which edges a record supports, drops, reviews, findings, axioms. Committed only after the graph write succeeds. |
| **Pass A** | Deterministic ingestion: nodes/edges from API fields and exact cross-source anchors. No model. |
| **Pass B / semantic pass** | Budgeted LLM extraction over pending chunks (`graph/ingestion/semantic_pass.py`), with Laya triage and admission gates. |
| **FalkorDB** | Property-graph database where live entities and temporal fact edges live (queried with Cypher). |
| **SourceRecord** | Canonical record every connector emits; also a graph node that facts cite for provenance. |
| **Chunk** | Content-keyed slice of a record’s text. Unchanged text keeps its id and prior extraction. |
| **Fact / fact edge** | A typed relationship on the graph with evidence, provenance, confidence, and two time clocks. |
| **Two clocks** | *World time* (when it was true) vs *record time* (when Neuron learned it). See [§8](#8-the-temporal-model). |
| **Named graph** | An isolated slug (`default`, `test_project`, …) with its own FalkorDB graph, vector collection, and ledger. |
| **Ontology** | Allowed entity kinds and (subject, relation, object) triples. Unknown relations are dropped or proposed for adoption. |
| **Axiom** | Semantic rule used with the ontology (e.g. direction / allowed shape of a relation). |
| **Finding** | Something the extraction LLM flags for a human (contradiction, architecture change, ambiguity). Comes from `assessments` in the same Pass B call — not a separate LLM call. See [§9.1](#91-findings-how-a-new-one-appears). |
| **Wisdom** | Intended durable guidance distilled from findings (Policy, Principle, …). Read path exists; write path is not wired yet. See [§9.2](#92-wisdom-read-path-exists-write-path-does-not). |
| **Review** | Human-in-the-loop queue item when automation is unsure (entity merge, fact update, ontology miss, …). Modes: *shadow* / *suggest* / *auto* or *enforce*. |
| **Link candidate** | Proposed edge between two existing nodes (often Laya `relation_type`), waiting for human accept/reject. |
| **Admission gates** | Checks a Pass B fact must pass before write: verbatim evidence, ontology-allowed relation, both endpoints resolved, conflict classified. |
| **BGE-M3** | Local embedding model (BAAI/bge-m3, 1024-dim) for vector search and cross-source candidate recall. |
| **RRF** | Reciprocal Rank Fusion: merges BM25 and dense-vector hit lists into one ranking at chat time. |
| **Test project** | Example named graph / local-data workspace used for demos and offline ingest (captured exports replayed by the synthetic loader). |

---

## Contents

0. [Glossary](#glossary)
1. [Architecture at a glance](#1-architecture-at-a-glance)
2. [Quick start](#2-quick-start)
3. [Repository layout](#3-repository-layout)
4. [Core concepts (read this first)](#4-core-concepts-read-this-first)
5. [Ingestion: from a source system to graph facts](#5-ingestion-from-a-source-system-to-graph-facts)
6. [Laya: the local classifier](#6-laya-the-local-classifier)
7. [Storage: graph, ledger, vectors](#7-storage-graph-ledger-vectors)
8. [The temporal model](#8-the-temporal-model)
9. [Findings and Wisdom](#9-findings-and-wisdom)
10. [Retrieval and answering (how the "agent" works)](#10-retrieval-and-answering-how-the-agent-works)
11. [Human-in-the-loop: reviews, link candidates, ontology adoption, hygiene](#11-human-in-the-loop-reviews-link-candidates-ontology-adoption-hygiene)
12. [HTTP API reference](#12-http-api-reference)
13. [Frontend](#13-frontend)
14. [Configuration reference](#14-configuration-reference)
15. [Scripts, evaluation and tests](#15-scripts-evaluation-and-tests)
16. [Known gaps and gotchas](#16-known-gaps-and-gotchas)
17. [Docs index (and which ones are stale)](#17-docs-index-and-which-ones-are-stale)

---

## 1. Architecture at a glance

```mermaid
flowchart LR
  subgraph UI["demo_ui/frontend (React 19 + Vite + Cytoscape)"]
    GC[GraphCanvas] --- CP[ChatPanel]
    CP --- EP[EntityPanel]
    CONN[Jira / GitHub / Bitbucket / Notion panels] --- LDP[LocalDataIngestPanel]
    BP["BridgePanel (reviews, link candidates, dashboard)"]
  end

  subgraph API["demo_ui/backend (FastAPI, :8000)"]
    APP["app.py: chat, graph, entities, ontology, config, graphs"]
    CR["*_routes.py: /api/connectors/{jira,github,bitbucket,notion}"]
    LD["synthetic_routes.py: /api/local-data"]
    RV["review / link-candidate / sync-coverage / dashboard routes"]
    JW["job_worker.py: leased job loop + nightly hygiene (in-process)"]
  end

  subgraph CONNS["connectors/"]
    CAPI["jira, github_app, bitbucket, notion: API + OAuth clients"]
    CORE["core: models, hashing, actions, runner, chunking, ledger, jobs, oauth_store"]
    SYN["synthetic/loader: replay of test-project local exports"]
  end

  subgraph G["graph/"]
    ING["ingestion: *_pipeline, semantic_pass, cross_source_context, laya, profiles, finding_bridge"]
    RES["resolution: resolver, anchors, resolve_text_fact, duplicate_collector, link_candidates, hygiene"]
    SEM["semantics: ontology, axioms, derived, inference, adoption, time_axis, fact_predicates"]
    RET["retrieval: chat, search, rerank, expand, structured_query, agentic_retrieval, entity, access"]
    STO["storage: writer, schema, falkor_client, multigraph, vector_store, embeddings"]
  end

  subgraph EXT["Runtimes"]
    FDB[(FalkorDB)]
    PG[("Postgres + pgvector")]
    SQ[("SQLite files (default SQL backend)")]
    OAI["OpenAI Responses API (LLM only)"]
    BGE["BAAI/bge-m3 (in-process, sentence-transformers)"]
    LAYA["laya package + local checkpoint model/laya-ingest"]
  end

  UI -->|HTTP /api| API
  CR -->|enqueue| JW
  JW -->|dispatch| CR
  CR --> CAPI --> CORE
  LD --> SYN
  CR --> ING
  LD --> ING
  ING --> RES --> SEM
  ING --> STO
  APP --> RET --> STO
  RV --> RES
  STO --> FDB
  STO --> PG
  CORE --> SQ
  CORE --> PG
  ING --> OAI
  RET --> OAI
  STO --> BGE
  ING --> LAYA
  RET --> LAYA
```

**Neuron in one paragraph.** A sync fetches records from a source API. Each record is turned into a canonical `SourceRecord`, hashed, and compared with the **ledger** to decide KEEP / INSERT / UPDATE. Changed records go through two passes:

- **Pass A** is deterministic. It writes structural nodes and edges from API fields, plus exact cross-source links such as Jira keys found in commit messages.
- **Pass B** is budgeted LLM extraction. It runs only on the text that Pass A could not already explain. Laya (a local classifier) triages chunks, a vector search nominates existing nodes from *other* sources, and the LLM extracts typed entities, facts and "assessments" under a strict schema. Every fact must pass admission gates before it is written: verbatim evidence, relation allowed by the ontology, both endpoints resolved, and conflicts classified.

Facts become FalkorDB edges with provenance and two clocks. Vectors are a rebuildable projection in pgvector. At question time, Neuron runs a deterministic structured-query lane, BM25 + vector RRF search, graph expansion and (optionally) Laya reranking. It then packs time-filtered evidence under a token budget, and asks the chat LLM for an answer that must cite its sources.

**What uses which model:**

| Work | Model |
|---|---|
| Pass A (structural writes, exact anchors) | none |
| Embeddings (write and query time) | local BGE-M3, 1024-dim |
| Chunk triage, same-entity check, fact-update check, rerank, relation type | local Laya checkpoint (not an LLM) |
| Entity/fact extraction | OpenAI `LLM_MODEL` via `responses.parse` + Pydantic schema |
| Answer synthesis | OpenAI `CHAT_MODEL` |
| Optional agentic sub-query planner | OpenAI `NEURON_AGENTIC_MODEL` (off by default) |

---

## 2. Quick start

### 2.1 Prerequisites

- Python **≥ 3.12** and [`uv`](https://docs.astral.sh/uv/)
- Node.js + npm (frontend)
- Docker (FalkorDB and Postgres + pgvector)
- An OpenAI API key (extraction and chat only; embeddings are local)
- Optional: a Laya checkpoint directory (for triage, reranking and resolution checks), usually `model/laya-ingest/`. It is gitignored.

### 2.2 Start the databases

No compose file is tracked any more; the old `compose.story.yml` was moved to `trash/story-removed/` when the story demo was removed. These commands reproduce what it ran:

```bash
docker run -d --name neuron-falkordb -p 6380:6379 -v neuron-falkor:/var/lib/falkordb/data falkordb/falkordb-server:latest
```

```bash
docker run -d --name neuron-postgres -p 55432:5432 -e POSTGRES_DB=neuron -e POSTGRES_USER=neuron -e POSTGRES_PASSWORD=neuron -v neuron-postgres:/var/lib/postgresql/data pgvector/pgvector:0.8.6-pg17-bookworm
```

Qdrant is a legacy vector backend. You only need it if you run without `DATABASE_URL`.

### 2.3 Configure

```bash
cp .env.example .env
```

At minimum, set these in `.env`:

| Variable | Value for the containers above |
|---|---|
| `FALKOR_HOST` / `FALKOR_PORT` | `localhost` / `6380` |
| `DATABASE_URL` | `postgresql://neuron:neuron@localhost:55432/neuron` |
| `VECTOR_BACKEND` | `postgres` (this is also the default when `DATABASE_URL` is set) |
| `NEURON_SQL_BACKEND` | `postgres` to keep the ledger/jobs/OAuth stores in Postgres, or leave unset for SQLite files in `DATA_DIR` |
| `OPENAI_API_KEY` | your key |
| `LLM_MODEL`, `CHAT_MODEL` | extraction and answer models |
| `LAYA_MODEL_DIR` | path to the Laya checkpoint (optional) |
| connector OAuth keys | only for the connectors you use (see [§14](#14-configuration-reference)) |

`.env.example` still mentions `compose.story.yml`, `scripts/run_story_demo.sh` and `STORY_*` variables. They are dead; ignore them.

Generate a Fernet key for a `*_TOKEN_ENCRYPTION_KEY`:

```bash
uv run python -m connectors.notion.oauth --generate-key
```

### 2.4 Install and run

```bash
uv sync
```

```bash
cd demo_ui/frontend && npm install
```

Start backend (`:8000`) and frontend (`:5173`, proxies `/api` to `:8000`) together:

```bash
./scripts/dev.sh
```

Or start the backend alone:

```bash
uv run uvicorn demo_ui.backend.app:app --reload --port 8000
```

On startup the backend does the following:

- bootstraps the FalkorDB indexes
- restores the saved Laya toggle
- warms the BGE-M3 model in a background thread
- starts the **job worker** and the **nightly hygiene scheduler** inside the same process (there is no separate worker command)

If `demo_ui/frontend/dist` exists, it is served at `/`.

### 2.5 Get data in

- **Real sources:** open the UI, connect Jira / GitHub / Bitbucket / Notion through OAuth, pick a project, repo or workspace and press sync. Syncs are queued; progress shows per run.
- **Local replay (no credentials):** the **Local data** panel ingests captured test-project exports from `$SYNTHETIC_DATA_DIR` through the same pipeline code (`POST /api/local-data/{jira,bitbucket,notion}/ingest`).
- **Isolated graphs:** use the graph selector to create a named graph (for example `test_project`). Each name gets its own FalkorDB graph, vector collection and ledger (see [§7.1](#71-storage-topology-and-named-graphs)).

### 2.6 Run the tests

```bash
uv run pytest tests
```

By default tests are pinned to SQLite with `NEURON_RERANK=off`. For live FalkorDB, Qdrant and Postgres tests, and the Postgres variant of the suite, see [§15.3](#153-tests). Run pytest on `tests/`, not the repo root. Root collection can also pick up vendored code under the local test-project export tree — point pytest at `tests/` only.

---

## 3. Repository layout

```
neuron/
├── connectors/                 # Everything that talks to a source system
│   ├── core/                   #   models, hashing, actions, runner, text, ledger, jobs, oauth_store, purge
│   │   └── chunking/           #   router, semantic chunker, tree-sitter code chunker (code_parser/, PipesHub-derived), tokens
│   ├── jira/                   #   Atlassian OAuth 3LO + read-only issue/changelog client
│   ├── bitbucket/              #   per-user OAuth + rate-limited REST client
│   ├── github_app/             #   GitHub App installation auth + client + store
│   ├── notion/                 #   Notion OAuth + async reader (notion.py is legacy, unused)
│   ├── synthetic/loader.py     #   replays test-project local exports as provider dataclasses
│   └── ingest/pdf_loader.py    #   orphan: no caller, undeclared dependency
├── graph/
│   ├── ingestion/              # Pass A writers per provider, semantic_pass (Pass B), cross_source_context,
│   │                           # laya adapters, profiles (prompts + schemas), selective_ingestion, dates, finding_bridge
│   ├── resolution/             # anchors, resolver (exact cross-source links), resolve_text_fact (classify before write),
│   │                           # duplicate_collector, link_candidates (+ path support), hygiene
│   ├── semantics/              # ontology, axioms, derived rules, inference (unused), adoption, time_axis,
│   │                           # fact_predicates, freshness (unused), skos_export
│   ├── retrieval/              # chat (answer pipeline), search (BM25+vector RRF), rerank (Laya), expand,
│   │                           # structured_query, text_window, agentic_retrieval, entity, history, graph_view, access, wisdom
│   ├── storage/                # writer (Cypher), schema (indexes), falkor_client, multigraph, vector_store, embeddings
│   └── token_usage.py
├── storage/                    # sql_backend.py (SQLite/Postgres shim), postgres.py (pgvector store + dead story-era schema)
├── util/                       # paths.py (.env + DATA_DIR), logging.py, flow_log.py (stage banners)
├── demo_ui/backend/            # FastAPI app + routers + job_worker + access
├── demo_ui/frontend/           # React 19 / Vite 7 / TypeScript / Cytoscape UI
├── scripts/                    # eval, migrations, rebuilds, backups, CLI sync
├── eval/                       # golden sets + results.md
├── tests/                      # ~70 test files
├── docs/                       # design docs (many partly stale, see §17)
├── model/laya-ingest/          # local Laya checkpoint (gitignored)
├── (local test-project export tree)  # captured Jira/Bitbucket/Notion replay data; gitignored; path from SYNTHETIC_DATA_DIR
├── services/embedder/          # empty directory, unused
├── trash/, backups/            # removed code and old data; ignore
└── VECTOR_SCHEMA.md            # embedding contract (matches code; docs/VECTOR_SCHEMA.md does not)
```

---

## 4. Core concepts (read this first)

| Concept | What it means in code |
|---|---|
| **SourceRecord** | The canonical record every connector emits (`connectors/core/models.py`). Connectors never build graph nodes or prompts. It also exists as a `:SourceRecord` node in FalkorDB. |
| **record_key** | `provider:connection_id:entity_type:external_id`, e.g. `jira:<conn>:work_item:<cloud_id>:<issue_id>`. It identifies a record everywhere: ledger, graph and citations. |
| **content hash** | sha256 of a canonical JSON of the record (text normalised, metadata sanitised, access policy included). The same hash means KEEP: nothing is re-processed. Metadata keys that look like secrets make hashing raise. |
| **Ledger** | Per-graph SQL store (`connectors/core/ledger.py`) holding record hashes, chunks, versions, which edges each record supports, extraction drops, reviews, findings, axioms and more. It is a source of truth alongside FalkorDB, and it is committed **only after** the graph write succeeds. |
| **Chunk** | A piece of record text. The id is `uuid5(record_key : sha256(text) : occurrence)`, so it is **content-keyed**: unchanged text keeps its id and its extraction. Removed chunks are soft-superseded, never deleted, because facts cite them. |
| **Pass A** | Deterministic writes from API fields: nodes, structural edges, exact-anchor cross-source edges and small derivation rules. No model is involved. |
| **Pass B** | `semantic_pass.run_semantic_pass`: budgeted, concurrent LLM extraction over *pending* chunks, with admission gates. |
| **Fact edge** | Every relationship is an edge carrying `fact_uid`, `source_record_keys[]`, `evidence`, `extraction_method`, `confidence`, and world time (`valid_at` / `invalid_at`) plus record time (`first_seen_at` / `last_confirmed_at`). |
| **Two clocks** | *World time:* when the fact was true. *Record time:* when Neuron knew it. Queries can ask either ([§8](#8-the-temporal-model)). |
| **Live fact** | `invalid_at IS NULL`, not `corrected`, and `projection_status = 'live'` (`graph/semantics/fact_predicates.py`). Facts waiting on a review are stored as `pending_review` and are not live. |
| **Named graph** | A slug (`default`, `test_project`, …) mapped to its own FalkorDB graph, vector collection and ledger file or schema (`graph/storage/multigraph.py`). |
| **Laya** | A local trained multi-question classifier (PyPI `laya` + a checkpoint). It is used for triage, entity matching, fact-update classification, relevance reranking and relation typing. It is not an LLM ([§6](#6-laya-the-local-classifier)). |
| **Modes** | Uncertain automation defaults to *shadow* or *suggest*: it records or proposes, and a human approves in the review queue. `NEURON_TRIAGE=shadow`, `NEURON_RESOLVE_MODE=suggest`, `NEURON_FACT_UPDATE_MODE=suggest`. |

---

## 5. Ingestion: from a source system to graph facts

### 5.1 End-to-end sequence of one sync

```mermaid
sequenceDiagram
    autonumber
    participant UI as Browser UI
    participant R as <provider>_routes (POST /sync)
    participant OS as OAuth / provider store (runs)
    participant Q as ConnectorJobStore (connector_jobs)
    participant W as job_worker.run_worker
    participant S as <provider>_routes._run / _run_sync
    participant API as Provider API client
    participant P as <provider>_pipeline.write_*
    participant RN as runner.prepare_record
    participant L as ConnectorLedger
    participant G as FalkorDB + vectors
    participant SP as semantic_pass.run_semantic_pass

    UI->>R: POST /api/connectors/{p}/sync
    R->>OS: create_run (409 if a run for this source is already active)
    R->>Q: enqueue(run_id, provider, payload)
    R-->>UI: 202 {run_id}
    W->>Q: claim() -> running, attempts+1, lease 3600 s (heartbeat every 30 s)
    W->>S: dispatch(job)
    S->>API: fetch (refresh token once on 401)
    S->>G: resolve named graph, bootstrap_schema, ensure_collection, open embedding batch
    loop every record (container first: project / repo / workspace)
        S->>P: write_*(record)
        P->>RN: prepare_record: hash, ledger.plan -> INSERT / UPDATE / KEEP, chunk if changed
        alt KEEP
            P-->>S: nothing to do
        else INSERT / UPDATE
            opt UPDATE
                P->>G: remove this record's support from its old edges (record_edges)
            end
            P->>G: Pass A: SourceRecord, entities, MENTIONED_IN, structural edges, exact anchors, derived rules
            P->>G: embed now (batched BGE-M3)
            P->>P: selective_chunk_writes: drop text already explained by exact anchors
            P->>L: save_chunks (content-keyed diff), record_edges_batch, commit(hash, status)
        end
    end
    S->>G: close_batch (flush vectors so the next step can find them)
    S->>SP: run_semantic_pass(record_prefix, context_provider)  [Jira, Bitbucket, Notion]
    SP->>L: pending_chunks(LLM_BUDGET_PER_RUN) by priority
    SP->>G: gated entity + fact writes, drops, reviews, findings
    S->>G: finding_bridge.sync_ledger_findings, delete_orphaned_shared_entities
    S->>L: record_sync_coverage
    S->>OS: set_run(completed)
    W->>W: run_hygiene_for_graph
    W->>Q: complete (on error: fail -> retry with backoff, or dead)
```

### 5.2 Job queue (`connectors/core/jobs.py`, `demo_ui/backend/job_worker.py`)

- **Asynchronous by design.** Sync routes only enqueue a job and return 202.
- **Table:** `connector_jobs` (in `connector_jobs.sqlite3`, or its Postgres schema).
- **States:** `queued → running → completed | retry | dead`.
- **Claiming:** `claim()` first recovers expired leases, then picks the oldest claimable job. SQLite uses `BEGIN IMMEDIATE`; Postgres uses `FOR UPDATE SKIP LOCKED`.
- **Leases:** 3600 s, with a heartbeat every 30 s.
- **Retries:** `fail()` backs off `min(300, 2^(attempts-1))` seconds; `retry_after_seconds` can push that up to 3600. Default `max_attempts` is 3; Bitbucket uses 10.
- **Rate limits:** a Bitbucket 429 with a long `Retry-After` raises `BitbucketRateLimited`, which becomes a delayed retry rather than a failure.
- **Hygiene:** after every successful job the worker runs `run_hygiene_for_graph`. The scheduler repeats it for every graph every `NEURON_HYGIENE_INTERVAL_S` (default 86400).
- **Exceptions:** local-data ingest (`/api/local-data/...`) runs synchronously with no queue. `scripts/sync_jira.py` is a CLI that runs Pass A only.

### 5.3 Canonical record → hash → action → chunks (`connectors/core/`)

1. **`SourceRecord`** holds identity (provider, connection_id, entity_type, external_id), name, content, url, parent, breadcrumbs, timestamps, mime, language, metadata and `SourceAccess` (public, principals, policy_version).
2. **`record_content_hash`** (`hashing.py`) normalises text (NFC, LF, no NUL), then hashes identity, content, metadata and access. Bumping `pipeline_version` in metadata forces an UPDATE for every record of that type.
3. **`resolve_action`** (`actions.py`) returns INSERT (no previous hash), KEEP (same hash), UPDATE (different hash) or DELETE.
4. **`prepare_record`** (`runner.py`) chunks only INSERT and UPDATE records: **hash first, then chunk**.

**Chunking** (`connectors/core/chunking/`):

- **Policy per profile** (`graph/ingestion/profiles.py`). Overlap is always 0; nonzero overlap raises.

  | Profile | Source | target / hard max tokens |
  |---|---|---|
  | `WORK_MANAGEMENT` | Jira | 1500 / 3500 |
  | `SOFTWARE_KNOWLEDGE` | GitHub, Bitbucket | 1700 / 3500 |
  | `BUSINESS_DOCUMENT` | Notion | 1800 / 3800 |

- **Prose** (`semantic.py`): paragraphs, then sentences when a paragraph is too big; units are greedily packed to the target, then token-split above the hard max. Tokens are counted with tiktoken `cl100k_base`.
- **Code** (`code.py` + `code_parser/`, adapted from PipesHub, Apache-2.0):
  - tree-sitter parses 18 languages;
  - only **comments and docstrings** are sent, each prefixed `# {kind} {qualified.name}`;
  - symbols without comments contribute nothing;
  - unknown languages and files over 5 MiB fall back to a raw token split.
  - `.md` files in repos are routed as code and take the token fallback, **not** heading-aware chunking.

### 5.4 Pass A: deterministic writes per source

Every Pass A writer follows the same shape (`graph/ingestion/*_pipeline.py`):

1. `upsert_source_records` → `upsert_entities` → `link_mentioned_in`.
2. Structural edges with `extraction_method="deterministic"`, `confidence=1.0`, and `valid_at = record.updated_at or created_at`.
3. `resolve_backlinks_for_target`: earlier records whose text mentioned this newly arrived target get their edge now.
4. `resolve_exact_anchors`: cross-source links from this record's text.
5. `derived.materialize_around`: small named derivation rules.
6. `ledger.record_edges_batch`: remember which edges this record supports.
7. `_embed_now`: write-time vectors.
8. `selective_chunk_writes`, then `save_chunks`, then `commit`.

On **UPDATE**, `_reset_record_support` first removes this record's key from the `source_record_keys` of every edge it supported. An edge left with no supporting record is closed and archived.

**What each source produces:**

| Source | Records (`entity_type`) | Nodes | Edges |
|---|---|---|---|
| Jira | `project`, `work_item` | Project, WorkItem, Person | `BELONGS_TO`, `ASSIGNED_TO`, `REPORTED_BY` (single-valued: supersede then upsert), `PARENT_OF` (**child → parent**), `BLOCKS`. The changelog becomes `FactHistory` intervals for `ASSIGNED_TO` and `HAS_STATUS`. |
| Bitbucket | `repository`, `source_file`, `commit`, `pull_request` | Repository, SourceFile, Commit, PullRequest, Person | `CONTAINS`, `AUTHORED_BY`, `MODIFIES` (Commit → SourceFile, with diffstat evidence) |
| GitHub | `repository`, `source_file`, `commit` | Repository, SourceFile, Commit, Person | `CONTAINS`, `AUTHORED_BY`. No PRs; **no chunks, no Pass B**. |
| Notion | `workspace`, `page` | Workspace, Document | `CONTAINS` Workspace → Document, `PARENT_OF` (**parent → child**) |

**Connector notes:**

- **Jira:**
  - Atlassian OAuth 3LO; JQL search with changelog expanded.
  - Every sync re-fetches the whole project (or subtree); unchanged issues are KEEP by hash.
  - There are no webhooks or cursors, and issues deleted in Jira are not detected.
- **Bitbucket:**
  - Per-user OAuth; the workspace slug is typed in by the user.
  - Recursive `src` walk; only `.py` and `.md` files, under `BITBUCKET_MAX_FILE_BYTES`.
  - Commits (capped by `BITBUCKET_MAX_COMMITS_PER_SYNC`) with diffstats, and PRs.
  - Requests are sequential and paced by default.
  - `reconcile_repository_records` hard-deletes records not seen in this run.
- **GitHub:** GitHub App installation token; default branch only; `.py`/`.md` files; commits.
- **Notion:**
  - Public OAuth integration; `/search`, then recursive block children rendered to markdown; child pages and databases are followed.
  - Pages removed from Notion only flip `notion_pages.is_active`; graph deletes are never propagated.
- **Local replay** (`connectors/synthetic/loader.py`): reads `$SYNTHETIC_DATA_DIR/*_real/` (test-project capture) and returns the same dataclasses the live clients return, so the same writers run. Connection ids are fixed per provider for the local-data ingest path.

### 5.5 Exact cross-source anchors and derived rules (`graph/resolution/`, `graph/semantics/derived.py`)

- **`anchors.py`** extracts these with regexes: Jira keys (`[A-Z][A-Z0-9]{1,11}-\d+`), URLs, repository names, commit SHAs and PR refs. They are stored on the `SourceRecord` as `anchor_*` arrays.
- **`resolver.resolve_exact_anchors`** matches anchors to nodes from **other** providers:
  - Commit/PR → WorkItem gives `IMPLEMENTS`;
  - Document → WorkItem/Commit/PR/Repository/SourceFile gives `DOCUMENTS`;
  - anything else gives `REFERENCES`.
  - Edges carry `extraction_method="exact_anchor"`, `confidence=1.0` and an evidence excerpt.
- **`link_verified_person_identity`** writes `SAME_AS` Person ↔ Person when emails match exactly (`derived_rule="verified_email"`).
- **`derived.materialize_around`** holds hand-written rules with `premise_fact_uids` and confidence 0.6:
  - `parent_implements` / `parent_documents` lift IMPLEMENTS/DOCUMENTS through PARENT_OF;
  - `pr_implements`: Document DOCUMENTS PR, and that PR IMPLEMENTS WorkItem, gives Document DOCUMENTS WorkItem;
  - `shared_concept` only proposes a link candidate; it never writes an edge.

### 5.6 Selective ingestion: send the LLM only what is unexplained (`graph/ingestion/selective_ingestion.py`)

`selective_chunk_writes` splits each chunk into sentence or line units. It removes a unit when all of these hold:

- the unit contains an anchor that Pass A already linked;
- it contains a relation word (ticket, implements, fixes, PR, commit…);
- it contains no semantic cue (because, decided, migrate, deprecate, must, should…);
- it is 24 words or fewer.

Outcome per chunk:

- **Nothing left:** the chunk is saved `DONE` with `resolution_status="deterministic"`.
- **Otherwise:** it is saved `PENDING` with `llm_text` = source header + the remaining units. **This is what the LLM sees.**

The record is committed `semantic_status=pending`, or `not_applicable` when nothing is pending.

### 5.7 Pass B: the semantic pass (`graph/ingestion/semantic_pass.py`)

```mermaid
flowchart TD
  A["ledger.pending_chunks(LLM_BUDGET_PER_RUN, record_prefix)<br/>ordered by semantic_priority DESC — changed records first"]
  A --> B["Gate 1: Laya triage — NEURON_TRIAGE<br/>shadow = record only; enforce = drop noise/scheduling/low-durability"]:::laya
  B --> C["cross_source_context: embed chunk → vector pool x4 → keep nodes from OTHER providers<br/>→ Laya rerank → top 7 → candidate UID allow-list"]:::laya
  C --> D["LLM: responses.parse — LLM_MODEL + profile.instructions + chunk llm_text + candidates<br/>→ WorkManagementExtraction: terms, decisions, systems, apis, endpoints, facts, assessments"]:::llm
  D --> E["Pre-filter: keep only entities used by a valid fact; drop generic mentions"]
  E --> F["Entity resolution ladder — 6 rungs → uid, or a review"]:::laya
  F --> G["Gate 2: evidence verbatim in chunk"]
  G --> H["Gate 3: relation allowed by ontology/axioms; auto-swap wrong direction"]
  H --> I["Resolve both endpoints: candidate UID, scoped key, record node, person/structure lookup"]
  I --> J["Classify before write: find_conflict_candidates → Laya fact_update → resolve_text_fact"]:::laya
  J --> K["upsert_fact_edges revive=False / reviews / extraction_drops / ingestion_findings"]
  K --> L["record fully processed → semantic_status=done, re-embed the record node"]
  classDef llm fill:#ffe0b3,stroke:#c77700
  classDef laya fill:#dfe9ff,stroke:#3a5fcd
```

(Orange = LLM call. Blue = Laya and/or vector similarity. Everything else is deterministic Python or Cypher.)

**Run settings:**

- `LLM_MODEL` (default `gpt-5.6-luna`)
- `LLM_BUDGET_PER_RUN`: maximum chunks per run, default 200
- `LLM_CONCURRENCY`: default 6

LLM calls run in a thread pool. **All writes happen serially** on the main thread, in completion order.

**Prompt and schema** (`graph/semantics/ontology.py`, `graph/ingestion/profiles.py`):

- **Base prompt:** `EXTRACTION_INSTRUCTIONS`, plus a per-profile addendum and an adjudication addendum. The rules are: prefer few entities, never invent types, no outside knowledge, quote evidence verbatim, and treat candidate UIDs as untrusted.
- **Output schema:** `WorkManagementExtraction`, shared by all profiles:
  - **Entities:**
    - `Term{name, definition, aliases}`
    - `Decision{name, statement, rationale, status}`
    - `System{name, purpose}`
    - `Api{name, version, status}`
    - `Endpoint{name, method, path}`
  - **`ExtractedFact`:** `subject_name/kind`, `relation`, `object_name/kind`, optional `subject_candidate_uid` / `object_candidate_uid`, and verbatim `evidence`.
  - **`IngestionAssessment`:** `action` ∈ {addition, update, contradiction, architecture_change, review}, `topic_key`, `should_flag`, `severity`, `title`, `summary`, `reasoning`, `evidence`, `related_candidate_uids`, `confidence`. Assessments become Findings ([§9](#9-findings-and-wisdom)).
- **Cross-source context** (`cross_source_context.py`) appends a `[RELATED EXISTING EVIDENCE + CANDIDATE NODES]` block. The LLM may link to an existing node only by quoting one of those UIDs, and only when the new text supports it. This is how a Notion page gets linked to a Jira ticket it never names exactly.

**Entity resolution ladder** (`_resolve_semantic_entity`). Each rung is tried in order; the first hit wins.

| Rung | Rule |
|---|---|
| 1. Mention filter | Terms/Systems shorter than 3 chars, or stoplisted, are dropped (`generic_mention`). |
| 2. Scoped exact | Same scoped key already seen in this run or present in the graph. |
| 3. Alias | `entity_aliases` lookup (created when a human approves a merge). |
| 4. Vector ≥ 0.90 | Within the namespace. Exactly one hit with no polarity conflict → merge. A negation mismatch on a Decision creates a `polarity_conflict_candidate` review. |
| 5. Vector 0.75–0.90 | Gray zone; hits are collected as candidates. |
| 6. Laya `same_entity` | Accept when p ≥ 0.85 and margin ≥ 0.15. In `suggest` mode (default) this creates a `possibly_same_as` review and mints a new uid. `auto` merges, except Decisions, which are always reviewed. |

Otherwise a new node is minted.

**Scoped identity** keeps different projects from colliding:

| Label | UID |
|---|---|
| `Decision` | `make_uid("Decision", record_key, normalized_statement)` (record-scoped) |
| `Term`, `System` | `make_uid(label, namespace_uid, normalized_name)`. The namespace is the owning Project / Repository / Workspace. |
| `Api`, `Endpoint` | global by name |

**Admission outcomes** are recorded per chunk in `extraction_drops`:

| Reason code | Meaning |
|---|---|
| `evidence_not_in_chunk` | Quoted evidence is not a verbatim substring of the chunk. |
| `relation_not_allowed` | (subject kind, relation, object kind) is not in the ontology. It is also counted in `ontology_misses` for adoption. |
| `direction_corrected` | Written, with subject and object swapped. |
| `endpoint_unresolved` | An endpoint could not be resolved to a node. |
| `entity_no_connecting_fact` | The entity was not used by any valid fact. |
| `generic_mention` | Stoplisted or too-short Term/System. |
| `laya_triage_skip` | The chunk was dropped by triage in enforce mode. |
| `classifier_failed` | The fact-update classifier failed on a cross-source candidate link. |

**Every entity** also gets:

- an `EXTRACTED_FROM` lineage edge to the record's own node;
- `MENTIONED_IN` to the SourceRecord;
- content + name vectors.

**LLM facts** carry `extraction_method="llm"`, `confidence=0.9`, `chunk_id`, `chunk_hash`, `model` and `extractor_version`. `valid_at` is the date stated in the evidence (`dates.stated_dates`, `valid_at_basis="stated"`), otherwise the record time.

### 5.8 Which sources run the LLM today

| Path | Pass A | Chunks saved | Pass B in the same run |
|---|---|---|---|
| Jira OAuth sync | yes | yes | **yes**, with cross-source context |
| Bitbucket OAuth sync | yes | yes | **yes**, with cross-source context |
| GitHub sync | yes | **no** | **no** (`NOT_APPLICABLE`) |
| Notion OAuth sync | yes | yes | **yes**, then ontology adoption |
| Local Jira / Bitbucket replay | yes | yes (left pending) | **no** |
| Local Notion replay | yes | yes | yes, **without** cross-source context |
| `scripts/sync_jira.py` | yes | yes (left pending) | no |

`docs/Jira.md`, `docs/Bitbucket.md`, `synthetic_routes.py` and `scripts/sync_jira.py` still say "Jira/Bitbucket: no LLM". The table above is what the code does now.

---

## 6. Laya: the local classifier

Laya is a trained, local, multi-question classifier.

- **Loading:** the PyPI package `laya` loads a checkpoint as `laya.Agent(LAYA_MODEL_DIR, device=LAYA_DEVICE)`. The checkpoint must contain `model.safetensors`, `questions.json` and `rl_agent_config.json`.
- **Adapters:** each adapter loads lazily and holds a thread lock. It **refuses to run** unless the checkpoint's `questions.json` entry for its question matches the hard-coded question exactly.
- **Model version:** the first 12 hex characters of `sha256(questions.json [+ rl_agent_config.json])`.
- **Not an LLM:** there is no API call and no cost per call.

| Head (question) | Where | Input | Output and decision |
|---|---|---|---|
| `chunk_type` + `has_durable_fact` (**triage**) | `graph/ingestion/laya.py` `LayaTriageClassifier`; gate 1 of the semantic pass | `{source, text[:1600]}` | type ∈ {decision, action_item, status_update, fact_statement, request, scheduling, discussion, noise} + p(durable). `would_skip` if the type is noise/scheduling, or discussion with p < 0.3, or p < 0.15. `shadow` only records (`source_chunks.triage_*`); `enforce` drops. |
| `same_entity` | `LayaSameEntityClassifier`; rung 6 of the resolution ladder | mention + context, candidate + profile | p(same). Accept at ≥ 0.85 with margin ≥ 0.15 (review in suggest mode). |
| `fact_update` | `LayaFactUpdateClassifier`; `resolve_text_fact.classify_fact_update` | existing fact + its valid_from, new evidence + timestamp | {duplicate, updates→newer_state, contradicts, extends, unrelated} |
| `retrieval_relevance` (**rerank**) | `graph/retrieval/rerank.py` `LayaReranker`; chat and ingest-time cross-source context | question + node text window (300 tokens) | p(needed to answer) → roles direct / temporal / bridge / irrelevant |
| `relation_type` | `graph/resolution/link_candidates.py` `LayaRelationClassifier` | two node windows | {owns, part_of, blocks, references, …, none}; proposes a link candidate at confidence ≥ 0.6 |

**Defaults differ between ingestion and chat:**

- Ingestion-time reranking (`NEURON_INGEST_RERANK`) falls back to `NEURON_RERANK`, then to `laya`.
- Chat reranking (`NEURON_RERANK`) defaults to `off`. It can be toggled at runtime with `PUT /api/config/reranker` or the UI switch, and the choice is persisted.

Latency: `scripts/benchmark_laya.py`. Measured effect: `eval/results.md`. On one 44-question set, Laya raised final recall from 0.09 to 0.25 and chain coverage from 0.07 to 0.27, at roughly 4× the latency.

---

## 7. Storage: graph, ledger, vectors

### 7.1 Storage topology and named graphs

```mermaid
flowchart LR
  subgraph Named["Graph slug -> 3 physical names (graph/storage/multigraph.resolve)"]
    N1["'default' -> FalkorDB 'neuron', vectors 'neuron_entities', ledger connector_ledger.sqlite3"]
    N2["'<slug>' -> 'neuron__<slug>', 'neuron_entities__<slug>', connector_ledger__<slug>.sqlite3"]
  end
  subgraph F["FalkorDB"]
    F1["Property graph per slug: nodes, temporal fact edges, FactHistory;<br/>range indexes on uid / record_key; fulltext on search_text"]
  end
  subgraph V["Vector projection (VECTOR_BACKEND)"]
    V1["postgres: entity_embeddings(collection, uid) content + name vector(1024), HNSW cosine"]
    V2["qdrant (legacy): named vectors content + name"]
  end
  subgraph S["Operational SQL (NEURON_SQL_BACKEND = sqlite | postgres)"]
    S1["connector_ledger[__slug]: records, chunks, versions, edges, drops, axioms, reviews, findings, ..."]
    S2["connector_jobs"]
    S3["oauth_connectors / github_connector / notion_connector (encrypted tokens, runs)"]
    S4["graphs (registry), runtime_settings"]
  end
  Named --> F
  Named --> V
  Named --> S
```

- **Sources of truth:** the **ledger** and **FalkorDB**. **Vectors are a rebuildable projection** (`scripts/rebuild_vectors.py`).
- **Slug rule:** `^[a-z0-9][a-z0-9_-]{0,39}$`.
- **OAuth connections are not per-graph.** One connection can sync into any graph.
- **SQL backend** (`storage/sql_backend.py`): every operational store opens through one factory.
  - **SQLite** (default) keeps files in `DATA_DIR` (default: the repo root).
  - **Postgres** (`NEURON_SQL_BACKEND=postgres`) maps each file stem to its own **Postgres schema**, e.g. `connector_ledger__test_project`. The shim translates `?` placeholders and SQLite DDL types, emulates `PRAGMA table_info`, and wraps writes in savepoints.
  - Move existing data with `scripts/migrate_sqlite_to_postgres.py`. It is a dry run unless `--apply` is given.

### 7.2 FalkorDB graph model

**Node labels.** Every entity has `uid`, `first_seen_at`, `last_seen_at` and (for content labels) `search_text`. Provenance is `(entity)-[:MENTIONED_IN]->(:SourceRecord)`.

| Label | Written by | Identity | Notable properties |
|---|---|---|---|
| `SourceRecord` | every Pass A writer | `record_key` | provider, connection_id, entity_type, external_id, name, url, content_hash, public, principals, policy_version, source_time, `anchor_*` arrays, ingested_at, deleted_at |
| `Project` | Jira | `make_uid("Project","jira",conn,ext)` | name, search_text, url |
| `WorkItem` | Jira | `make_uid("WorkItem","jira",conn,cloud:issue_id)` | name, search_text, url, status, issue_type, labels, issue_key |
| `Person` | Jira, GitHub, Bitbucket | account id / author email | name, email |
| `Repository`, `SourceFile`, `Commit`, `PullRequest` | GitHub, Bitbucket | connection + repo + path / sha / PR id | path, language, blob_sha, sha, authored_at, pr_ref, state, branches, default_branch, private |
| `Workspace`, `Document` | Notion | workspace id (+ page id) | name, search_text, url, last_edited_time |
| `Decision` | LLM | record-scoped (see [§5.7](#57-pass-b-the-semantic-pass-graphingestionsemantic_passpy)) | name, statement, rationale, status, namespace_uid |
| `Term`, `System` | LLM | namespace-scoped | definition, aliases / purpose, namespace_uid |
| `Api`, `Endpoint` | LLM | global by name | version, status / method, path |
| `Finding` | `finding_bridge` (from the ledger) | `make_uid("Finding", finding_key)` | kind, status, severity, confidence, title (name), summary, reasoning, created_at, stale_at |
| `Wisdom` | **nothing at runtime** (read only; see [§9.2](#92-wisdom-read-path-exists-write-path-does-not)) | — | status, statement, rationale, recommended_action, wisdom_type |
| `FactHistory` | writer (close/supersede/changelog) | (from, rel, to, valid_from, valid_to) | fact_uid, relation, valid_from/to, observed_from/to, evidence, extraction_method, close_reason, assertion_status, … |

**Edge types:**

| Family | Relations | Written by |
|---|---|---|
| Provenance | `MENTIONED_IN` entity → SourceRecord (no properties) | every writer |
| Lineage | `EXTRACTED_FROM` semantic entity → the record's own node | semantic pass |
| Structural, Jira | `BELONGS_TO`, `ASSIGNED_TO`, `REPORTED_BY` (functional), `PARENT_OF` (child → parent), `BLOCKS`; `HAS_STATUS` only in FactHistory | Pass A |
| Structural, code | `CONTAINS` Repository → SourceFile/Commit/PR, `AUTHORED_BY`, `MODIFIES` (Bitbucket) | Pass A |
| Structural, Notion | `CONTAINS` Workspace → Document, `PARENT_OF` (parent → child) | Pass A |
| Exact cross-source | `IMPLEMENTS`, `DOCUMENTS`, `REFERENCES`; `SAME_AS` Person ↔ Person (verified email) | resolver |
| Semantic (LLM) | `DEFINES`, `APPLIES_TO`, `CAVEAT_OF`, `DECIDED_BY`, `SUPERSEDES`, `OWNS`, `PROVIDES_API`, `CONSUMES_API`, `EXPOSES_ENDPOINT`, `CALLS_ENDPOINT`, `CHANGES`, `DEPRECATES`, `MIGRATES_TO` (+ IMPLEMENTS / DOCUMENTS / REFERENCES between allowed pairs) | semantic pass, whitelisted by `RELATION_TYPE_MAP` + adopted axioms |
| Conflict | `DISPUTED_WITH` Decision ↔ Decision | resolve_text_fact |
| Derived | IMPLEMENTS / DOCUMENTS via `derived.py` (conf 0.6); approved link candidates (REFERENCES / OWNS / PARENT_OF / BLOCKS) | derived rules, reviews |
| Declared, never written | `HAS_REPOSITORY`, `HAS_DOCUMENT`, `MAY_IMPACT`, `DERIVED_FROM` | — |

**Fact-edge properties** (`graph/storage/writer.py::upsert_fact_edges`):

- **Identity:** `fact_uid = uuid5("Fact", from, rel, to)`.
- **Provenance:** `source_record_keys[]`, `evidence` (verbatim), `extraction_method` (`deterministic` / `exact_anchor` / `changelog` / `llm` / `derived`), `confidence`, `chunk_id`, `chunk_hash`, `extractor_version`, `model`, `attested_by_record`, `adopted_batch`.
- **World time:** `valid_at`, `valid_at_basis` (`stated` / `record_time`), `invalid_at`, `ended_unknown`.
- **Record time:** `first_seen_at` (set once), `last_confirmed_at` (bumped only on a content change), `attested_from`.
- **Status:** `assertion_status` (`live` / `corrected`), `projection_status` (`live` / `pending_review`), `pinned`, `decay_class`.
- **Derivation:** `derived`, `derived_rule`, `premise_fact_uids[]`.

**Write semantics:**

- All writes are batched `UNWIND … MERGE`. Label and relation names are sanitised before interpolation.
- `upsert_entities` never erases a known property with `null`.
- `revive=True` (structural re-assertion) reopens a closed edge; `revive=False` (text facts, backfill) never does.
- `supersede_fact_edges`, `close_fact`, `correct_fact` and `remove_record_support` always archive the edge's interval into `FactHistory` before changing it.

**Indexes** (`graph/storage/schema.py::bootstrap_schema`):

- range index on `SourceRecord.record_key`;
- range index on `uid` for 17 labels;
- `FactHistory.fact_uid`;
- fulltext on `search_text` for WorkItem, Document, Decision, Term, Api, Endpoint, Commit, PullRequest, SourceFile, Finding and Wisdom.

There is **no** FalkorDB vector index; vectors live in pgvector or Qdrant.

**Ontology as data** (`graph/semantics/axioms.py`, ledger `relation_axioms`):

- Each relation has flags: extractable, functional, transitive, symmetric, asymmetric, inverse_of, sub_property_of, and temporal class (`state` / `event` / `eternal`).
- The flags are seeded per graph and drive: which LLM facts are allowed; direction correction; cardinality checks in hygiene; and how time filters apply (an `event` must fall inside a query window; a `state` only needs to overlap it).

### 7.3 Ledger schema (`connectors/core/ledger.py`, one store per named graph)

DDL is written in SQLite dialect and translated for Postgres. Columns are added idempotently with `ALTER TABLE` guards.

| Table | Key columns | Purpose |
|---|---|---|
| `source_records` | **record_key** PK, content_hash, primary_node_uid, semantic_status (`pending` / `done` / `not_applicable`), semantic_priority (default 100; changed records get 200+), update_count, updated_at | Current hash per record and the extraction queue order |
| `source_chunks` | PK (record_key, chunk_id), chunk_index, text, status (`pending` / `done`), committed_at, **superseded_at**, **llm_text**, resolution_status/reason, triage_type, triage_durable_p, triage_model, triage_facts_written | Content-keyed chunk queue with soft supersession |
| `record_versions` | PK (record_key, version), content_hash, ingested_at | Append-only history of real content changes |
| `record_edges` | PK (record_key, rel_type, from_uid, to_uid) | Which record supports which edge (used on UPDATE and delete) |
| `extraction_drops` | record_key, chunk_id, reason, subject/relation/object kind and name, detail, created_at | Why extracted items were not written (rewritten per chunk) |
| `relation_axioms` | PK (relation, subject_kind, object_kind), extractable, functional, is_transitive, is_symmetric, is_asymmetric, inverse_of, sub_property_of, temporal, adopted_batch | The ontology |
| `axiom_adoptions` | batch_id PK, adopted_at, shapes (JSON), facts_expected, min_docs, undone_at | Revertible ontology widening |
| `ontology_misses` | PK (kind, key), example, count, first/last_seen_at, dismissed_at | Refused shapes, counted for adoption |
| `graph_settings` | key PK, value | e.g. auto-extend ontology |
| `reviews` / `review_rejections` | id, type, payload JSON, identity, state (`pending` / `approved` / `rejected`), decided_by/at / rejected identities | Human review queue; a rejected proposal is never recreated |
| `entity_aliases` | UNIQUE (label, namespace_uid, alias_norm) → uid, source | Aliases from approved merges |
| `mention_stoplist` | UNIQUE (term_norm, label), reason | Generic terms never minted as Term/System (seeded) |
| `resolution_stats` | UNIQUE (run_id, label, resolved_by), count | Which ladder rung resolved what |
| `hygiene_runs` | run_id, graph_name, label, isolated_count, total_count | Graph-health trend |
| `link_candidates` | UNIQUE (from_uid, to_uid, relation), confidence, derived_rule, state | Proposed edges awaiting review |
| `merge_trace` | survivor_uid, absorbed_uid, label, reason, merged_at | Executed merges |
| `ingestion_findings` | **finding_key** PK, record_key, chunk_id, kind, severity, status (`open` / `stale`), title, summary, reasoning, confidence, properties_json, created/updated_at, stale_at, stale_reason | Findings (source of truth) |
| `ingestion_finding_evidence` | PK (finding_key, record_key, role), chunk_id, excerpt | Evidence for findings |
| `sync_coverage` | run_id, provider, connection_id, provider_reported_total, fetched_count, ledger_count, skipped_by_rule_count, extension_filtered_count, commits_capped | One row per sync run: did we get everything? |

### 7.4 Other operational stores

| Store (file or Postgres schema) | Tables |
|---|---|
| `connector_jobs` | `connector_jobs(job_id PK, provider, payload_json, status, attempts, max_attempts, available_at, lease_owner, lease_expires_at, created_at, updated_at, last_error)` |
| `oauth_connectors` (Jira, Bitbucket) | `oauth_states` (hashed state, 600 s TTL), `oauth_connections` (**Fernet-encrypted token**), `oauth_session_connections`, `oauth_sources`, `oauth_sync_runs` (+ a partial unique index: one active run per source and graph) |
| `github_connector` | `github_oauth_states`, `github_installations`, `github_session_installations`, `github_sources`, `github_sync_runs`, `github_episodes` (legacy) |
| `notion_connector` | `oauth_states`, `notion_connections` (encrypted token), `notion_pages` (is_active), `notion_sync_runs`, `notion_session_connections`, `notion_episodes` (legacy) |
| `graphs` | `graphs(name PK, display_name, created_at)`: the registry of named graphs |
| `runtime_settings` | `runtime_settings(name PK, value)`: currently only `laya_enabled` |

### 7.5 Vector store (`graph/storage/vector_store.py`, `graph/storage/embeddings.py`)

- **Model:**
  - `BAAI/bge-m3`, 1024 dimensions, cosine, L2-normalised, up to 8192 tokens, schema version 2.
  - Loaded in-process with sentence-transformers; the device is MPS if available, else CPU.
  - A single background thread owns the model.
  - Writes are batched (`open_batch` / `close_batch`, flushing at 16 records or about 100k tokens).
  - Write-time input is clipped to `EMBEDDING_WRITE_MAX_CHARS` (4096).
- **Layout:** one row per node `uid`, with **two named vectors**:
  - `content`: the full `search_text`;
  - `name`: the bare name or path.
  - They exist because short "which file handles X" queries otherwise favoured near-empty files.
  - This is a current projection, not a history.
- **Not embedded:** facts and edges.
- **pgvector table:** `entity_embeddings(collection, uid, label, content_embedding vector(1024), name_embedding vector(1024), embedded_text, embedded_model, embedded_content_hash, embedding_schema_version, namespace_uid, updated_at)`, PK `(collection, uid)`, HNSW `vector_cosine_ops` on both vectors.
- **Qdrant (legacy):** same channels, INT8 quantised, payload `label` / `uid` / `namespace_uid` / model fingerprint.
- **Access control is not in the vector store.** Every vector hit is hydrated and ACL-filtered in FalkorDB, and searches over-fetch ×4 to compensate.

> **Warning: `storage/postgres.py` has two personalities.** Only its `entity_embeddings` / `vector_*` methods are live. `bootstrap()` also creates a story-era schema in `public` (`knowledge_graphs`, `source_records`, `source_chunks` with embeddings, `fact_ledger`, `findings`, `wisdom_*`, `graph_outbox`, `story_runs`, …) that **no live code reads or writes**. The live ledger tables are the per-graph ones from `connectors/core/ledger.py`.

---

## 8. The temporal model

Neuron is **bi-temporal** (`graph/semantics/time_axis.py`).

| Axis | Question it answers | On a live edge | In `FactHistory` |
|---|---|---|---|
| World time | "When was this true?" | `valid_at` / `invalid_at` (+ `ended_unknown`) | `valid_from` / `valid_to` |
| Record time | "When did Neuron know it?" | `first_seen_at`, `last_confirmed_at` | `observed_from` / `observed_to` |

**Where `valid_at` comes from:**

- **Deterministic edges:** the record's `updated_at or created_at`.
- **Jira changelog:** exact `FactHistory` intervals for assignee and status.
- **LLM facts:** `dates.stated_dates(evidence)`. It looks for start keywords (since, from, effective, as of…) and end keywords (until, ended, replaced by, superseded…), and understands sprints and quarters. The fallback is record time (`valid_at_basis="record_time"`).

**How a fact changes over time:**

```mermaid
flowchart TD
  N["New fact (from a changed record or LLM extraction)"] --> C{"find_conflict_candidates:<br/>same rel into same object; same rel out of subject if functional;<br/>live Decision APPLIES_TO same object"}
  C -->|none| W["write live (revive=False)"]
  C -->|candidates| K["classify_fact_update (Laya fact_update)"]
  K -->|duplicate| D["confirm_fact: add provenance, times untouched"]
  K -->|extends / unrelated| W
  K -->|"newer_state, windows disjoint"| W
  K -->|"newer_state, new is later"| X["close old at new.valid_at<br/>(review in suggest mode)"]
  K -->|"newer_state, new is older"| B["write new already closed (backfill)"]
  K -->|"newer_state, dates missing"| E["mark old ended_unknown + write new"]
  K -->|"contradicts / corrects"| R["suggest: create fact_update review,<br/>write new as projection_status=pending_review<br/>auto: close/correct old, DISPUTED_WITH for Decisions, then set live"]
```

- **Structural single-valued facts** (e.g. a Jira assignee): `supersede_fact_edges` closes the old edge and archives it to `FactHistory`, then upserts the new one.
- **Deleted or changed records:** `remove_record_support` removes the record from an edge's `source_record_keys`. The edge closes only when no supporting record remains.
- **Pinned facts** are never destroyed automatically; destructive kinds go to review.
- **Nothing is ever hard-deleted from history.** Corrections set `assertion_status='corrected'` and write a zero-width interval.

**At query time** (see [§10.8](#108-time-at-query-time)): `infer_query_window(question)` turns "in August 2026" into a world window, and "what did we know on 2026-09-10" into a record-time `as_of`. `holds_at` / `held_at` filter each candidate's facts.

---

## 9. Findings and Wisdom

### 9.1 Findings: how a new one appears

A Finding is something the extraction LLM thinks a human should know: a contradiction, a broad architecture or dependency change, or an ambiguity. **There is no separate LLM call.** Findings come out of the same extraction call, as `assessments`.

```mermaid
flowchart TD
  A["Semantic pass LLM call returns assessments[]<br/>(action, topic_key, should_flag, severity, title, summary, reasoning, evidence, related_candidate_uids)"] --> B{"evidence verbatim in chunk<br/>AND related UIDs in candidate allow-list?"}
  B -->|no| X["dropped"]
  B -->|yes, should_flag=false| U["logged as unflagged only"]
  B -->|yes, should_flag=true| C["ledger.record_ingestion_assessments"]
  C --> S["mark every OPEN finding from this same (record_key, chunk_id) stale<br/>(a re-processed chunk that no longer raises it should not keep it open)"]
  S --> K["finding_key = llm:{provider}:{action}:{slug(topic_key)}<br/>INSERT ... ON CONFLICT DO UPDATE -> status=open, stale cleared"]
  K --> EV["ingestion_finding_evidence row (role=new_evidence, excerpt)"]
  EV --> FB["after the pass: finding_bridge.sync_ledger_findings<br/>MERGE (:Finding {uid}) + MENTIONED_IN SourceRecord + embed"]
  FB --> R["retrievable: Finding lane in chat, knowledge citations, UI Findings layer"]
```

- **Lifecycle in the ledger:** `open ↔ stale`. A finding reopens when its topic is raised again.
  - `kind` = `llm_<action>`; severity is one of info/warning/high/critical (default `warning` when missing).
  - `resolved` exists only in the unused story-era Postgres code.
- **Dedup:** `topic_key` deduplicates within one provider only. The key includes the provider, so the same topic raised from Jira and from Notion gives two findings.
- **Graph links:** a Finding node is linked only by `MENTIONED_IN`. There is no `FLAGS` edge to the entities it is about.
- **Deletion:** findings are removed together with their connection or local-data provider.

### 9.2 Wisdom: read path exists, write path does not

Wisdom is meant to be durable guidance (Policy, Principle, Pattern, AntiPattern, Playbook, Heuristic) distilled from clusters of findings. It would be reviewed, and then used silently to steer answers.

**What exists:**

- `Wisdom` is an indexed, embedded, searchable label.
- Chat has a reserved Wisdom lane that keeps only `status IN ('active','proposed')`, and follows `(:Wisdom)-[:DERIVED_FROM]->(:Finding)` for lineage.
- The chat prompt has rules for it: `active` = approved and used silently; `proposed` = awaiting review; `rejected` never guides an answer.
- `graph/retrieval/wisdom.py::generate_wisdom_proposal` makes one LLM call over a findings cluster. It returns `create / strengthen_existing / weaken_existing / supersede_existing / insufficient_evidence` plus type, statement, rationale, recommended action and confidence. Promotion is always `human_review`.

**What does not exist yet:**

- nothing calls `generate_wisdom_proposal` outside tests;
- nothing writes `:Wisdom` nodes or `DERIVED_FROM` edges;
- the Postgres `wisdom_*` tables are story-era and unused.

The Wisdom lane therefore returns nothing unless Wisdom nodes are created by some other means. Wiring this up is open work.

---

## 10. Retrieval and answering (how the "agent" works)

**There is no tool-calling agent.** The LLM never chooses tools, Cypher or graph actions. Retrieval is a fixed set of deterministic lanes plus bounded Laya rounds. The only "agentic" step is an optional planner that writes up to 2 extra search strings, once. The answer LLM only sees packed evidence and must cite it.

### 10.1 The whole flow

```mermaid
flowchart TD
  Q["POST /api/chat -> run_chat_turn (graph/retrieval/chat.py)"] --> T["time_axis.infer_query_window -> world at/at_end + record as_of"]
  T --> S{"structured_query.resolve_structured<br/>(regex intents -> exact Cypher)"}
  S -->|match| SP["complete hit list, never cut + STRUCTURED preamble"]
  S -->|no match| R{"reranker mode (NEURON_RERANK or runtime toggle)"}
  R -->|off| H0["hybrid_search limit=40 (+ Wisdom/Finding lanes if the question asks)"]
  H0 --> PL["+ pair lane -> expand_neighbors 1 hop (8 seeds x 4) -> + named persons -> + time-window lane"]
  PL --> CUT["cut to search_limit = 6"]
  R -->|laya| HL["hybrid_search pool 40 + Wisdom lane (5) + Finding lane (5) + Finding lineage"]
  HL --> DL["+ pair / named-person / time-window lanes"]
  DL --> L1["Laya scores every candidate -> roles direct / temporal / bridge / irrelevant"]
  L1 --> LOOP{"accepted < 2 and rounds < 2?"}
  LOOP -->|yes| HOP["expand from direct+temporal+bridge seeds -> rescore -> path_support >= 0.4 gate"] --> LOOP
  LOOP -->|no| SEL["keep direct + temporal, deterministic lanes first, max 12"]
  SEL --> AG{"NEURON_AGENTIC_RETRIEVAL=on and kept < 2?"}
  AG -->|yes| PLN["LLM plan_subqueries (<= 2) -> hybrid_search each (20) -> Laya two-pass once more"] --> PK
  AG -->|no| PK
  L1 -.->|exception| FB["fallback to the RRF path (trace.fallback)"] --> PK
  CUT --> PK
  SP --> PK
  PK["_pack_context: per hit, time-filtered facts (<= 25), best 500-token window,<br/>Finding/Wisdom metadata; 10k-token budget minus reserve"]
  PK --> LLM["responses.parse(CHAT_MODEL): SYSTEM_PROMPT + CLOCKS header + EVIDENCE -> {answer, used_sources}"]
  LLM --> CIT["used_sources -> SourceRecord citations (ACL) + knowledge citations + path_support / low_support"]
  CIT --> FBK["optional POST /api/chat/feedback helpful=true -> link candidates"]
```

### 10.2 Structured lane (`graph/retrieval/structured_query.py`)

Regex intent detection runs first, and the first match wins. When it matches, the result is a **complete** list that is never truncated, and the prompt is told so.

1. "What breaks if we remove X" → open Findings of kind `removed_dependency_still_called`. No current writer produces that kind (see [§16](#16-known-gaps-and-gotchas)).
2. "Who calls API X" → live `CALLS_ENDPOINT` facts from source files.
3. Unassigned work items.
4. Work assigned to a person. Fuzzy name match (difflib ≥ 0.82), expanded over the `SAME_AS` cluster.
5. Latest commit(s), optionally for a named repo.
6. Commit SHA prefix lookup.
7. Jira key lookup (`ABC-123`).

### 10.3 Hybrid search (`graph/retrieval/search.py::hybrid_search`)

- **Keyword leg:**
  - FalkorDB fulltext `db.idx.fulltext.queryNodes` on each label's `search_text`;
  - terms are OR-joined, each with a `*` prefix wildcard;
  - results are restricted to nodes `MENTIONED_IN` a visible, non-deleted SourceRecord;
  - 30 results per label, merged by global BM25 score.
- **Vector leg:**
  - one query embedding (BGE-M3), two global searches (the `content` and `name` channels);
  - over-fetch ×4, then ACL post-filter in FalkorDB;
  - the two channels are **interleaved round-robin**, never score-merged, because their cosine scales differ.
- **Fusion:** weighted reciprocal-rank fusion.
  - Fulltext contributes `1/(K + rank + 1)`; vectors contribute `3.0/(K + rank + 1)`, with `RRF_K = 10`.
  - These constants were tuned by `scripts/sweep_retrieval_params.py` against the golden sets; there is no env override.

### 10.4 The other lanes

- **Pair lane** (`chat._two_entity_lane`): when the question names two anchors (Jira keys, SHAs, PR refs, repo names, file paths, people), both nodes are returned with "co-mentioned in …" evidence.
- **Named persons** (`find_named_persons`): fuzzy person match + `SAME_AS` cluster.
- **Time window** (`find_window_activity`): for dated questions, entities with edges whose `valid_at` falls in the window, busiest first.
- **Graph expansion** (`graph/retrieval/expand.py`):
  - one hop over `IMPLEMENTS, DOCUMENTS, PARENT_OF, REFERENCES, MODIFIES, APPLIES_TO, DEFINES`;
  - live edges only; hub labels (Repository, Project, Workspace, Person, SourceRecord) skipped;
  - 8 seeds × 4 neighbours;
  - each hit is tagged with an **authority tier**: deterministic/exact_anchor/changelog = primary, llm = secondary, derived = derived.
- **Knowledge lanes:** Wisdom and Finding hits, used when the question asks about them, or always when Laya is on.

### 10.5 Reranking

**Stage 1 (always on)** is the weighted RRF above.

**Stage 2 (optional)** is the Laya `retrieval_relevance` gate (`graph/retrieval/rerank.py`). There is no cross-encoder and no LLM reranker.

- **Candidate text:** `[Label] Name`, summary, and up to 6 linked and temporal facts, windowed to 300 tokens.
- **Roles** (`assign_roles`):

  | Condition | Role |
  |---|---|
  | method `time_window` | TEMPORAL_CONTEXT |
  | method `named_entity` / `pair`, or p ≥ `NEURON_RERANK_MIN_P` (0.50) | DIRECT_EVIDENCE (deterministic lanes can never be demoted) |
  | p ≥ `NEURON_RERANK_BRIDGE_MIN_P` (0.20) | BRIDGE_CANDIDATE (max 4, padded with the best rejects) |
  | otherwise | IRRELEVANT |

- **Bounded hop loop** (`_laya_two_pass_search`):
  - Runs while fewer than `NEURON_RERANK_MIN_DIRECT` (2) candidates are accepted and rounds < `NEURON_RERANK_MAX_ROUNDS` (2).
  - Each round expands one hop from the direct, temporal and bridge seeds, then rescores.
  - A newly accepted neighbour must also have **path support** ≥ `NEURON_PATH_MIN` (0.4) to the seeds, or it is rejected.
- **Selection:** only DIRECT and TEMPORAL reach the answer; **bridges never do**. Deterministic lanes come first, then Laya probability. Maximum `NEURON_RERANK_MAX_KEEP` (12).
- **Failure:** any Laya exception falls back to the RRF path and is reported as `retrieval.fallback` / `fallbackReason` in the response.

### 10.6 Optional agentic retry (`graph/retrieval/agentic_retrieval.py`)

Only when `NEURON_AGENTIC_RETRIEVAL=on`, Laya is active, and fewer than 2 hits were kept:

1. One `NEURON_AGENTIC_MODEL` call receives the question plus clues from the first 8 hits.
2. It returns `RetrievalPlan{needs_followup, subqueries}` (at most 2 used; echoes of the question are dropped).
3. Each subquery runs `hybrid_search(limit=20)`.
4. The Laya two-pass runs **once** more over old + new candidates.

The planner never answers, never writes, and never picks tools.

### 10.7 Context packing, answer and citations

**Packing** (`_pack_context`):

- **Budget:** `NEURON_CONTEXT_TOKENS` (10000) minus system prompt, question, header and `NEURON_ANSWER_RESERVE_TOKENS` (2000).
- **For each hit, in order:**
  1. fetch its facts, filtered by the query clocks;
  2. cap at `NEURON_FACTS_PER_BLOCK` (25), asserted before derived;
  3. window the text to `NEURON_BLOCK_TOKENS` (500) with `best_window`, the window with the most distinct question terms;
  4. add Finding/Wisdom status metadata and the expansion tier.
- **Over budget:** shrink the window first, then trim facts, then drop the block.

**Answer:** `responses.parse(CHAT_MODEL, …, text_format={answer, used_sources})`. `SYSTEM_PROMPT` enforces:

- evidence-only answers;
- the two time axes;
- `[inferred]` facts are marked;
- code outranks Jira/Notion on how the system works;
- Wisdom/Finding status rules;
- inline citations like `(HERA-101)`;
- exact block names in `used_sources`.

**Citations:**

- `used_sources` names are mapped back to the record keys of the facts that were actually packed, then resolved to `{recordKey, name, url}` under the ACL.
- Findings and Wisdom are returned as `knowledgeCitations`.

**Path support** (`link_candidates.path_support`):

- the geometric mean of pairwise support over the cited nodes: direct edge 1.0, two-hop 0.8, name in text 0.7, shared record 0.5, otherwise 0.01;
- below `NEURON_PATH_MIN` the answer is flagged `lowSupport`.
- A thumbs-up (`/api/chat/feedback`) turns the cited nodes into **link candidates** for review.

**Access control:** every user-facing read goes through `AccessScope` (`graph/retrieval/access.py`). It is built from the connector session cookies: a record is visible if it is public, or its provider + connection belongs to the caller, or its principals overlap. Local-data connections are always visible. There is no user authentication beyond that.

### 10.8 Time at query time

- `infer_query_window` understands:
  - months ("August 2026", "2026-08", "aug'26") → a month window;
  - years → a year window;
  - days → a day window;
  - explicit times → an instant.
  - Keys like `DATAOS-4346` are not read as years.
  - "What did we know / believe / have on record …" routes the date to `as_of` (record time).
- Only **each candidate's facts** are time-filtered (`holds_at` for world time, `held_at` for record time). The search legs do not filter by time.
- Undated facts are excluded from dated reads.
- The prompt receives a `CLOCKS:` header.
- The UI does not send explicit `at` / `as_of`, so only inferred clocks apply from the UI.

---

## 11. Human-in-the-loop: reviews, link candidates, ontology adoption, hygiene

| Mechanism | Created by | Approve does | UI |
|---|---|---|---|
| `possibly_same_as` review | resolution ladder rung 6 (suggest mode) | merge + namespace-scoped alias + merge_trace | BridgePanel |
| `polarity_conflict_candidate` review | rung 4 negation veto | nothing (no apply handler) | BridgePanel |
| `fact_update` review | `resolve_text_fact` in suggest mode | close / correct / dispute the old fact and make the new one live | BridgePanel |
| `duplicate_pair` review | `duplicate_collector` (Decision/Term; 0.4·vector + 0.2·lexical + 0.2·shared targets + 0.2·shared records) | merge edges and mentions into the most-reinforced survivor | BridgePanel |
| Link candidate | two-hop co-occurrence, semantic isolated-node matching (+ Laya relation type), `shared_concept` rule, chat feedback | write a derived edge with union provenance | BridgePanel |
| Ontology adoption | shapes refused as `relation_not_allowed` in ≥ 2 records and ≥ 3 facts | add extractable axioms as one batch, requeue the affected chunks; `unadopt` reverts by batch | API only (`/api/ontology/*`; `OntologyPanel.tsx` is not mounted) |
| Hygiene | after every sync and nightly | read-only report: isolated nodes per label, cardinality violations, open disputes → `hygiene_runs` | BridgePanel dashboard |

A rejected review's identity is remembered (`review_rejections`), so the same proposal is never created again. `duplicate_collector.propose_duplicate_reviews` and the two-hop/semantic link proposers are currently called only from scripts and tests; nothing schedules them.

---

## 12. HTTP API reference

All paths are under `/api`. Everything is in `demo_ui/backend/`.

**Core (`app.py`):**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health`, `/config` | status, providers, reranker status |
| PUT | `/config/reranker` `{enabled}` | toggle Laya at runtime (409 if unavailable); persisted |
| GET / POST | `/graphs` | list / create a named graph (bootstraps indexes + vector collection) |
| GET | `/graph`, `/sources` | canvas nodes/edges, source list (ACL-scoped) |
| GET | `/entities/{uid}?at&at_end&as_of&providers&graph_name` | relations, history, derived facts |
| GET | `/facts/{fact_uid}/history` | bi-temporal intervals |
| GET | `/export/skos` | SKOS Turtle export (live edges, reified facts) |
| POST | `/admin/clear-graph` | wipe one graph's FalkorDB graph, vectors and ledger records |
| GET / POST | `/ontology/pending`, `/ontology/adoptions`, `/ontology/adopt`, `/ontology/unadopt/{batch_id}`, `/ontology/auto-extend` | ontology adoption |
| POST | `/chat` `{message, providers, at?, as_of?, graph_name}` | returns `answer`, `citations`, `knowledgeCitations`, `highlight{nodes,edges}`, `tokenUsage`, `retrieval{…trace counts, fallback}`, `support{score, lowSupport, citedNodeUids}` |
| POST | `/chat/feedback` `{graph_name, node_uids, helpful}` | helpful → link candidates |

There is no standalone search endpoint; `hybrid_search` is reachable only through chat.

**Connectors** (`/connectors/{jira,github,bitbucket,notion}`):

- `status`, `oauth/start`, `oauth/callback`;
- pickers (`sites`/`projects`, `repositories`, `workspace`/`repositories`/`branches`);
- `POST sync` (202, queued), `GET sync/{run_id}`;
- `DELETE` connection or installation.

**Other routers:**

- `/local-data`: `status`, `POST /{jira,bitbucket,notion}/ingest`, `DELETE /{provider}`.
- `/reviews`: list, `POST /{id}/approve`, `POST /{id}/reject`.
- `/link-candidates`: same shape as reviews.
- `/sync-coverage`: `GET ""`, `GET /{run_id}`.
- `/dashboard`: hygiene, queue sizes, resolution stats, merges, latest eval.

---

## 13. Frontend

`demo_ui/frontend` uses React 19, Vite 7, TypeScript and Cytoscape. `src/api.ts` has every API call and `src/types.ts` the shared types.

| Component | Shows |
|---|---|
| `App.tsx` | Shell: token metrics, connectors, graph map (clear graph, SKOS export, source picker, all / Findings / Wisdom layer toggle), reranker switch |
| `GraphCanvas` | Cytoscape graph, coloured by label, highlighting from chat |
| `EntityPanel` | Selected node: relations, history, derived facts |
| `ChatPanel` + `MarkdownBody` | Grounded chat, citations, verify button (feedback) |
| `GraphSelector` | Switch or create named graphs |
| `JiraPanel`, `GitHubPanel`, `BitbucketPanel`, `NotionPanel` + `SyncProgress` | Connect, pick, sync, disconnect |
| `LocalDataIngestPanel` | Ingest or reset the test-project local replay per provider |
| `BridgePanel` | Review queue + link candidates + dashboard tiles |
| `OntologyPanel` | Exists but is **not mounted** |

---

## 14. Configuration reference

Code defaults are listed. `.env` is loaded from the repo root by `util/paths.py`.

**Infrastructure:**

| Variable | Default | Notes |
|---|---|---|
| `FALKOR_HOST`, `FALKOR_PORT`, `FALKOR_GRAPH` | `localhost`, `6379`, `neuron` | The containers above use port 6380 |
| `DATABASE_URL` / `POSTGRES_URL` | — | Enables pgvector (and Postgres SQL if selected) |
| `NEURON_SQL_BACKEND` | `sqlite` | `postgres` puts every operational store in Postgres (not in `.env.example`) |
| `NEURON_SQL_SCHEMA_PREFIX`, `NEURON_SQL_POOL_SIZE` | `""`, `8` | |
| `VECTOR_BACKEND` | `postgres` if a DB URL is set, else `qdrant` | |
| `VECTOR_COLLECTION` / `QDRANT_COLLECTION`, `QDRANT_URL` | `neuron_entities`, `http://localhost:6333` | |
| `DATA_DIR` | repo root | where SQLite store files live |
| `CONNECTOR_JOB_DB`, `OAUTH_CONNECTOR_STATE_DB`, `GITHUB_STATE_DB`, `NOTION_STATE_DB` | `*.sqlite3` names | store file names |
| `DEMO_UI_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | CORS |
| `SYNTHETIC_DATA_DIR` | local test-project export tree | captured Jira / Bitbucket / Notion replay data |

**Models:**

| Variable | Default |
|---|---|
| `OPENAI_API_KEY` | — |
| `LLM_MODEL` (extraction) / `CHAT_MODEL` (answers) | `gpt-5.6-luna` / `gpt-5.6-sol` |
| `LLM_BUDGET_PER_RUN` / `LLM_CONCURRENCY` | 200 / 6 |
| `NEURON_AGENTIC_MODEL`, `WISDOM_LLM_MODEL`, `CHAIN_GOLDEN_MODEL` | `gpt-5.6-luna`, → `LLM_MODEL`, → `LLM_MODEL` |
| `EMBEDDING_MODEL`, `EMBEDDING_DEVICE`, `EMBEDDING_BATCH_SIZE` | `BAAI/bge-m3`, auto (MPS / CPU), auto (4 / 8) |
| `EMBEDDING_MAX_TOKENS`, `EMBEDDING_WRITE_MAX_CHARS`, `EMBEDDING_MODEL_REVISION` | 8192, 4096, — |
| `LAYA_MODEL_DIR`, `LAYA_DEVICE` | —, `cpu` |

**Ingestion decisions:**

| Variable | Default | Values |
|---|---|---|
| `NEURON_TRIAGE` | `shadow` | `off` / `shadow` / `enforce` |
| `NEURON_RESOLVE_MODE` | `suggest` | `suggest` / `auto` (Decisions are always reviewed) |
| `NEURON_FACT_UPDATE_MODE` | `suggest` | `suggest` / `auto` |
| `NEURON_INGEST_RERANK` | → `NEURON_RERANK` → `laya` | `off` / `laya` |
| `NEURON_INGEST_CONTEXT_POOL` / `_TOP_K` / `_THRESHOLD` | 20 / 7 / 0.5 | |
| `NEURON_HYGIENE_INTERVAL_S` | 86400 (min 60) | |

**Retrieval:**

| Variable | Default |
|---|---|
| `NEURON_RERANK` | `off` (`off` / `laya`) |
| `NEURON_RERANK_MIN_P` / `_BRIDGE_MIN_P` / `_BRIDGE_LIMIT` / `_MIN_DIRECT` / `_MAX_KEEP` / `_MAX_ROUNDS` / `_FACTS` | 0.50 / 0.20 / 4 / 2 / 12 / 2 / 6 |
| `NEURON_PATH_MIN` | 0.4 |
| `NEURON_POOL_SIZE` | 40 |
| `NEURON_BLOCK_TOKENS` / `NEURON_FACTS_PER_BLOCK` / `NEURON_CONTEXT_TOKENS` / `NEURON_ANSWER_RESERVE_TOKENS` | 500 / 25 / 10000 / 2000 |
| `NEURON_AGENTIC_RETRIEVAL` | `off` |
| `NEURON_AGENTIC_MAX_SUBQUERIES` / `_POOL_PER_QUERY` / `_MIN_RESULTS` | 2 / 20 / 2 |

Hard-coded, with no env override: `RRF_K=10`, `VECTOR_LEG_WEIGHT=3.0`, `OVERFETCH_FACTOR=4`, per-method limit 30, expansion `MAX_SEEDS=8` / `PER_SEED=4`, chat `search_limit=6`, and the chunk policies.

**Connectors:**

| Connector | Variables |
|---|---|
| Jira | `JIRA_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI`, `JIRA_TOKEN_ENCRYPTION_KEY` (or `CONNECTOR_TOKEN_ENCRYPTION_KEY`) |
| GitHub | `GITHUB_APP_ID`, `GITHUB_CLIENT_ID/SECRET`, `GITHUB_APP_SLUG`, `GITHUB_PRIVATE_KEY_PATH`, `GITHUB_OAUTH_CALLBACK_URL`, `GITHUB_MAX_FILE_KB` (512), `GITHUB_MAX_COMMITS_PER_SYNC` (100) |
| Bitbucket | `BITBUCKET_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI`, `BITBUCKET_TOKEN_ENCRYPTION_KEY`, `BITBUCKET_MAX_FILE_BYTES` (1,000,000 in code; the example sets 524,288), `BITBUCKET_MAX_COMMITS_PER_SYNC` (100), `BITBUCKET_API_CONCURRENCY` (1), `BITBUCKET_API_MIN_INTERVAL_SECONDS` (0.25), `BITBUCKET_MAX_REQUEST_INTERVAL_SECONDS` (5), `BITBUCKET_MAX_INLINE_RETRY_SECONDS` (60) |
| Notion | `NOTION_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI`, `NOTION_TOKEN_ENCRYPTION_KEY` (a valid Fernet key), `NOTION_OAUTH_SUCCESS_REDIRECT`, `NOTION_REQUEST_INTERVAL_SECONDS` (0.34) |

Tokens are stored Fernet-encrypted. Never put tokens in record metadata: hashing refuses secret-looking keys.

---

## 15. Scripts, evaluation and tests

### 15.1 Scripts (`uv run python -m scripts.<name>`)

| Script | Does |
|---|---|
| `evaluate_retrieval <jsonl> [--k 8] [--with-chat] [--graph NAME] [--stage-metrics]` | Runs a golden set through the real `retrieve` / chat path: recall@k, precision@k, MRR, citation P/R, stage metrics, tokens, cost, latency. Appends to `eval/results.md`. |
| `sweep_retrieval_params` | Grid sweep over the RRF constants |
| `build_chain_golden` | Generates multi-hop chain questions |
| `benchmark_laya` | Laya latency and memory |
| `measure_review_precision` | Review precision / ECE for suggest → auto promotion |
| `rebuild_vectors [--recreate] [--graph]` | Rebuilds the vector projection from the graph (either backend) |
| `migrate_sqlite_to_postgres [--apply]` | Copies the SQLite stores into Postgres schemas (dry run by default) |
| `migrate_scoped_identity [--apply]` | Scoped-identity migration + temporal backfill (dry run by default) |
| `reset_knowledge_data [--apply]` | Wipes rebuildable graph, vector and ledger state; keeps OAuth, the registry and `.env` |
| `backup_falkordb_rdf` | N-Quads backup of FalkorDB graphs |
| `sync_jira --connection ID --project-key K` | CLI Jira Pass A (default graph, cwd-relative stores) |

### 15.2 Evaluation (`eval/`)

| File | Contents |
|---|---|
| `less_token_golden.jsonl` | 7 cases |
| `test_project_golden.jsonl` | 41 cases: 31 control, 10 target |
| `argus_golden.jsonl` | 6 cases |
| `chain_golden.jsonl` | 44 multi-hop chains; uses `question`, not `query`, so it needs mapping |
| `golden.example.jsonl` | template |
| `test_project_baseline_2026-09-16.json` | saved baseline run |
| `results.md` | run log; the dashboard shows its latest table |

There is currently **no trustworthy baseline** after the Postgres + BGE-M3 reset. See [§16](#16-known-gaps-and-gotchas).

### 15.3 Tests

- About 70 test files and roughly 850 tests.
- `tests/conftest.py` pins SQLite and `NEURON_RERANK=off`.
- `NEURON_INTEGRATION=1` enables live FalkorDB, Qdrant and Postgres tests.
- `NEURON_TEST_SQL_BACKEND=postgres` with `NEURON_TEST_DATABASE_URL` runs the store tests on Postgres.
- `tests/test_reranker_config.py` imports `demo_ui.backend.app`, which opens the stores at import time. It fails to collect when `.env` selects Postgres and Postgres is not running.
- Not covered by tests: the GitHub pipeline, the Notion API client, SKOS export, the frontend and end-to-end sync routes.

---

## 16. Known gaps and gotchas

**Code state:**

- **The refactor is uncommitted.** `git log --follow` cannot yet link old `graph/*.py` paths to the new subpackages.
- `connectors/core/purge.py::purge_groups` imports the removed `graph.bridge.resolver` and would raise.

**Data correctness:**

- **`PARENT_OF` direction differs by source:** Jira child → parent, Notion parent → child. The axioms and `derived.py` assume child → parent.
- **Deletes:**
  - Jira deletions are not detected.
  - Notion deletions are not propagated.
  - Bitbucket/GitHub reconciliation deletes commits that fall past the per-sync commit cap.
- **Connection DELETE handlers purge only the `default` graph's ledger and graph**, not named graphs.
- **Pending chunks that are never extracted:** local Jira/Bitbucket replay and `sync_jira.py` leave chunks pending, and no semantic pass runs with their prefix.
- **`find_moved_from` / Notion `adopt_from` is effectively unreachable.** The hash includes the record_key, so two keys never share a hash.
- **Markdown in repos is token-split**, not heading-aware.
- **Notion documents** may end up with no vector when all their chunks are resolved deterministically. This is inferred from the code and not verified.

**Findings and Wisdom:**

- **Wisdom has no writer** ([§9.2](#92-wisdom-read-path-exists-write-path-does-not)).
- **Findings** dedupe only per provider, have no `FLAGS` edges, and the structured "removal impact" lane looks for a finding kind no one writes.

**Unused code** (tested but with no runtime caller): `inference.py` (forward chaining), `freshness.py`, `wisdom.py`, `connectors/ingest/pdf_loader.py`, `connectors/notion/notion.py`, `OntologyPanel.tsx`, and the story-era schema in `storage/postgres.py`.

**Configuration drift:**

- `.env.example` has dead `STORY_*` keys and omits `NEURON_SQL_BACKEND`.
- Its values differ from code defaults for `FALKOR_PORT` and `BITBUCKET_MAX_FILE_BYTES`.
- `docs/25-plan.md` lists flags no code reads (`NEURON_EXPAND*`, `NEURON_RERANK_TIMEOUT_S`, `NEURON_RESOLVE_GRAY_MIN`, `NEURON_HYGIENE_DRY_RUN`).

**Leftovers:**

- `open_batch` and `embed_query` still take an OpenAI client argument that they ignore.
- Many comments still say "Qdrant" where pgvector is now the default.

**Data and eval state:** the named graphs hold stale or foreign-schema data, and the Bitbucket re-ingest on the new Postgres + pgvector + BGE-M3 stack is incomplete. Re-run the golden sets after a fresh ingest before trusting any numbers.

**No LICENSE / NOTICE file**, although Apache-2.0 code from PipesHub (`connectors/core/chunking/code_parser/`) is included with headers.

---

## 17. Docs index (and which ones are stale)

| Doc | Covers | Status |
|---|---|---|
| `VECTOR_SCHEMA.md` (root) | Embedding contract | **Current** (one old module path) |
| `docs/VECTOR_SCHEMA.md` | Embedding contract | **Stale**: describes a TEI embedder service that does not exist |
| `docs/SQL_BACKEND.md` | SQLite/Postgres switch, schema-per-file, migration, tests | Current |
| `docs/25-plan.md` | Upgrade plan, phases 0–7 | Current on status; old paths; lists some unread flags |
| `docs/QUERIES.md` | Open blockers and decisions (27 Sep) | Mostly current |
| `docs/Jira.md`, `docs/Bitbucket.md`, `docs/Notion.md` | Field → node/edge mapping per connector | Mapping current; the "no LLM for Jira/Bitbucket" claim is stale |
| `docs/SYNTHETIC_DATA_SCHEMA.md` | Local replay file shapes | New; points at `synthetic/mcp-access/`, but the loader reads `$SYNTHETIC_DATA_DIR` (test-project capture) |
| `docs/flow.md` | Full ingest flow + code map | Useful narrative, old flat paths |
| `docs/flow2.md` | One Notion page in and out (Hinglish walkthrough) | Mostly valid |
| `docs/ingestion-explained.md` | Plain-English ingestion changes (16 Sep) | Narrative |
| `docs/neuron-upgrade-report.md` | Measured upgrade report (15 Sep) | Snapshot |
| `docs/utopia-to-neuron.md` | Ideas borrowed from Utopia and why | Historical, accurate |
| `docs/plan.md`, `docs/CHECKLIST.md` | v1 design and build log | Historical |
| `docs/cost.md`, `docs/cost2.md` | Cost snapshots | Historical (OpenAI embeddings / Qdrant era) |
| `docs/demo.md` | Argus demo data map | Historical |

**Where to start reading code:**

1. `connectors/core/models.py` → `runner.py` → `ledger.py` (the record contract and the ledger)
2. `graph/ingestion/jira_pipeline.py::write_issue` (a complete Pass A)
3. `graph/ingestion/semantic_pass.py::run_semantic_pass` (Pass B and its gates)
4. `graph/storage/writer.py::upsert_fact_edges` (the fact contract)
5. `graph/retrieval/chat.py::run_chat_turn` → `retrieve` (the answer path)
