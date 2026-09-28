# Jira → Neuron graph

How one project (or epic subtree) sync becomes Falkor nodes and edges. No LLM. Pass A only (API fields).

Base API: `https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3`  
Auth: per-user OAuth (`auth.atlassian.com`). Scopes on the authorize URL: `offline_access read:jira-work read:jira-user`.

Sync input: one Atlassian site + one project. Optional: one root issue key — ingest that issue plus Epic → Task → Sub-task descendants (depth 8), not the whole project.

Record key: `jira:{connection_id}:{project|work_item}:{cloud_id}:{id}`  
Unchanged content hash → `KEEP` (graph not rewritten).

---

## 1. Connect and pick (not written to the graph)

These calls only drive the UI and the OAuth store.

| API | Jira returns | Neuron stores |
|---|---|---|
| `GET auth.atlassian.com/authorize` then `POST …/oauth/token` | access + refresh token | Encrypted token in `oauth_connectors.sqlite3`. Cookie `neuron_jira_session`. |
| `GET /oauth/token/accessible-resources` | cloud id, site name, url | Site picker. Connection display name is the site list. Not a graph `Person`. |
| `GET /ex/jira/{cloud}/rest/api/3/project/search` | project id, key, name, description | Picker. Graph write happens later, for the chosen project only. |

---

## 2. Sync fetch → graph write

After the job is queued, Neuron fetches, then writes Falkor + Qdrant + the named-graph ledger.

One search family: `POST /ex/jira/{cloud}/rest/api/3/search/jql`  
`expand=changelog`. Fields: `summary`, `description`, `status`, `issuetype`, `created`, `updated`, `assignee`, `reporter`, `labels`, `components`, `parent`, `issuelinks`, `comment`.

- Whole project: `project = "KEY" ORDER BY updated ASC`
- Subtree: BFS (`"Epic Link" in (…) OR parent in (…) OR parentEpic in (…)`), then `key in (…)` in batches of 100

`components` is requested and discarded. Changelog items other than `assignee` / `status` are discarded.

### Project

| API | Jira returns | Graph |
|---|---|---|
| Same project object as the picker (refetched at sync start) | `id`, `key`, `name`, `description` | **`Project`**: `name`, `url` (`{site}/browse/{key}`), `search_text` = `Jira project KEY: Name` + description. **`SourceRecord`** via `MENTIONED_IN`. **Not embedded.** |

### Work item

| API field | Jira returns | Graph |
|---|---|---|
| `id`, `key`, `summary` | stable id, mutable key, title | **`WorkItem`**: `issue_key`, `name` (`DATAOS-4346 — summary`), `url` |
| `status`, `issuetype`, `labels` | current values | Props on the node. Labels are **not** Term nodes. Current status is a **prop**, not a live `HAS_STATUS` edge. |
| `description` (ADF) | rich doc | Flattened to plain text. Goes into `search_text` (and the vector). |
| `comment.comments` | author display name + ADF body (the page Jira put on the search hit) | Flattened as `Comment by {name}:` + body in `search_text`. **No comment node. No commenter `Person`. No comment time.** |
| `assignee`, `reporter` | `accountId`, display name, email (email often hidden) | **`Person`** uid = Jira `accountId`. Props: `name`, `email`. |
| `parent.id` / `parent.key` | parent issue | `PARENT_OF` (see edges). Stub if parent is not in this fetch. |
| `issuelinks` | typed links | Only outward name containing `block` → target **issue id** (not key). Other link types dropped. |
| `created`, `updated` | ISO datetimes | `SourceRecord.source_created_at` / `source_updated_at` / `source_time`. **Not** copied onto the WorkItem. **Not** in `search_text`. |
| `changelog.histories` | field diffs with `created` | Only `assignee` and `status`. Closed `[start, end)` rows on `FactHistory`. Live value stays on the edge / prop. |

**Node `Person`:** namespaced `jira-account`. Comment authors and changelog authors are **not** Persons.

**Edges**

