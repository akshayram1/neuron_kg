# Neuron — Company Knowledge Graph on FalkorDB

Status: **design locked, implementation not started.**

Ingest Jira + GitHub + Notion (and later more sources) into a single incremental
knowledge graph on FalkorDB, without Graphiti. Every fact keeps its source, its
validity window, and how it was derived.

---

## 1. Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Graph topology | One unified FalkorDB graph | Cross-source edges become normal edges; no projection/bridge layer needed |
| Framework | None — `falkordb` client + Cypher directly | Graphiti's episode/ontology coupling is what we are removing |
| Extraction | Deterministic always + LLM budgeted | Deterministic scales to millions; LLM reserved for free text |
| Facts | Temporal edges (`valid_at` / `invalid_at`) | "What did we believe in March" must be answerable |
| Search | FalkorDB native fulltext (BM25) + vector index, own RRF merge | No separate vector store needed |
| History | Full backfill from day 1, no truncation | Complete audit trail required |
| LLM cost | Hard budget per sync run + pending queue | Predictable cost/time regardless of backlog |
| Provenance | First-class: `:SourceRecord` nodes + edge properties | Graphiti gave this for free; we must build it |
| UI | `demo_ui/` only (FastAPI + React) — Streamlit apps retired | One UI, not two; demo_ui is the more complete one |
| Agent access | Inside `demo_ui` chat/agent endpoints — **no MCP server** | No external agent clients needed |
| Build order | Jira end-to-end → GitHub → Notion → cross-source → query layer | Prove one vertical slice before replicating |

---

## 2. Schema design

### 2.1 Provenance anchor

One node per ingested source record. Cheap (no embedding), always written first.

```cypher
(:SourceRecord {
  record_key,      // "jira:conn123:work_item:10045" — canonical identity, UNIQUE
  provider,        // jira | github | notion
  connection_id,
  entity_type,     // work_item | commit | source_file | pull_request | page | project | repository
  external_id,     // provider's stable id
  name,
  url,             // safe public URL — never a signed/temporary URL
  content_hash,    // drives INSERT/UPDATE/KEEP
  semantic_status, // done | pending  (LLM enrichment queue state)
  created_at, updated_at, ingested_at
})
```

### 2.2 Entity nodes

**Structural** — built deterministically from API fields, no LLM:

| Source | Labels |
|---|---|
| Jira | `:Project`, `:WorkItem` |
| GitHub | `:Repository`, `:SourceFile`, `:Commit`, `:PullRequest` |
| Notion | `:Document` |
| Shared | `:Person` |

**Semantic** — extracted by LLM from free text, cross-source by nature:

`:Decision`, `:Term`, `:System`

Every entity node carries:

```cypher
{
  uid,             // uuid5(label + canonical identity) — deterministic, UNIQUE, indexed
  name,
  summary,         // accumulated across sources
  first_seen_at, last_seen_at,
  search_text,     // concatenated searchable text → fulltext index
  embedding        // ONLY on content-bearing labels (see §4.6)
}
```

Provider is deliberately **not** a property on entity nodes — an entity can come
from many sources, and that is expressed as edges instead:

```cypher
(:Person {name:"Akshay"})-[:MENTIONED_IN]->(:SourceRecord {provider:"jira"})
(:Person {name:"Akshay"})-[:MENTIONED_IN]->(:SourceRecord {provider:"github"})
```

### 2.3 Edge types

**Structural (deterministic, from API fields):**

```
WorkItem      -[:BELONGS_TO]->    Project
WorkItem      -[:ASSIGNED_TO]->   Person
WorkItem      -[:REPORTED_BY]->   Person
WorkItem      -[:BLOCKS]->        WorkItem
Repository    -[:CONTAINS]->      SourceFile
Repository    -[:CONTAINS]->      Commit
Commit        -[:AUTHORED_BY]->   Person
Commit        -[:MODIFIES]->      SourceFile
PullRequest   -[:REVIEWED_BY]->   Person
Document      -[:PARENT_OF]->     Document
<any entity>  -[:MENTIONED_IN]->  SourceRecord
```

**Semantic (LLM-extracted facts):**

```
Decision -[:APPLIES_TO]->  System | Term | CatalogEntity
Decision -[:DECIDED_BY]->  Person
Decision -[:SUPERSEDES]->  Decision
Term     -[:DEFINES]->     Term
Person   -[:OWNS]->        Term | Decision
<any>    -[:CAVEAT_OF]->   <any>
```

