# Neuron ingest flow — Jira / Bitbucket / Notion → FalkorDB

How the three connectors become one property graph. Not three graphs. Not raw
API JSON. Flattened nodes + typed fact edges, written with `UNWIND … MERGE`.

Field-level fetch tables: `Jira.md`, `Bitbucket.md`, `Notion.md`.  
Function index at the bottom ([§10](#10-code-map--file--function)).

**Jump:** `graph/writer.py` → `upsert_source_records` · `upsert_entities` · `upsert_fact_edges`

---

## 0. Three stores

```
┌─────────────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│ FalkorDB            │     │ Qdrant               │     │ SQLite ledger       │
│ named graph         │     │ rebuildable vectors  │     │ incremental sync    │
│ neuron /            │     │ {uid, label, vec}    │     │ content_hash        │
│ neuron__<slug>      │     │ text-embedding-      │     │ KEEP / INSERT       │
│                     │     │ 3-small, 1536        │     │ which edges a       │
│ nodes + live edges  │     │                      │     │ record supports     │
│ search_text + BM25  │     │ no ACL / provider    │     │ Notion chunk queue  │
│ FactHistory         │     │                      │     │                     │
└─────────────────────┘     └──────────────────────┘     └─────────────────────┘
         ▲                            ▲
         │  source of truth           │  projection of search_text
         └──────── chat / canvas ─────┘
```

OAuth tokens live in `oauth_connectors.sqlite3` and are **not** graph-scoped.
Switching named graphs does not require re-login.

| Store | File → function |
|---|---|
| Pick Falkor graph | `graph/falkor_client.py` → `get_graph` · `build_client` |
| Named graph → 3 physical names | `graph/multigraph.py` → `resolve` · `GraphTarget` |
| Qdrant upsert / search | `graph/vector_store.py` → `upsert_vectors` · `search` · `ensure_collection` |
| Hash / KEEP / edges supported | `connectors/core/ledger.py` → `ConnectorLedger.commit` · `record_edges_batch` |
| Hash compare | `connectors/core/runner.py` → `prepare_record` · `connectors/core/hashing.py` → `record_content_hash` |
| Encrypted OAuth | `connectors/core/oauth_store.py` → `OAuthConnectorStore` (Jira/Bitbucket). Notion: `connectors/notion/oauth.py` → `NotionStore` |

---

## 1. Shared write contract

Every ingested object (ticket, file, commit, PR, page, project, workspace)
goes through the same five steps.

```
┌──────────────┐   hash    ┌─────────┐
│ Provider API │ ────────► │ Ledger  │── KEEP ──► stop (no Falkor write)
└──────────────┘           └─────────┘
                                │ INSERT / UPDATE
                                ▼
                    ┌───────────────────────┐
                    │ 1. MERGE SourceRecord │
                    │ 2. MERGE entity {uid} │
                    │ 3. MENTIONED_IN       │
                    │ 4. MERGE fact edges   │
                    │ 5. embed → Qdrant     │
                    └───────────────────────┘
                                │
                    resolve_exact_anchors
                    resolve_backlinks_for_target
                    materialize_around
```

| Step | File → function |
|---|---|
| KEEP / INSERT / UPDATE | `connectors/core/runner.py` → `prepare_record` · `connectors/core/actions.py` → `resolve_action` |
| 1. MERGE SourceRecord | `graph/writer.py` → `upsert_source_records` |
| 2. MERGE entity `{uid}` | `graph/writer.py` → `upsert_entities` · `make_uid` |
| 3. MENTIONED_IN | `graph/writer.py` → `link_mentioned_in` |
| 4. MERGE fact edges | `graph/writer.py` → `upsert_fact_edges` · `supersede_fact_edges` |
| 5. embed → Qdrant | `graph/jira_pipeline.py` → `_embed_now` (same helper in bitbucket). Notion Luna: `graph/semantic_pass.py` → `_embed` |
| Cross-source after write | `graph/resolver.py` → `resolve_exact_anchors` · `resolve_backlinks_for_target` |
| Derived lifts | `graph/derived.py` → `materialize_around` |

**Identity.** `uid = uuid5(namespace, label|identity-parts)` — `graph/writer.py` → `make_uid`. Same ticket /
commit / page always MERGEs onto the same node.

```
WorkItem     jira|{connection_id}|{cloud_id}:{issue_id}     graph/jira_pipeline.py → work_item_uid
Person       jira-account|{accountId}                       graph/jira_pipeline.py → person_uid
Person       bitbucket-author|{email or name}               graph/bitbucket_pipeline.py → person_uid / pr_person_uid
Repository   bitbucket|{connection_id}|{repo_uuid}          graph/bitbucket_pipeline.py → repository_uid
SourceFile   bitbucket|{connection_id}|{repo_uuid}|{path}   graph/bitbucket_pipeline.py → file_uid
Commit       bitbucket|{connection_id}|{repo_uuid}|{sha}    graph/bitbucket_pipeline.py → commit_uid
PullRequest  bitbucket|{connection_id}|{repo_uuid}|{pr_id}  graph/bitbucket_pipeline.py → pull_request_uid
Workspace    notion|{workspace_id}                          graph/notion_pipeline.py → workspace_uid
Document     notion|{workspace_id}|{page_id}                graph/notion_pipeline.py → document_uid
```

Provider is **not** a property on the entity. It lives on `:SourceRecord`.
A Person from Jira and a Person from Bitbucket are two nodes; `SAME_AS`
joins them when emails match.

**None-safe props.** `n += props` skips `null`. A later write that does not
know `email` must not erase an earlier one.  
**Jump:** `graph/writer.py` → `upsert_entities` (strips `None` before `SET n += row.props`).

**Stubs.** `MERGE (n:WorkItem {uid})` with no props — parent / BLOCKS target
not yet in this batch. The real record fills the same uid via `ON MATCH`.  
**Jump:** `graph/writer.py` → `ensure_node_stub`. Jira callers: `write_issue` (parent + BLOCKS).

---

## 2. Shared schema — SourceRecord, entity, fact edge

### SourceRecord (written first, never embedded)

**Jump:** dataclass `connectors/core/models.py` → `SourceRecord`.  
Row builder: `graph/jira_pipeline.py` → `_source_record_row` · `graph/bitbucket_pipeline.py` → `_source_row` · `graph/notion_pipeline.py` → `_source_row`.  
Write: `graph/writer.py` → `upsert_source_records`.  
Anchor arrays: `graph/resolver.py` → `anchor_properties`. Regexes: `graph/bridge/anchors.py` → `jira_keys` · `commit_shas` · `pull_request_refs` · `repository_names` · `urls`.

```
(:SourceRecord {
  record_key,            // "jira:{conn}:work_item:{cloud}:{id}"  UNIQUE
  provider,              // jira | bitbucket | notion
  connection_id,
  entity_type,           // work_item | commit | page | …
  external_id,
  name, url, content_hash,
  public, principals, policy_version,
  source_created_at,     // Jira created — not copied onto WorkItem
  source_updated_at,
  source_time,           // reference / world time for live edges
  ingested_at, deleted_at,
  anchor_jira_keys,      // ["DATAOS-4346"]
  anchor_commit_shas,    // only Commit: / github.com/…/commit/…
  anchor_repository_names,
  anchor_pull_request_refs,  // "rubik_/argus#12" or "#12"
  anchor_urls
})
```

Anchors make linking **order-independent**. A commit ingested before the
ticket still parks `DATAOS-4346` here; when the WorkItem arrives,
`graph/resolver.py` → `resolve_backlinks_for_target` writes `IMPLEMENTS`.

### Entity (every structural / semantic node)

**Jump:** `graph/writer.py` → `upsert_entities` · `make_uid`. Labels listed in `graph/schema.py` → `ENTITY_LABELS`.

```
(:WorkItem|:Commit|:Document|… {
  uid,                   // MERGE key
  name,
  search_text,           // BM25 + embedding input
  url?,
  first_seen_at,         // set once
  last_seen_at           // bumped on re-ingest
  // plus label-specific props below
})
```

### Fact edge (every typed relationship except MENTIONED_IN)

**Jump:** `graph/writer.py` → `upsert_fact_edges` (create/confirm) · `supersede_fact_edges` (invalidate live) · `_archive_history_rows` (snapshot before invalidate).  
`fact_uid` = `make_uid("Fact", from_uid, rel_type, to_uid)` inside `upsert_fact_edges`.

Facts are **not** reified as `(:Fact)` nodes. Provenance and time live on
the relationship so traversal stays one hop.

```
(WorkItem)-[:ASSIGNED_TO {
  fact_uid,              // uuid5(Fact|from|REL|to)
  valid_at,              // world time — when this was true
  invalid_at: null,      // null = live; set = superseded
  first_seen_at,
  last_confirmed_at,
  source_record_keys: ["jira:…:work_item:…:4346"],
  evidence,              // verbatim span, or null for API fields
  extraction_method,     // deterministic | exact_anchor | changelog | derived | llm
  confidence,            // 1.0 deterministic; 0.6 derived
  derived,               // false unless materialize_around
  derived_rule,          // parent_implements | shared_concept | pr_implements | …
  premise_fact_uids,     // proof chain for derived edges
  chunk_id, chunk_hash,  // Luna only
  extractor_version, model,
  ended_unknown,
  attested_from
}]->(Person)
```

Live query filter: `r.invalid_at IS NULL`.

| Cardinality | Write pattern | Examples |
|---|---|---|
| Single-valued | `supersede_fact_edges` then `upsert_fact_edges` | `ASSIGNED_TO`, `BELONGS_TO`, `PARENT_OF` (Jira) |
| Multi-valued | upsert only | `BLOCKS`, `IMPLEMENTS`, `DOCUMENTS`, `MODIFIES` |

Re-confirming the **same** assignee: supersede sets `invalid_at`, upsert
MERGEs the same triple and clears it (`ON MATCH`). Unchanged assignee stays live.

`MENTIONED_IN` is presence-only (entity → SourceRecord). No temporal fields.  
**Jump:** `graph/writer.py` → `link_mentioned_in`.

### FactHistory (closed world-time windows)

**Jump:** changelog backfill `graph/writer.py` → `upsert_history_intervals` called from `graph/jira_pipeline.py` → `_write_changelog_history` · `connectors/jira/api.py` → `field_intervals`.  
Live-edge archive on supersede: `graph/writer.py` → `_archive_history_rows`.  
Read path: `graph/history.py` → `fetch_fact_history` · `graph/time_axis.py` → `holds_at` · `held_at`.

Hot traversals use live edges. Closed intervals are **nodes**, not dead-edge scans.

```
(:FactHistory {
  uid, fact_uid, from_uid, to_uid, relation,
  valid_from, valid_to,          // world axis
  observed_from, observed_to,    // record axis
  source_record_keys, evidence,
  extraction_method,             // changelog on first Jira sync
  ended_unknown, attested_from
})
```

Jira writes these from changelog on the **first** sync (assignee + status
only). Current assignee is the live `ASSIGNED_TO` edge, not a history row.
Current status is a **WorkItem property**, not a live `HAS_STATUS` edge.
Previous statuses are `FactHistory` with `relation: "HAS_STATUS"`.

---

## 3. Indexes (Falkor)

Created at startup. **Jump:** `graph/schema.py` → `bootstrap_schema` · `_run_idempotent`.  
Called from `demo_ui/backend/app.py` → `_ensure_schema` and each sync (`jira_routes._run`, `bitbucket_routes._run_sync`, `notion_routes._run_sync`).

Without them every MERGE is a label scan.

```
CREATE INDEX FOR (n:SourceRecord) ON (n.record_key)
CREATE INDEX FOR (n:<Label>)      ON (n.uid)          // every entity label
CREATE INDEX FOR (n:FactHistory)  ON (n.fact_uid)

CALL db.idx.fulltext.createNodeIndex('WorkItem',    'search_text')
CALL db.idx.fulltext.createNodeIndex('Document',    'search_text')
CALL db.idx.fulltext.createNodeIndex('Commit',      'search_text')
CALL db.idx.fulltext.createNodeIndex('PullRequest', 'search_text')
CALL db.idx.fulltext.createNodeIndex('SourceFile',  'search_text')
CALL db.idx.fulltext.createNodeIndex('Decision',    'search_text')
CALL db.idx.fulltext.createNodeIndex('Term',        'search_text')
```

No Falkor vector index. Embeddings left Falkor because `vecf32` is
unquantized RAM (~6 KB × 1536-dim per node).  
**Jump:** why-Qdrant docstring `graph/vector_store.py`. Dead writer still in `graph/writer.py` → `upsert_entity_embeddings` (not used on the hot path). Labels that *may* embed: `graph/schema.py` → `VECTOR_LABELS` / `FULLTEXT_LABELS`.

---

## 4. Jira

Pass A only. `semantic_status = NOT_APPLICABLE`. No Luna.

**Fetch.** Jira Cloud search JQL + `expand=changelog`. Whole project, or one
root key + Epic/parent/parentEpic BFS (depth 8). Auth: Atlassian OAuth.

| What | File → function |
|---|---|
| OAuth start / callback / picker | `demo_ui/backend/jira_routes.py` → `jira_oauth_start` · `jira_oauth_callback` · `list_sites` · `list_projects` |
| Token + settings | `connectors/jira/oauth.py` → `JiraOAuthSettings` · `connectors/core/oauth_store.py` |
| Search / subtree | `connectors/jira/api.py` → `JiraApiClient.issues` · `subtree_keys` · `issues_by_keys` · `adf_text` |
| Changelog windows | `connectors/jira/api.py` → `_parse_changelog` · `field_intervals` |
| Job loop | `demo_ui/backend/jira_routes.py` → `_run` · `start_sync` |
| SourceRecord shape | `graph/jira_pipeline.py` → `project_record` · `issue_record` |
| Graph write | `graph/jira_pipeline.py` → `write_project` · `write_issue` |

### Flow

```
OAuth + site/project picker          (not written)
        │
        ▼
POST /rest/api/3/search/jql
  fields: summary, description, status, issuetype,
          assignee, reporter, labels, parent, issuelinks,
          comment, changelog
        │
        ▼
┌─────────────┐     ┌──────────────────────────────────────────┐
│ :Project    │◄────┤ :WorkItem                                │
│             │     │  issue_key, status, issue_type, labels   │
│ not embedded│     │  search_text = summary+desc+comments     │
└─────────────┘     └──────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         :Person         :Person         :WorkItem
         assignee        reporter        parent / blocked
         accountId                       (stub if unseen)
              │
              ▼
         SAME_AS ── if email matches another Person
```

### Nodes

```
(:Project {
  uid, name, url,
  search_text: "Jira project KEY: Name …"
})

(:WorkItem {
  uid, name,                          // "DATAOS-4346 — summary"
  issue_key, url,
  status,                             // live value — NOT an edge
  issue_type, labels,                 // labels are props, not Term nodes
  search_text                         // flattened description + "Comment by …"
})

(:Person {
  uid,                                // jira-account|{accountId}
  name, email
})
```

Created / updated sit on the **SourceRecord only**. They are not WorkItem
props and not in `search_text`. Comment authors are not Persons.

**Jump (node write):** `graph/jira_pipeline.py` → `write_project` · `write_issue` (WorkItem props around the `upsert_entities(..., "WorkItem", …)` call). Flattened body: `issue_record` + `connectors/jira/api.py` → `adf_text`.

### Edges

```
(WorkItem)-[:BELONGS_TO { …fact… }]->(Project)          // single-valued

(WorkItem)-[:ASSIGNED_TO {
  fact_uid,
  valid_at,                    // ticket updated, not the assignment instant
  invalid_at: null,
  first_seen_at, last_confirmed_at,
  source_record_keys: ["jira:{conn}:work_item:{cloud}:{id}"],
  evidence: null,
  extraction_method: "deterministic",
  confidence: 1.0,
  derived: false
}]->(Person)

(WorkItem)-[:REPORTED_BY { …fact… }]->(Person)          // single-valued

(WorkItem)-[:PARENT_OF { …fact… }]->(WorkItem)          // child → parent
                                                        // 4346 → 3839 → 3833

(WorkItem)-[:BLOCKS { …fact… }]->(WorkItem)             // multi; outward only

(Person)-[:SAME_AS {
  extraction_method: "deterministic",
  derived_rule: "verified_email"
}]->(Person)

(WorkItem)-[:MENTIONED_IN]->(SourceRecord)
(Project)-[:MENTIONED_IN]->(SourceRecord)
(Person)-[:MENTIONED_IN]->(SourceRecord)
```

| Edge | File → function |
|---|---|
| `BELONGS_TO` / `ASSIGNED_TO` / `REPORTED_BY` / `PARENT_OF` | `graph/jira_pipeline.py` → `_write_single_valued` (calls `supersede_fact_edges` then `upsert_fact_edges`) |
| `BLOCKS` | `graph/jira_pipeline.py` → `write_issue` (plain `upsert_fact_edges`, + `ensure_node_stub`) |
| `SAME_AS` | `graph/resolver.py` → `link_verified_person_identity` |
| `MENTIONED_IN` | `graph/writer.py` → `link_mentioned_in` from `write_issue` / `write_project` |
| FactHistory assignee/status | `graph/jira_pipeline.py` → `_write_changelog_history` |

```
(:FactHistory { relation: "ASSIGNED_TO", valid_from, valid_to, … })
(:FactHistory { relation: "HAS_STATUS",  valid_from, valid_to, … })
```

### After write

Reconcile is **not** done — tickets that leave the JQL/subtree stay until
disconnect. Disconnect deletes `jira:{connection_id}:*` then orphans.

**Jump:** `demo_ui/backend/jira_routes.py` → `delete_connection` · `graph/jira_pipeline.py` → `delete_record` · `delete_orphaned_shared_entities` · `graph/writer.py` → `delete_orphaned_entities`.

---

## 5. Bitbucket

Pass A only. Files: HEAD `.py` / `.md`. Commits: last N on the chosen branch
(default 100) + one diffstat each. PRs: title/body, cap 100, no diff, no
PR↔commit edge.

| What | File → function |
|---|---|
| OAuth + pickers | `demo_ui/backend/bitbucket_routes.py` → `bitbucket_oauth_start` · `bitbucket_oauth_callback` · `get_workspace` · `list_repositories` · `list_branches` |
| Token | `connectors/bitbucket/oauth.py` → `BitbucketOAuthSettings` |
| Tree / commits / PRs | `connectors/bitbucket/api.py` → `BitbucketApiClient.files` (`walk`) · `commits` · `diffstat` · `attach_diffstats` · `pull_requests` · `resolve_ref` · `parse_diffstat` |
| Job loop | `demo_ui/backend/bitbucket_routes.py` → `_run_sync` · `start_sync` |
| SourceRecord shape | `graph/bitbucket_pipeline.py` → `repository_record` · `file_record` · `commit_record` · `pull_request_record` |
| Graph write | `graph/bitbucket_pipeline.py` → `write_repository` · `write_file` · `write_commit` · `write_pull_request` |

### Flow

```
OAuth + workspace/repo/branch picker     (not written)
        │
        ▼
GET /2.0/repositories/{ws}/{slug}/src/{head}/…     walk .py/.md
GET /2.0/repositories/{ws}/{slug}/commits/{head}   last N
GET /2.0/repositories/{ws}/{slug}/diffstat/{sha}   paths + +/-
GET /2.0/…/pullrequests?state=OPEN,MERGED,DECLINED
        │
        ▼
                    ┌──────────────┐
                    │ :Repository  │
                    └──────┬───────┘
           ┌───────────────┼────────────────┐
           ▼               ▼                ▼
     :SourceFile        :Commit        :PullRequest
     HEAD text          message+Files  title+body
           │               │                │
           │          AUTHORED_BY      AUTHORED_BY
           │               ▼                ▼
           │          :Person           :Person
           │          email-keyed       name-keyed
           │
           └──── REFERENCES → WorkItem     iff exact key in file
                 IMPLEMENTS → WorkItem     iff exact key in commit/PR text
                 MODIFIES   → SourceFile   iff path still ingested HEAD
```

### Nodes

```
(:Repository {
  uid, name, url, default_branch, private, search_text
})

(:SourceFile {
  uid, name, path, language, blob_sha, url,
  search_text                    // full current file
})

(:Commit {
  uid, name,                     // "abc1234 — first line"
  sha, authored_at, url,
  search_text                    // message + Files: block (no hunk)
})

(:PullRequest {
  uid, name,                     // "PR #12 — title"
  state, source_branch, destination_branch,
  pr_id, pr_ref,                 // "rubik_/argus#12"
  url, search_text
})

(:Person {
  uid,                           // bitbucket-author|{email or name}
  name, email?
})
```

Parents of a commit are **not** stored. Diffstat failure leaves `files`
empty and the sync continues.

**Jump (node write):** `write_repository` · `write_file` · `write_commit` · `write_pull_request` in `graph/bitbucket_pipeline.py`. Author split: `connectors/bitbucket/api.py` → `_split_author`. `pr_ref`: `pull_request_ref`.

### Edges

```
(Repository)-[:CONTAINS { …fact… }]->(SourceFile)
(Repository)-[:CONTAINS { …fact… }]->(Commit)
(Repository)-[:CONTAINS { …fact… }]->(PullRequest)

(Commit)-[:AUTHORED_BY { …fact… }]->(Person)
(PullRequest)-[:AUTHORED_BY { …fact… }]->(Person)

(Commit)-[:MODIFIES {
  fact_uid,
  valid_at, invalid_at: null,
  source_record_keys: ["bitbucket:…:commit:…:{sha}"],
  evidence: "modified +12/-3",
  extraction_method: "deterministic",
  confidence: 1.0
}]->(SourceFile)

(Commit)-[:IMPLEMENTS {
  fact_uid,
  valid_at, invalid_at: null,
  source_record_keys: ["bitbucket:…:commit:…:{sha}"],
  evidence: "…DATAOS-4346 Added ai_instructions…",
  extraction_method: "exact_anchor",
  confidence: 1.0,
  extractor_version: "tier2-v1"
}]->(WorkItem)

(PullRequest)-[:IMPLEMENTS { extraction_method: "exact_anchor" }]->(WorkItem)
(SourceFile)-[:REFERENCES { extraction_method: "exact_anchor" }]->(WorkItem)

(Person)-[:SAME_AS]->(Person)     // email only — PR author often cannot
```

`IMPLEMENTS` is **exact Jira key in text**, not “related topic”. A commit
titled “updated pipeline” with no key gets no ticket edge.

Deleted / non-`.py`/`.md` paths stay in commit `search_text` (`Files:`)
and do **not** get `MODIFIES`.

| Edge | File → function |
|---|---|
| `CONTAINS` / `AUTHORED_BY` | `graph/bitbucket_pipeline.py` → `_write_edge` from `write_file` / `write_commit` / `write_pull_request` |
| `MODIFIES` | `graph/bitbucket_pipeline.py` → `modifies_paths` then `_write_edge` inside `write_commit` |
| `IMPLEMENTS` / `REFERENCES` | `graph/resolver.py` → `resolve_exact_anchors` · `_relation` (Commit/PR→WorkItem = IMPLEMENTS, else REFERENCES) |
| `SAME_AS` | `graph/resolver.py` → `link_verified_person_identity` (email; PR author usually skipped) |

### After write

Ledger keys for this repo’s files / commits / PRs that were **not** in the
fetch are deleted, then orphans. Disconnect deletes `bitbucket:{connection_id}:*`.

**Jump:** `graph/bitbucket_pipeline.py` → `reconcile_repository_records`. Disconnect: `demo_ui/backend/bitbucket_routes.py` → `delete_connection`.

---

## 6. Notion

Pass A writes the page tree. **Pass B (Luna) runs only here.** Jira and
Bitbucket stay `NOT_APPLICABLE`.

**Fetch.** `POST /v1/search` (`object=page`). Notion documents this as
incomplete. Then `GET /v1/blocks/{id}/children`, flatten. Comments, DB
properties, and people-mention ids are not fetched.

| What | File → function |
|---|---|
| OAuth | `demo_ui/backend/notion_routes.py` → `notion_oauth_start` · `notion_oauth_callback` · `connectors/notion/oauth.py` → `NotionOAuthClient` · `NotionStore` |
| Search + flatten | `connectors/notion/api.py` → `NotionApiClient.fetch_pages` · `render_children` · `_render_block` · `_rich_text` · `_page_title` |
| Job loop | `demo_ui/backend/notion_routes.py` → `_run_sync` · `start_sync` |
| SourceRecord shape | `graph/notion_pipeline.py` → `workspace_record` · `page_record` |
| Pass A write | `graph/notion_pipeline.py` → `write_workspace` · `write_page` |
| Pass B Luna | `graph/semantic_pass.py` → `run_semantic_pass` · `_call_llm` · `_write_extraction` |

### Flow

```
OAuth + Notion share picker              (not written)
        │
        ▼
POST /v1/search  →  GET /v1/blocks/{id}/children
        │
        ▼
┌─────────────┐     ┌─────────────────────────────────────┐
│ :Workspace  │────►│ :Document                           │
│ not embedded│     │  title, url, last_edited_time       │
└─────────────┘     │  search_text = header + body        │
                    └─────────────────────────────────────┘
                              │
              PARENT_OF (parent → child)     // opposite of Jira
              DOCUMENTS → WorkItem/Commit/PR/Repository/SourceFile
                              │  only if exact anchor in page text
                              ▼
                    semantic_status = PENDING
                              │
                              ▼
                         Luna (Pass B)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
           :Term          :Decision        :System
              │
              EXTRACTED_FROM → Document
              Document -DEFINES-> Term
```

### Nodes

```
(:Workspace {
  uid, name,
  search_text: "Notion workspace: …"
})

(:Document {
  uid, name, url, last_edited_time,
  search_text
})

(:Term { uid, name, definition?, aliases?, search_text })
(:Decision { uid, name, statement?, rationale?, status?, search_text })
(:System { uid, name, purpose?, search_text })
```

**Jump (node write):** `write_workspace` · `write_page` in `graph/notion_pipeline.py`. Luna nodes: `graph/semantic_pass.py` → `_write_extraction` · `semantic_uid`. Pydantic shapes: `graph/ontology.py` → `Term` · `Decision` · `System`.

### Edges — Pass A

```
(Workspace)-[:CONTAINS { …fact… }]->(Document)

(Document)-[:PARENT_OF { …fact… }]->(Document)     // parent → child

(Document)-[:DOCUMENTS {
  fact_uid,
  valid_at, invalid_at: null,
  source_record_keys: ["notion:{workspace}:page:{id}"],
  evidence: "…DATAOS-4346…",
  extraction_method: "exact_anchor",
  confidence: 1.0,
  extractor_version: "tier2-v1"
}]->(WorkItem)

(Document)-[:DOCUMENTS { extraction_method: "exact_anchor" }]->(Commit)
(Document)-[:DOCUMENTS { extraction_method: "exact_anchor" }]->(PullRequest)
(Document)-[:DOCUMENTS { extraction_method: "exact_anchor" }]->(Repository)
(Document)-[:DOCUMENTS { extraction_method: "exact_anchor" }]->(SourceFile)
```

**Jump (Pass A edges):** `CONTAINS` / `PARENT_OF` from `graph/notion_pipeline.py` → `_edge` inside `write_page`. `DOCUMENTS` from `graph/resolver.py` → `resolve_exact_anchors` · `_relation` (Document → those labels = DOCUMENTS).

Anchor rules (no LLM):

| Target | What must appear in the page | Regex |
|---|---|---|
| WorkItem | exact key `DATAOS-4346` **and** that WorkItem already on the graph | `graph/bridge/anchors.py` → `jira_keys` |
| Commit | `Commit: <sha>` or `github.com/…/commit/<sha>` — bare hex is not enough | `commit_shas` |
| PullRequest | Bitbucket/GitHub PR URL, or `PR #12` if exactly one ingested PR has that id | `pull_request_refs` |
| Repository | `Repository: owner/repo` or a matching github.com URL | `repository_names` |
| SourceFile | exact file URL already on a SourceFile | `urls` |

### Edges — Pass B (Luna)

```
(Document)-[:DEFINES { extraction_method: "llm", chunk_id, model }]->(Term)
(Term)-[:EXTRACTED_FROM { … }]->(Document)
(Decision)-[:EXTRACTED_FROM { … }]->(Document)
(System)-[:EXTRACTED_FROM { … }]->(Document)
(Decision)-[:APPLIES_TO]->(Term|System|WorkItem|…)
(Decision)-[:DECIDED_BY]->(Person)          // by name reference; LLM does not create Person
```

Allowed Luna pairs: `graph/ontology.py` → `RELATION_TYPE_MAP` · `is_relation_allowed`. Unknown types
are rejected, not stored.  
**Jump:** `graph/semantic_pass.py` → `_write_extraction` · `run_semantic_pass`. Profile/prompt: `graph/profiles.py` → `profile_for_record`.

### After write

Pages that vanish from `/search` are **kept** (search is not a complete
inventory). Luna runs over `PENDING` `notion:{workspace_id}:*` chunks.
Disconnect deletes `notion:{workspace_id}:*` then orphans.

**Jump:** Luna kickoff `demo_ui/backend/notion_routes.py` → `_run_sync` (calls `run_semantic_pass`). Chunks saved in `write_page` via `ConnectorLedger.save_chunks`. Disconnect: `notion_routes.delete_connection` · `connectors/notion/oauth.py` → `NotionStore.delete_connection`.

---

## 7. Cross-source join

There is no Jira↔Bitbucket↔Notion sync API. One Falkor name, one resolver.

**Jump:** `graph/resolver.py` → `resolve_exact_anchors` · `resolve_backlinks_for_target` · `_targets` · `_relation` · `link_verified_person_identity`.  
Parked ids: `anchor_properties`. Regex: `graph/bridge/anchors.py`.

```
                    SAME_AS (verified email)
         Jira Person ──────────────────── Bitbucket Person
                │
           ASSIGNED_TO
                │
           :WorkItem
                ▲
                │ IMPLEMENTS          exact key in commit / PR / file
           :Commit / :PullRequest / :SourceFile
                ▲
                │ DOCUMENTS           exact key / Commit: / PR URL / repo
           :Document
                │
             DEFINES (Luna)
                ▼
              :Term
                │
                └── shared Term with a WorkItem
                    → derived Document -DOCUMENTS-> WorkItem
                    (almost never fires: Jira extracts no Terms)
```

Relation chosen by endpoint labels (`graph/resolver.py` → `_relation`):

```
Commit | PullRequest  →  WorkItem     = IMPLEMENTS
Document              →  WorkItem | Commit | PR | Repository | SourceFile
                                      = DOCUMENTS
anything else         →  anything     = REFERENCES
```

### Derived lifts (`materialize_around`)

**Jump:** `graph/derived.py` → `materialize_around` · `_lift_through_parent` · `_shared_concept_documents` · `_document_via_pr` · `_write_derived`.

Asserted edges always win. Derived rows are `derived: true`, `confidence: 0.6`.

```
(Commit)-[:IMPLEMENTS { derived: true, derived_rule: "parent_implements" }]->(parent WorkItem)
    // _lift_through_parent  — Commit IMPLEMENTS child, child PARENT_OF parent

(Document)-[:DOCUMENTS { derived: true, derived_rule: "parent_documents" }]->(parent WorkItem)
    // _lift_through_parent

(Document)-[:DOCUMENTS { derived: true, derived_rule: "shared_concept" }]->(WorkItem)
    // _shared_concept_documents — both live-linked to the same Term|System

(Document)-[:DOCUMENTS { derived: true, derived_rule: "pr_implements" }]->(WorkItem)
    // _document_via_pr — Document DOCUMENTS PR, PR IMPLEMENTS WorkItem
```

---

## 8. What Falkor actually holds for the live triangle

Scope on `less_token`: Jira subtree `DATAOS-3833`, Bitbucket `rubik_/argus`
@ `typesense`, Notion workspace **DataOS (Internal)**.

```
(:Project { name: "DATAOS" })

(:WorkItem {
  issue_key: "DATAOS-4346",
  status: "In Progress",
  name: "DATAOS-4346 — Argus API — close Glossary & Context Cards gaps …"
})

(:Person { name: "Aashish Verma" })          // jira-account
(:Person { name: "Aashish Verma" })          // bitbucket-author (email)

(:Commit {
  sha: "be023b9…",
  search_text: "DATAOS-4346 Added ai_instructions, …\nFiles:\n  modified  plan.py"
})

(:Document { name: "Argus API gaps — Glossary & Context Cards" })
```

Written:

```
(4346)-[:BELONGS_TO]->(DATAOS)
(4346)-[:ASSIGNED_TO { invalid_at: null }]->(Jira Aashish)
(4346)-[:PARENT_OF]->(3839)-[:PARENT_OF]->(3833)

(be023b9)-[:IMPLEMENTS {
  extraction_method: "exact_anchor",
  evidence: "DATAOS-4346 Added ai_instructions…"
}]->(4346)

(Jira Aashish)-[:SAME_AS]->(BB Aashish)     // if emails match
```

**Not** written, even though humans treat them as one story:

```
(gap page)-[:DOCUMENTS]->(4346)            // page text has no DATAOS-4346
(gap page)-[:DOCUMENTS]->(be023b9)         // no Commit: / github commit URL
```

Chat can still retrieve both by hybrid search (`graph/search.py` → `hybrid_search` · `graph/chat.py` → `run_chat_turn`). The graph has **zero** edge
between that Document and that WorkItem until the page (or ticket) states
the key.

`ce9b313` (*updated pipeline*) is the other miss: three files in `Files:`
(`graph/bitbucket_pipeline.py` → `commit_record` / `_file_change_line`),
no `MODIFIES` (`modifies_paths` — not HEAD `.py`/`.md`), no `IMPLEMENTS`
(`resolve_exact_anchors` — no ticket key).

---

## 9. Reconcile / delete

| Source | Missing from this fetch | Disconnect |
|---|---|---|
| Jira | **Kept** until disconnect | `DELETE` `jira:{connection_id}:*` then orphans |
| Bitbucket | Files / commits / PRs **deleted** from graph | `bitbucket:{connection_id}:*` then orphans |
| Notion | Pages **kept** (`/search` is incomplete) | `notion:{workspace_id}:*` then orphans |

Orphans = shared entities (`Person`, `Term`, `Decision`, `System`) with no
remaining `MENTIONED_IN`. Structural WorkItems are deleted by uid when
their records go.

| Action | File → function |
|---|---|
| Drop one record + its exclusive edges | `graph/jira_pipeline.py` → `delete_record` · `graph/writer.py` → `remove_record_support` · `delete_source_record` · `delete_node` |
| Bitbucket missing keys | `graph/bitbucket_pipeline.py` → `reconcile_repository_records` |
| Shared orphans | `graph/writer.py` → `delete_orphaned_entities` · `graph/jira_pipeline.py` → `delete_orphaned_shared_entities` |
| Jira disconnect | `demo_ui/backend/jira_routes.py` → `delete_connection` |
| Bitbucket disconnect | `demo_ui/backend/bitbucket_routes.py` → `delete_connection` |
| Notion disconnect | `demo_ui/backend/notion_routes.py` → `delete_connection` |

---

## 10. Code map — file → function

Cmd/Ctrl-click the path, then search the function name.

### Writer / schema / stores

| File | Functions |
|---|---|
| `graph/writer.py` | `make_uid` · `upsert_source_records` · `upsert_entities` · `ensure_node_stub` · `link_mentioned_in` · `upsert_fact_edges` · `supersede_fact_edges` · `_archive_history_rows` · `upsert_history_intervals` · `remove_record_support` · `delete_orphaned_entities` |
| `graph/schema.py` | `bootstrap_schema` · `ENTITY_LABELS` · `FULLTEXT_LABELS` · `VECTOR_LABELS` |
| `graph/falkor_client.py` | `get_graph` · `build_client` |
| `graph/multigraph.py` | `resolve` · `GraphTarget` · `GraphRegistry` |
| `graph/vector_store.py` | `upsert_vectors` · `search` · `truncate_for_embedding` · `ensure_collection` |
| `connectors/core/models.py` | `SourceRecord` |
| `connectors/core/runner.py` | `prepare_record` |
| `connectors/core/hashing.py` | `record_content_hash` |
| `connectors/core/ledger.py` | `ConnectorLedger.commit` · `record_edges_batch` · `save_chunks` · `edges_for_record` |
| `connectors/core/oauth_store.py` | `OAuthConnectorStore` |

### Jira

| File | Functions |
|---|---|
| `demo_ui/backend/jira_routes.py` | `jira_oauth_start` · `jira_oauth_callback` · `list_sites` · `list_projects` · `_run` · `start_sync` · `delete_connection` |
| `connectors/jira/oauth.py` | `JiraOAuthSettings` |
| `connectors/jira/api.py` | `JiraApiClient.issues` · `subtree_keys` · `issues_by_keys` · `adf_text` · `field_intervals` |
| `graph/jira_pipeline.py` | `issue_record` · `work_item_uid` · `person_uid` · `write_project` · `write_issue` · `_write_single_valued` · `_write_changelog_history` · `_embed_now` |

### Bitbucket

| File | Functions |
|---|---|
| `demo_ui/backend/bitbucket_routes.py` | `bitbucket_oauth_start` · `list_repositories` · `list_branches` · `_run_sync` · `start_sync` · `delete_connection` |
| `connectors/bitbucket/oauth.py` | `BitbucketOAuthSettings` |
| `connectors/bitbucket/api.py` | `BitbucketApiClient.files` · `commits` · `diffstat` · `attach_diffstats` · `pull_requests` · `resolve_ref` · `parse_diffstat` · `_split_author` |
| `graph/bitbucket_pipeline.py` | `file_record` · `commit_record` · `write_file` · `write_commit` · `write_pull_request` · `modifies_paths` · `_write_edge` · `person_uid` · `pr_person_uid` · `reconcile_repository_records` |

### Notion

| File | Functions |
|---|---|
| `demo_ui/backend/notion_routes.py` | `notion_oauth_start` · `_run_sync` · `start_sync` · `delete_connection` |
| `connectors/notion/oauth.py` | `NotionStore` · `NotionOAuthClient` |
| `connectors/notion/api.py` | `NotionApiClient.fetch_pages` · `render_children` · `_render_block` · `_rich_text` |
| `graph/notion_pipeline.py` | `page_record` · `write_workspace` · `write_page` · `_edge` |
| `graph/semantic_pass.py` | `run_semantic_pass` · `_call_llm` · `_write_extraction` · `semantic_uid` |
| `graph/ontology.py` | `Term` · `Decision` · `System` · `RELATION_TYPE_MAP` · `is_relation_allowed` |
| `graph/profiles.py` | `profile_for_record` |

### Join / query

| File | Functions |
|---|---|
| `graph/bridge/anchors.py` | `jira_keys` · `commit_shas` · `pull_request_refs` · `repository_names` · `urls` · `evidence_excerpt` |
| `graph/resolver.py` | `anchor_properties` · `_targets` · `_relation` · `resolve_exact_anchors` · `resolve_backlinks_for_target` · `link_verified_person_identity` |
| `graph/derived.py` | `materialize_around` · `_lift_through_parent` · `_shared_concept_documents` · `_document_via_pr` · `_write_derived` |
| `graph/search.py` | `hybrid_search` |
| `graph/chat.py` | `run_chat_turn` |
| `graph/structured_query.py` | `resolve_structured` |
| `graph/time_axis.py` | `holds_at` · `held_at` · `infer_query_clocks` |
| `graph/history.py` | `fetch_fact_history` |
| `graph/entity.py` | `fetch_entity_detail` |
| `graph/graph_view.py` | `fetch_graph` |