| Edge | When |
|---|---|
| `WorkItem -[:BELONGS_TO]-> Project` | Always. Single-valued (supersede then upsert). |
| `WorkItem -[:ASSIGNED_TO]-> Person` | Current assignee. Live edge only. `valid_at` = ticket `updated`, not the assignment instant. |
| `WorkItem -[:REPORTED_BY]-> Person` | Reporter present. |
| `WorkItem -[:PARENT_OF]-> WorkItem` | **Child → parent** (4346 → 3839 → 3833). |
| `WorkItem -[:BLOCKS]-> WorkItem` | Outward “blocks”. Multi-valued. Target stubbed if not synced yet this run. |
| `Person -[:SAME_AS]-> Person` | Jira email matches a Person from another provider. |
| `WorkItem -[:MENTIONED_IN]-> SourceRecord` | Provenance. Same for Project and Person. |

**History** (world time, first sync — not only after we watch a live change):

| History | Meaning |
|---|---|
| `ASSIGNED_TO` closed intervals | Previous assignees, start/end from changelog. Current assignee is the live edge, not a history row. |
| `HAS_STATUS` closed intervals | Previous statuses. Current status is the WorkItem prop. |

**Cross-source (still no LLM)**

| Step | When |
|---|---|
| `resolve_backlinks_for_target` | A Bitbucket/GitHub/Notion record already stored this ticket key → `Commit`/`PullRequest -[:IMPLEMENTS]-> WorkItem`, else `REFERENCES`. |
| `resolve_exact_anchors` | This issue’s text contains a **other-provider** SHA / `owner/repo` / URL → `REFERENCES`. Same-provider Jira-to-Jira keys are not linked here. |
| `materialize_around` | Child `IMPLEMENTS` lifts to parent; shared Term/System can derive `Document -[:DOCUMENTS]-> WorkItem`. |

`search_text` example (this is also the embedding input):

```
Issue: DATAOS-4346
Summary: Argus API — close Glossary & Context Cards gaps (Notion matrix)
Type: Sub-task
Status: In Progress
Assignee: Aashish Verma
Reporter: Soumadip De
Labels: ONTOLOGY, PlatformAI-Aug-Sprint

Description:
Context
Single Argus backend delivery ticket to close all API gaps…

Comment by Akshay:
…
```

Created / updated / parent key / blocked ids / changelog are **not** in this blob.

---

## 3. Dates — fetched vs on the graph

Jira sends datetimes. Most of them never reach the WorkItem or chat evidence.

| Datetime | Stored on | Visible to search / chat? |
|---|---|---|
| Issue `created` | `SourceRecord.source_created_at` | No. Not on WorkItem, not in `search_text`. |
| Issue `updated` | `SourceRecord.source_updated_at`, `source_time`; live edge `valid_at` | Not as “ticket created/updated”. |
| Changelog `created` (assignee / status) | `FactHistory` `valid_from` / `valid_to` | Yes, for “who was assignee in March?”. |
| Comment `created` | Dropped | No. |

So “when was this ticket created?” has no evidence on the WorkItem. Assignment/status *history* questions do.

---

## 4. Vector

One Qdrant point per **WorkItem** at write time (`_embed_now`). Model: `text-embedding-3-small` (1536). Point payload is only `uid` + `label` + the vector.

Input = the `search_text` blob above (truncated at 8000 tokens; real tickets here are a few thousand characters).  
**Not embedded:** Project, Person, SourceRecord, changelog, created/updated.

Hybrid search = this vector + Falkor BM25 on the same `search_text`. Chat answers from edges / history after retrieval; the vector only finds the ticket.

`semantic_status = NOT_APPLICABLE` — no Luna extraction. Description and comments stay as text.

---

## 5. Shape after a sync

```
Project
  └─ BELONGS_TO ← WorkItem              flattened summary + description + comments
                    ├─ ASSIGNED_TO → Person          live assignee (accountId)
                    ├─ REPORTED_BY → Person
                    ├─ PARENT_OF → WorkItem          child → parent
                    ├─ BLOCKS → WorkItem             outward blocks only
                    ├─ (history) ASSIGNED_TO         closed changelog windows
                    └─ (history) HAS_STATUS          closed changelog windows

Person ─ SAME_AS → Person               verified email only
Commit / PR / File ─ IMPLEMENTS / REFERENCES → WorkItem
                    if another provider already named this key

every node ─ MENTIONED_IN → SourceRecord
             (SourceRecord holds created / updated)
```