**Cross-source links** (deterministic anchors, or LLM-adjudicated — see §5):

```
Commit      -[:IMPLEMENTS]-> WorkItem
PullRequest -[:RESOLVES]->   WorkItem
Document    -[:DOCUMENTS]->  WorkItem | Commit | Repository
<any>       -[:REFERENCES]-> <any>
<any>       -[:SAME_AS]->    <any>        // requires verified stable identifier
```

### 2.4 Fact edge properties — provenance + temporal

Every semantic and cross-source edge carries:

```cypher
{
  source_record_keys: [...],   // ARRAY — multiple sources can support one fact
  evidence,                    // verbatim span from the source text
  extraction_method,           // deterministic | llm
  confidence,                  // 1.0 for deterministic
  valid_at, invalid_at,        // temporal window; invalid_at = null means live
  first_seen_at, last_confirmed_at,
  resolver_version             // for cache invalidation when prompts/policy change
}
```

**Design note — edge properties vs. fact reification.** Reifying each fact as its
own `(:Fact)` node would allow facts-about-facts and a richer audit trail, but
multiplies node count 3–5x and adds a hop to every traversal. At millions of
records that cost is not acceptable, so provenance and temporality live on edge
properties. Revisit only if meta-facts become a real requirement.

### 2.5 Indexes — created before any bulk load

Non-negotiable. Without these, every `MERGE` degrades to a full label scan and
ingestion time grows quadratically with graph size.

```cypher
CREATE INDEX FOR (n:SourceRecord) ON (n.record_key)   // unique
CREATE INDEX FOR (n:WorkItem)     ON (n.uid)
CREATE INDEX FOR (n:Project)      ON (n.uid)
CREATE INDEX FOR (n:Person)       ON (n.uid)
CREATE INDEX FOR (n:Repository)   ON (n.uid)
CREATE INDEX FOR (n:SourceFile)   ON (n.uid)
CREATE INDEX FOR (n:Commit)       ON (n.uid)
CREATE INDEX FOR (n:PullRequest)  ON (n.uid)
CREATE INDEX FOR (n:Document)     ON (n.uid)
CREATE INDEX FOR (n:Decision)     ON (n.uid)
CREATE INDEX FOR (n:Term)         ON (n.uid)
CREATE INDEX FOR (n:System)       ON (n.uid)

// keyword search
CALL db.idx.fulltext.createNodeIndex('WorkItem', 'search_text')
CALL db.idx.fulltext.createNodeIndex('Document', 'search_text')
CALL db.idx.fulltext.createNodeIndex('Decision', 'search_text')
CALL db.idx.fulltext.createNodeIndex('Term',     'search_text')
CALL db.idx.fulltext.createNodeIndex('Commit',   'search_text')

// semantic search — only on content-bearing labels
CREATE VECTOR INDEX FOR (n:WorkItem) ON (n.embedding) OPTIONS {dimension: 1536, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (n:Document) ON (n.embedding) OPTIONS {dimension: 1536, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (n:Decision) ON (n.embedding) OPTIONS {dimension: 1536, similarityFunction: 'cosine'}
CREATE VECTOR INDEX FOR (n:Term)     ON (n.embedding) OPTIONS {dimension: 1536, similarityFunction: 'cosine'}
```

Exact index DDL syntax must be verified against the installed FalkorDB version
during Phase 0.

---

## 3. Ingestion pipeline

```
Provider API (existing connector)
  → SourceRecord                        (connectors/core/models.py)
  → canonical content hash              (connectors/core/hashing.py)
  → ledger compare → action             (connectors/core/actions.py + ledger.py)
      ├── KEEP    → stop, no writes, no LLM
      ├── DELETE  → invalidate facts sourced only from this record
      └── INSERT / UPDATE ↓

  PASS A — deterministic (always, every record, no LLM)
    1. upsert (:SourceRecord)
    2. build structural nodes + edges from API fields
    3. batched UNWIND MERGE writes
    4. exact anchor extraction (Jira keys, URLs, commit SHAs, verified emails)
       → deterministic cross-source edges, confidence 1.0
    5. temporal diff: changed field → set invalid_at on old fact edge,
       create new edge with valid_at = now

  PASS B — semantic (budget-capped, resumable queue)
    6. chunk free text (semantic for prose, AST for code)
    7. LLM extraction with the per-source profile schema → typed nodes/facts
    8. compute embeddings for content-bearing nodes
    9. entity linking: vector top-K → type filter → LLM adjudication
       → link / review-queue / reject, decision cached
   10. budget exhausted → leave semantic_status = 'pending', resume next run
```

