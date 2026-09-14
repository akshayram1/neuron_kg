# Notion → Neuron graph

How a workspace sync becomes Falkor nodes and edges. Pass A writes the page tree. **Pass B (Luna) runs only here** — Jira and Bitbucket stay `NOT_APPLICABLE`.

Base API: `https://api.notion.com/v1` (`Notion-Version: 2026-03-11`)  
Auth: per-user OAuth (`api.notion.com/v1/oauth`). Pages are chosen in Notion’s own share dialog, not in Neuron.

Sync input: one connected workspace. Fetch = every **page** `/search` returns for that integration (not exhaustive — Notion says so). Databases are not walked as tables; a database row that is a page is just another page.

Record key: `notion:{workspace_id}:{workspace|page}:{id}`  
Unchanged content hash → `KEEP` (graph not rewritten; semantic pass skipped for that page).

---

## 1. Connect and pick (not written to the graph)

| API | Notion returns | Neuron stores |
|---|---|---|
| `GET api.notion.com/v1/oauth/authorize` then `POST …/oauth/token` | access token + workspace id/name | Encrypted token. Cookie `neuron_notion_session`. |
| Notion share picker (in the OAuth window) | which pages the integration may read | Not a Neuron picker. Re-share in Notion to add pages. |

---

## 2. Sync fetch → graph write

`POST /v1/search` (`object=page`, oldest-edited first). Skip archived / trash.  
For each page: `GET /v1/blocks/{id}/children` (paged), flatten to markdown-ish text. Recurse into nested blocks, **not** into `child_page` / `child_database` (those arrive as their own search hits).

Comments, discussion `?d=` threads, users, and database property schemas are **not** fetched.

### Workspace

| API | Graph |
|---|---|
| Workspace id/name on the OAuth token | **`Workspace`**: `name`, `search_text` = `Notion workspace: …`. **Not embedded.** |

### Document (page)

| API | Graph |
|---|---|
| page id, title, url, `last_edited_time`, parent `page_id` | **`Document`**: `name`, `url`, `last_edited_time`, `search_text` = source header + body |
| block children | Body only. No comment nodes. |

**Edges (Pass A, no LLM)**

| Edge | When |
|---|---|
| `Workspace -[:CONTAINS]-> Document` | Always. |
| `Document -[:PARENT_OF]-> Document` | **Parent → child** (opposite of Jira’s child → parent). |
| `Document -[:MENTIONED_IN]-> SourceRecord` | Provenance. |
| `Document -[:DOCUMENTS]-> WorkItem` | Page text contains an exact Jira key **and** that WorkItem is already on the graph. |
| `Document -[:DOCUMENTS]-> Commit` | Page text has `Commit: <sha>` or a `github.com/…/commit/<sha>` URL, and that Commit exists. Bare hex (`294795f`) is **not** enough. |
| `Document -[:DOCUMENTS]-> Repository` | `Repository: owner/repo` or a github.com repo URL that matches a graph `Repository.name`. Bitbucket `rubik_/argus` only matches if the page uses that exact name form. |
| `Document -[:DOCUMENTS]-> SourceFile` | Exact file URL already on a SourceFile. |

Same two-order rule as Bitbucket↔Jira: keys stored on `SourceRecord.anchor_jira_keys` / `anchor_commit_shas` so whichever sync runs second still links.

### Pass B (Luna) — Notion only

Chunks of the page go to the semantic pass. Typical writes:

| Edge | Meaning |
|---|---|
| `Document -[:DEFINES]-> Term` | Extracted glossary-ish phrase. |
| `Term` / `Decision` / `System -[:EXTRACTED_FROM]-> Document` | Provenance of the extraction. |

Then `materialize_around` can infer:

`Document` and `WorkItem` share a live Term/System → `Document -[:DOCUMENTS]-> WorkItem` (`derived`).

That inferred hop almost never fires for Jira tickets: Jira does **not** extract Terms. A WorkItem has no `DEFINES`/`ABOUT` Term unless something else wrote one.

`search_text` example:

```
[SOURCE]
Kind: Document
Name: Argus API gaps — Glossary & Context Cards
Workspace: DataOS (Internal)

Design references (Figma):
…
## Page: Glossary
August v1 intent: Browse the org term tree…
```

`last_edited_time` is on the Document node. Comment times do not exist (comments were never fetched).

---

## 3. How Notion meets Jira and Bitbucket

There is **no** Notion↔Jira or Notion↔Bitbucket API sync. Three graphs share one Falkor name (`neuron__less_token`) and one resolver.

```
Notion page text
    │
    ├─ exact DATAOS-4346 ─────────────► Document -DOCUMENTS-> WorkItem
    ├─ exact Commit: be023b9 ─────────► Document -DOCUMENTS-> Commit
    ├─ exact Repository: rubik_/argus ► Document -DOCUMENTS-> Repository
    │
    └─ Luna Term/Decision/System
            │
            └─ same Term on a WorkItem ► inferred DOCUMENTS
               (Jira never writes that Term today)
```