---

## 6. Example (live `less_token`)

Sync scope: subtree of epic `DATAOS-3833` (not the whole DATAOS project). One leaf:

> `DATAOS-4346` — *Argus API — close Glossary & Context Cards gaps (Notion matrix)*  
> Type Sub-task, status In Progress.  
> Assignee Aashish Verma, reporter Soumadip De.  
> Parent `DATAOS-3839` (task), whose parent is `DATAOS-3833` (epic).

What Neuron writes from Jira:

```
Project DATAOS
  └─ BELONGS_TO ← WorkItem DATAOS-4346
                    ├─ ASSIGNED_TO → Person     Aashish Verma  (Jira accountId)
                    ├─ REPORTED_BY → Person     Soumadip De
                    └─ PARENT_OF → WorkItem     DATAOS-3839
                                      └─ PARENT_OF → WorkItem DATAOS-3833
```

`search_text` / the Qdrant vector is the flattened blob in §2 (summary + description + comments). `created` / `updated` sit only on the `SourceRecord`.

Then Bitbucket commits whose **message** contains `DATAOS-4346` attach from the other side (exact key, no LLM). On this graph that is 11 commits, newest first:

| Commit | Message (first line) |
|---|---|
| `df38ac6` | DATAOS-4346 Added ai_caveats,ai_synonyms to the member search hits |
| `be023b9` | DATAOS-4346 Added ai_instructions,… to the asset search hits |
| `6b1811d` | DATAOS-4346 Added measure_names and dimension_names to the search hits |
| … | eight older `DATAOS-4346 …` commits on `typesense` |

```
Commit be023b9 ─ IMPLEMENTS → WorkItem DATAOS-4346
Commit df38ac6 ─ IMPLEMENTS → WorkItem DATAOS-4346
…
```

`ce9b313` (*updated pipeline*) does **not** get this edge — no key in the message. Walkthrough of that commit is in `Bitbucket.md`.

If Aashish's Jira email equals the Bitbucket commit-author email, the two Person nodes get `SAME_AS`. That links **people**, not the ticket to the commit. The ticket↔commit link is only the key in the message.

The ticket title names a Notion gap matrix. That page is ingested, but it does **not** contain `DATAOS-4346`, so there is no `Document -DOCUMENTS-> WorkItem`. Walkthrough: `Notion.md`.

---

## 7. After write

| Step | What happens |
|---|---|
| Orphans | Shared entities with no remaining `MENTIONED_IN` are removed. |
| Reconcile | **Not done.** Tickets that left the JQL/subtree stay on the graph until disconnect. |
| Disconnect | `DELETE` connection deletes every `jira:{connection_id}:*` record, then orphans. Uses the **default** graph + default ledger, not a named graph. |

---

## 8. Jira can give this — we do not use it

| API / field | Would give | Why skipped |
|---|---|---|
| `priority`, sprint, story points, fix versions | Planning fields | Not in the field list. |
| `components` | Component names | Requested, then ignored. |
| Inward / other `issuelinks` | “is blocked by”, relates, duplicates | Only outward **blocks**. |
| `/issue/{id}/changelog` (paged) | Full history | Whatever `expand=changelog` put on the search hit. |
| Comment timestamps, more comment pages | When / remaining comments | First search page, body only. |
| Attachments, worklogs, watchers | Files, time spent | Not ingested. |
| Custom fields | Anything else on the screen | Not ingested. |
| Boards / user directory | Sprint membership, org chart | Not ingested. |
| Description → Term / Decision (Luna) | Semantic extraction | Notion-only. Jira free text is search + embed. |

---

## 9. Limits

| Limit | Value | Effect |
|---|---|---|
| Search page | 100 issues | JQL pagination via `nextPageToken`. |
| Subtree BFS | depth 8 | Epic → … → leaf. Root included. |
| Key batch | 100 | `key in (…)` after subtree discovery. |
| Changelog fields | `assignee`, `status` | Everything else in the expand is dropped. |

Code: `connectors/jira/api.py` (fetch), `graph/jira_pipeline.py` (write), `demo_ui/backend/jira_routes.py` (OAuth + job).