### 3.1 Extraction profiles

Per-source typed schemas, ported from `graph/profiles.py` + `graph/ontology.py`
(the Graphiti plumbing is dropped; the type definitions and their
prompt-engineered docstrings are kept — they are good prompt engineering).

| Profile | Sources | Entities |
|---|---|---|
| `work_management` | Jira | Project, WorkItem, Person, Decision, Term, System |
| `software_knowledge` | GitHub | Repository, SourceFile, Commit, PullRequest, Person, Decision, Term, System |
| `business_document` | Notion | Document, Person, Decision, Term, System |

Post-extraction validation is strict: an edge is persisted only if its relation
is allow-listed in the active profile **and** the source/target label pair is
permitted. Unknown relations are rejected and counted, never silently stored.

---

## 4. How this scales to millions of records

| Mechanism | Effect |
|---|---|
| **Indexes before load** | `MERGE` becomes an index lookup instead of an O(n) label scan. Biggest single lever — without it ingestion time grows quadratically. |
| **Deterministic-first** | ~95% of graph structure is built with zero LLM calls, so a multi-million-record backfill is bounded by API fetch + Cypher write speed, not model latency. |
| **Batched `UNWIND` writes** | One query per ~1000 rows instead of one per row; ~1000x fewer round-trips. |
| **Hash-based KEEP** | After the initial backfill, a daily sync only touches the changed delta. Unchanged records never reach chunking, LLM, or embeddings. |
| **LLM budget + pending queue** | Cost and wall-clock per run are bounded regardless of backlog size; the backlog drains across runs instead of blocking one giant run. |
| **Selective embeddings** | 1536 floats ≈ 6 KB/node. Embedding only content-bearing nodes (not every commit SHA) is the difference between GBs and tens of GBs of RAM. |
| **Type-constrained top-K candidates** | Entity linking stays O(changed × K) instead of O(n²) all-pairs comparison. |
| **Decision cache** | An A↔B adjudication is paid for once, keyed by both content hashes + resolver version — not re-paid on every re-sync. |
| **Ledger side-table `record_key → edge_ids`** | Deletion/invalidation is a targeted lookup instead of a full graph scan. |

### 4.1 Open scaling risks — measure, don't assume

1. **FalkorDB memory.** It is Redis-based and holds the graph in memory. Total
   RAM for nodes + edges + embeddings at target volume must be measured after
   Phase 1 with real counts, then the container sized (and persistence/RDB
   cadence set) accordingly. This is the main infrastructural unknown.
2. **Relationship index support.** Temporal filtering (`invalid_at IS NULL`)
   across millions of edges may need a relationship index; confirm what the
   installed FalkorDB version supports.
3. **Full GitHub history volume.** Full commit history × many repos is the
   largest single contributor to node count. Measure one real repo in Phase 2
   and extrapolate before backfilling everything.
4. **Embedding model + dimension** choice fixes the vector index shape; changing
   it later means re-embedding and rebuilding indexes.

---

## 5. Connecting new data to the existing graph

Three separate mechanisms, applied in order. Only fall through to the next when
the previous finds nothing.

### Tier 1 — same entity re-ingested (dedup, no LLM)

`MERGE (n:WorkItem {uid: $uid})` on deterministic identity. Re-syncing the same
Jira issue / commit / page matches the existing node and updates its properties.
Free, from `SourceRecord.record_key`.

### Tier 2 — exact cross-reference anchors (no LLM)

Scan new content for literal identifiers that already exist in the graph: Jira
keys (`HERA-101`), Jira/Notion/GitHub URLs, commit SHAs, `owner/repo` names,
verified account emails. A unique match creates the edge directly with
`confidence = 1.0`, `extraction_method = 'deterministic'`.

Reuses `graph/bridge/anchors.py`.

### Tier 3 — semantic similarity (vector + LLM, budgeted)

For content with no exact anchor:

1. Vector top-K search over existing nodes using the new node's embedding — cheap, no LLM.
2. Filter to compatible label pairs only (Decision↔Commit yes, Person↔Commit no).
3. Send only the surviving candidates to LLM adjudication with both sides' evidence spans. Output is schema-validated: `LINK | NO_LINK | NEEDS_REVIEW`, relation, confidence, evidence.
4. Policy:

| Result | Action |
|---|---|
| Exact unique identifier (Tier 2) | Activate deterministically |
| confidence ≥ 0.92 + evidence from both sides | Activate |
| confidence 0.75–0.92 | Human review queue — surfaced in UI, never auto-linked |
| confidence < 0.75, or conflicting identifiers | Reject, cache the negative |

5. Cache every decision keyed by `(hash_a, hash_b, prompt_version, resolver_version)`.

**Similarity score alone never creates an edge.** Wrong edges poison every future
query, so a topical match with no supporting evidence goes to review, not to the graph.

`SAME_AS` on `:Person` requires a verified stable identifier (matching verified
email or account id) or explicit human approval — never name similarity.

---

## 6. Query layer

Answers must be traceable, so every returned claim carries fact + verbatim
evidence + source URL + validity window.

- **Keyword search** — `db.idx.fulltext.queryNodes` (BM25).
- **Semantic search** — `db.idx.vector.queryNodes` (cosine).
- **Fusion** — reciprocal rank fusion computed in Python over both result sets
  (this is what Graphiti's `NODE_HYBRID_SEARCH_RRF` did internally).
- **Graph expansion** — bounded traversal (2 hops) from the seed nodes to pull in
  connected context, ranked by graph distance.
- **Temporal filter** — default to live facts (`invalid_at IS NULL`); history mode
  returns superseded facts with their windows.

---

## 6a. UI and agent surface

Both surfaces are preserved — humans inspect the graph visually, agents query it
over MCP. Every component's shape stays; only what sits behind it changes from
Graphiti to the new query layer (§6).

### 6a.1 Web UI — `demo_ui/` (FastAPI + React/Vite)

| Component | Purpose | Action |
|---|---|---|
| `frontend/src/components/GraphCanvas.tsx` | Interactive graph visualization | Keep. Same node/edge JSON contract; backend fills it from Cypher instead of Graphiti |
| `frontend/src/components/ChatPanel.tsx` | Grounded chat with provenance | Keep. Rewire to the new query layer; answers must show evidence + source URL + validity window |
| `frontend/src/components/{Jira,GitHub,Notion}Panel.tsx` | Connector connect/select/sync | Keep as-is — they call backend routes, not the graph |
| `frontend/src/components/SyncProgress.tsx` | Live ingestion progress | Keep. Extend counters for the two-pass model: deterministic done / semantic pending / budget remaining |
| `frontend/src/components/BridgePanel.tsx` | Cross-source links + evidence | Keep, repurpose as the **Tier-3 review queue** UI (§5) — approve/reject pending links |
| `frontend/src/{api.ts,types.ts}` | API client + shared types | Update types for the new node/edge/fact shape |
| `backend/app.py` | App wiring, graph + health routes | Replace `build_graphiti()` with the FalkorDB client |
| `backend/{jira,github,notion}_routes.py` | Per-connector sync endpoints | Keep routes; swap the ingestion call to the new pipeline |
| `backend/bridge_routes.py` | Link/review endpoints | Keep, retarget to the review queue |
| `backend/{sharepoint,bitbucket}_routes.py` | Other connectors | Out of scope for Phases 1–3; leave untouched or disable |

New backend surface needed for the two-pass model:

```
GET  /api/graph                 nodes+edges for the canvas (filters: provider, label, time)
GET  /api/search                hybrid BM25+vector+RRF results with provenance
GET  /api/facts/{uid}/history   temporal windows for a fact
GET  /api/reviews?state=pending Tier-3 candidates awaiting approval
POST /api/reviews/{id}/approve  activate a reviewed link
POST /api/reviews/{id}/reject   reject + cache the negative
GET  /api/sync/{run_id}         progress incl. semantic budget/queue state
```

### 6a.2 Agent access — inside `demo_ui`, no MCP

**Decided: `mcp/server.py` is out of scope and not ported.** No external MCP
clients, no `.cursor/mcp.json` wiring. The `mcp` dependency leaves the project.

The agent runs inside `demo_ui` — `ChatPanel.tsx` talking to backend endpoints
that sit on the query layer (§6). The agent's capabilities are therefore the
backend routes themselves, not MCP tools:

| Capability | Endpoint |
|---|---|
| Search facts / entities (hybrid, with provenance) | `GET /api/search` |
| Entity context (bounded 2-hop traversal) | `GET /api/graph?anchor=…` |
| Fact history (temporal windows) | `GET /api/facts/{uid}/history` |
| What sources exist in the graph | `GET /api/sources` |
| Grounded answer with citations | `POST /api/chat` |

**Consequence of the single-graph decision.** The old code's `group` concept
meant "a separate physical FalkorDB graph per source". In a unified graph that
disappears: there is nothing to select between, so `group` becomes an optional
**filter** on requests (`WHERE sr.provider IN $providers`) rather than a graph
selector. Anything that called `FalkorDB.list_graphs()` is replaced by a
`:SourceRecord` aggregation.

### 6a.3 Streamlit apps — `ui/` — retired

**Decided: `demo_ui/` is the only UI. The Streamlit apps are not ported.**

`ui/graph_app.py`, `ui/chat_app.py`, `ui/hub.py`, `ui/graph_view.py` duplicate
what `demo_ui/` already does, and `ui/graph_view.py` is the single most
Graphiti-coupled file in the repo (typed on `graphiti_core` node/edge classes,
plus a per-group driver-clone workaround for FalkorDB).

Consequences:

- Drop the `graph` (8501) and `chat` (8502) services from `docker-compose.yml`;
  only `demo` (8000) and `falkordb` remain.
- `streamlit` and `pyvis` leave the dependency list.
- Keep `ui/graph_view.py` as a **read-only reference while implementing** — it
  documents the actual Cypher-level schema and FalkorDB driver quirks — then
  delete it. Do not copy it into the new codebase.

---

## 6b. RDF exports — SKOS and full backup

Both are kept. Neither is hard to port: the SKOS exporter's Graphiti coupling is
**type annotations only** (it reads `.name`, `.summary`, `.labels`, `.uuid`,
`.group_id` off nodes and `.name`, `.fact`, endpoints off edges), and the backup
script never imported Graphiti at all.

### 6b.1 SKOS/Turtle export — `graph/skos_export.py`

Already wired end to end: `build_skos_turtle(nodes, edges)` →
`GET /api/export/skos` (`demo_ui/backend/app.py`) → download button in
`App.tsx` via `api.ts`. Keep all three.

How the mapping works (and why): SKOS only defines generic semantic relations
(`skos:broader`, `skos:narrower`, `skos:related`) and has no custom named
predicates. So every fact is emitted twice — as a plain `skos:related` triple,
plus a **reified `ctx:Fact`** blank node that preserves the real relation name
and fact text. Nothing is lost; it just isn't expressible as native SKOS.

Changes required:

| Change | Why |
|---|---|
| Swap input types from `EntityNode`/`EntityEdge` to our node/edge shapes | Only attribute reads, no behaviour |
| `skos:inScheme` derived per **provider** (via the linked `:SourceRecord`) instead of per `group_id` | `group_id` does not exist in a unified graph; one ConceptScheme per source keeps the useful grouping |
| Enrich the reified `ctx:Fact` with `ctx:validFrom` / `ctx:validTo` / `ctx:confidence` / `ctx:extractionMethod` / `ctx:sourceRecord` / `skos:note` (evidence) | Our edges carry real provenance + temporal data; the current exporter drops all of it. Finally uses the already-declared but unused `xsd:` prefix for dateTime literals |
| Add a `include_history` flag — default **live facts only** (`invalid_at IS NULL`) | Otherwise a superseded fact exports as if it were still true |
| Rename URN namespace `urn:graphiti-context:` → project URN | Graphiti is gone |

### 6b.2 Full RDF backup — `scripts/backup_falkordb_rdf.py`

Already Graphiti-free (imports only `falkordb`). Dumps the whole store as
N-Quads with a manifest (statement count + sha256), optionally clearing the
graph afterwards — this is what produced `backups/*.nq`.

Changes required: it iterates `client.list_graphs()` to walk every per-source
graph; in a unified graph that collapses to the single configured graph. Also
rename the `urn:graphiti-falkor:` namespace. Keep the manifest/sha256 behaviour
as-is — that is the safety net before any destructive reset.

Both exporters are the reason a destructive FalkorDB wipe is acceptable during
development: take a backup first, and the graph is reproducible from the
connectors anyway.

---

## 7. What to take from `graphiti_context_explorer`

### Copy as-is (zero Graphiti coupling)

| Path | What it is |
|---|---|
| `connectors/core/models.py` | `SourceRecord`, `SourceDeletion`, `SourceSelection`, `SourceAccess`, `SourceBreadcrumb` |
| `connectors/core/hashing.py` | canonical content hashing |
| `connectors/core/actions.py` | INSERT / UPDATE / KEEP / DELETE resolution |
| `connectors/core/ledger.py` | connector ledger — **extend** with `semantic_status` + `record_key → edge_ids` |
| `connectors/core/oauth_store.py` | encrypted OAuth token store |
| `connectors/core/runner.py` | `prepare_record` — normalize + hash + chunk |
| `connectors/core/text.py` | canonical textual representation |
| `connectors/core/purge.py` | purge helpers |
| `connectors/jira/{api,oauth}.py` | Jira API client + OAuth |
| `connectors/github_app/{api,auth,store}.py` | GitHub App auth + API |
| `connectors/notion/{api,notion,oauth}.py` | Notion API + OAuth |
| `connectors/ingest/pdf_loader.py` | `chunk_text` utility |
| `connectors/core/chunking/*` | Semantic + AST chunking engine (`router.py` picks the route, `tokens.py` does `cl100k_base` recount) — confirmed zero Graphiti references; `runner.py` (already in copy-as-is) imports this directly |
| `graph/bridge/anchors.py` | Jira key / URL / SHA / email extraction |
| `scripts/backup_falkordb_rdf.py` | Full N-Quads RDF backup + manifest/sha256 — already Graphiti-free (§6b.2) |
| `docker-compose.yml`, `Dockerfile`, `Makefile`, `scripts/` | infra (FalkorDB container already correct) — drop the `graph`/`chat` Streamlit services |
| `demo_ui/frontend/` | React UI: `GraphCanvas`, `ChatPanel`, connector panels, `SyncProgress`, `BridgePanel` — components keep their shape, only types/API calls update |

### Copy and rewrite heavily (right idea, wrong shape)

| Path | Why |
|---|---|
| `connectors/ingest/graph_store.py` | Already writes Cypher `MERGE` to FalkorDB — our starting point. Needs batching, provenance properties, temporal fields, index bootstrap. |
| `connectors/ingest/graph_extraction.py` | litellm structured-output extraction pattern — needs per-profile typed schemas instead of one generic `KnowledgeGraph`. |
| `graph/ontology.py`, `graph/profiles.py` | Keep the entity/edge **type definitions and their prompt-engineered docstrings** as reference; drop `graphiti_kwargs()` plumbing. |
| `graph/bridge/{models,policy,prompt,store}.py` | Adjudication models, thresholds, prompt, decision cache — adapt to the unified-graph schema. |
| `graph/bridge/candidates.py` | Candidate filtering — retarget to FalkorDB vector search. |
| `graph/skos_export.py` | SKOS/Turtle exporter — only type annotations reference Graphiti; retarget per §6b.1 |
| `demo_ui/backend/app.py`, `demo_ui/backend/{jira,github,notion}_routes.py`, `demo_ui/backend/bridge_routes.py` | Route structure reusable; every `build_graphiti()` call replaced, new endpoints from §6a.1 added. |

### Do not copy — rebuild

| Path | Reason |
|---|---|
| `graph/client.py` | Graphiti + `FalkorDriver` connection builder |
| `graph/source_ingest.py`, `graph/{jira,github,notion,sharepoint,bitbucket}_ingest.py` | All built on `graphiti.add_episode()` |
| `graph/context_ops.py` | Built on Graphiti's `SearchConfig` / RRF recipes / bi-temporal edges |
| `graph/llm_chat.py`, `graph/ask.py` | Consume `context_ops` + Graphiti search |
| `ui/` (all Streamlit: `graph_app.py`, `chat_app.py`, `hub.py`, `graph_view.py`, `common.py`, `chat_examples.py`, `bootstrap.py`) | Retired per §6a.3. Keep `graph_view.py` open as a schema/driver reference while implementing, then delete |
| `mcp/server.py`, `.cursor/mcp.json` | Out of scope per §6a.2 |
| `graph/bridge/resolver.py` | Assumes separate physical graphs + projection nodes; unnecessary in a unified graph |
| `graph/episode.py`, `graph/pdf_chunker.py` | Shaped around Graphiti episodes |
| `demo_ui/backend/{sharepoint,bitbucket}_routes.py` | Connectors out of scope for Phases 1–3 |

### Reference docs worth keeping

- `cross_source_graph.md` — the linking/adjudication policy and evidence rules are sound even though the storage model changes.
- `ingest.md` §14 — the source-object → canonical-kind → profile mapping table maps almost directly onto our deterministic schema.

---

## 7a. Pydantic usage and dependency changes

### Where Pydantic is used — the LLM boundary only

Pydantic earns its place exactly where data is untrusted (model output):

1. **Per-profile extraction schemas** — `openai>=3.6` accepts Pydantic models
   directly for schema-constrained structured output.
2. **Adjudication output** — typing `decision` as `Literal["LINK","NO_LINK","NEEDS_REVIEW"]`
   and `relation` as a `Literal[...]` of allow-listed names makes §9's
   "unknown relation types are rejected, not stored" a validation guarantee
   rather than a hand-written check.
3. **Ported models** — `graph/ontology.py`'s Pydantic entity/edge models are
   reusable as-is once the Graphiti plumbing is dropped. Their docstrings are
   prompt engineering, not documentation; keep them verbatim.

### Where Pydantic is deliberately not used — the hot path

- `SourceRecord` stays a `@dataclass(frozen=True)`. At millions of records,
  per-instance Pydantic validation in a tight ingestion loop is measurable
  overhead, and the existing `frozen=True` + `__post_init__` (freezing metadata
  into a `MappingProxyType`) is load-bearing: content hashing depends on the
  object not mutating after construction.
- Batched `UNWIND` write rows stay plain dicts — no per-row validation across
  thousands of rows per batch.

Rule: **Pydantic at the model input/output boundary, dataclasses and dicts where
volume lives.**

### Dependency changes required

Verified against the current `pyproject.toml` / `uv.lock`:

| Package | Current state | Action |
|---|---|---|
| `falkordb` (1.7.1) | Installed **only** via the `graphiti-core[falkordb]` extra; not a declared direct dependency | Must be declared explicitly — removing `graphiti-core` otherwise silently removes the FalkorDB client |
| `pydantic` (2.13.5) | Present only transitively (fastapi, graphiti-core) | Declare explicitly, since it becomes a direct import |
| `litellm` | **Not installed at all** — yet imported by `connectors/ingest/graph_extraction.py`, so that module cannot run today and was evidently never wired up | Do not adopt it. Use the already-declared `openai` client, which supports Pydantic-native structured output |
| `graphiti-core[falkordb]` | Declared | Remove once Phases 0–5 no longer import it |
| `streamlit`, `pyvis` | Declared, used only by the retired `ui/` Streamlit apps | Remove (§6a.3) |
| `mcp` | Declared, used only by `mcp/server.py` | Remove (§6a.2) |

---

## 8. Build phases

**Phase 0 — foundation**
FalkorDB client (no Graphiti), schema + index bootstrap, batched `UNWIND` writer,
`:SourceRecord` provenance model, extended ledger (`semantic_status`, edge map).

**Phase 1 — Jira end-to-end**
Deterministic pass (Project / WorkItem / Person + structural edges), temporal fact
handling, budgeted semantic pass, hybrid search over the result, visible in the UI.
Ingest one real project and inspect the graph.

**Phase 2 — GitHub**
Same pattern. Full history backfill, AST chunking for code, Repository /
SourceFile / Commit / PullRequest. Measure real node/edge/RAM counts here.

**Phase 3 — Notion**
Same pattern. One page first, then a workspace.

**Phase 4 — cross-source linking**
Tier 2 anchors, then Tier 3 vector + LLM adjudication, decision cache, review-queue UI.

**Phase 5 — query + chat layer**
BM25 + vector + RRF, bounded graph expansion, temporal modes, provenance-cited answers.

---

## 9. Non-negotiables

- No OAuth tokens, refresh tokens, private keys, or signed URLs in the graph, in
  prompts, or in logs — stable IDs and safe public URLs only.
- Unchanged records never reach chunking, the LLM, or the embedding API.
- Every persisted fact carries `source_record_keys` and `extraction_method`.
- Unknown/unlisted relation types are rejected, not stored.
- Indexes exist before the first bulk load.
- Similarity score alone never activates an edge.