Bitbucket still reaches Jira by key in the **commit message** (`IMPLEMENTS`). Notion is a third, optional spoke. If the page never writes the key / `Commit:` / repo name, chat can retrieve both nodes by hybrid search and still have **zero** edge between them.

---

## 4. Vector

After write, the Document `search_text` is embedded (`text-embedding-3-small`), same as WorkItem/Commit. Extracted Term/Decision nodes are also embeddable. Workspace is not.

Hybrid search can therefore surface a gap-matrix page for “glossary context cards” even when no `DOCUMENTS` edge exists.

---

## 5. Shape after a sync

```
Workspace
  └─ CONTAINS → Document
                  ├─ PARENT_OF → Document          page tree
                  ├─ DEFINES → Term                Luna
                  ├─ DOCUMENTS → WorkItem          only if exact key in the page
                  ├─ DOCUMENTS → Commit            only if Commit: / github commit URL
                  └─ DOCUMENTS → Repository        only if repo name / github URL

Term / Decision / System ─ EXTRACTED_FROM → Document

every node ─ MENTIONED_IN → SourceRecord
```

---

## 6. Example (live `less_token`)

Five Notion pages in workspace **DataOS (Internal)**. Jira subtree is epic `DATAOS-3833`. Bitbucket is `rubik_/argus` @ `typesense`.

The design page Jira cites:

> WorkItem `DATAOS-4346` — *Argus API — close Glossary & Context Cards gaps (Notion matrix)*  
> Document *Argus API gaps — Glossary & Context Cards*

Humans know they are the same story. The graph does **not** join them.

| Check | Result |
|---|---|
| Gap page text contains `DATAOS-4346` | **No** |
| `DATAOS-4346` description mentions the Notion page | As prose / URL in Jira `search_text` only. Jira `resolve_exact_anchors` ignores **same-provider** keys and does not treat a Notion title as an anchor. |
| Shared Term between that Document and that WorkItem | **None** (Jira extracted no Terms) |
| `Document -DOCUMENTS-> WorkItem DATAOS-4346` | **Missing** |
| `Document -DOCUMENTS-> Commit be023b9` | **Missing** (page has no `Commit:` / github commit URL) |

What **is** on the Notion side:

```
Workspace DataOS (Internal)
  └─ CONTAINS → Document "Argus API gaps — Glossary & Context Cards"
                  └─ DEFINES → Term   search hit projection, data freshness, …

Untitled ─PARENT_OF→ Build with AI ─PARENT_OF→ Platform AI Release Notes - AUG 2026
                                          ─PARENT_OF→ Angle of Thought: …
```

Release notes **do** contain Jira keys (`DATAOS-3836`, `DATAOS-3843`, …) and bare SHAs (`294795f`, …). Still no cross edge:

- those tickets are **not** in the ingested 3833 subtree, so there is no WorkItem to attach
- bare SHAs are not `Commit:` / github URLs, so they are not commit anchors
- even as anchors they would miss: those SHAs are not in the last-100 `typesense` commits

So on this graph the only Jira↔Bitbucket join is the commit message key (`be023b9 -IMPLEMENTS-> DATAOS-4346`, see `Jira.md` / `Bitbucket.md`). Notion sits next to them as searchable prose + extracted Terms, not as a linked third source.

To make the intended triangle, the gap page (or the ticket) has to **say** `DATAOS-4346` / `Commit: be023b9` / `Repository: rubik_/argus` in text we ingest — or Jira would have to extract the same Term names Luna already put on the page.

---

## 7. After write

| Step | What happens |
|---|---|
| Reconcile | Pages that vanish from `/search` are **kept**. Notion search is not a complete inventory. |
| Semantic | Luna over `notion:{workspace_id}:*` chunks that are `PENDING`. |
| Orphans | Shared Term/Decision/System with no remaining `MENTIONED_IN` are removed. |
| Disconnect | Deletes `notion:{workspace_id}:*` records, then orphans. |

---

## 8. Notion can give this — we do not use it

| API / field | Would give | Why skipped |
|---|---|---|
| Comments / discussions (`?d=`) | Review threads | Not fetched. |
| Database query (rows as structured properties) | Status, owner, relation columns | Page body only. |
| Users / people mentions as account ids | Person nodes | Display text in the body at most. |
| Block-level last_edited | Per-paragraph time | Page `last_edited_time` only. |
| Bare commit SHAs | Link to Bitbucket/GitHub commits | Resolver requires `Commit:` or a github.com commit URL. |
| Workspace-wide page list beyond `/search` | Guaranteed complete set | Notion documents `/search` as incomplete. |

---

## 9. Limits (env)

| Variable | Default | Effect |
|---|---|---|
| `NOTION_REQUEST_INTERVAL_SECONDS` | `0.34` | ~3 req/s. Block walks are chatty. |
| Block recurse | depth 20 | Nested toggles / columns. Child pages not inlined. |

Code: `connectors/notion/api.py` (fetch), `graph/notion_pipeline.py` (write), `graph/semantic_pass.py` (Luna), `demo_ui/backend/notion_routes.py` (OAuth + job).
