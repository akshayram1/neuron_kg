# Bitbucket → Neuron graph

How one repo sync becomes Falkor nodes and edges. No LLM. Pass A only (API fields).

Base API: `https://api.bitbucket.org/2.0`  
Auth: per-user OAuth (`bitbucket.org/site/oauth2`). Scopes live on the Bitbucket consumer, not on the authorize URL.

Sync input: one workspace slug + one repository + one branch. Files: `.py` / `.md` only. Commits: last N on that branch head (default 100). PRs: OPEN + MERGED + DECLINED (cap 100).

Record key: `bitbucket:{connection_id}:{entity_type}:{repo_uuid}:{id}`  
Unchanged content hash → `KEEP` (graph not rewritten).

---

## 1. Connect and pick (not written to the graph)

These calls only drive the UI and the OAuth store.

| API | Bitbucket returns | Neuron stores |
|---|---|---|
| `GET bitbucket.org/site/oauth2/authorize` then `POST …/access_token` | access + refresh token | Encrypted token in `oauth_connectors.sqlite3`. Cookie `neuron_bitbucket_session`. |
| `GET /user` | display name, account id | Connection display name. Not a graph `Person`. |
| `GET /workspaces/{slug}` | workspace uuid, slug, name | Validate typed slug. **No workspace list** — Bitbucket removed enumeration (`/workspaces` 404). |
| `GET /repositories/{workspace}` | repos: uuid, slug, full name, default branch, private, description, html url | Picker list. Graph write happens later, for the chosen repo only. |
| `GET /repositories/{ws}/{slug}/refs/branches` | branch names | Picker. Default branch first. |
| `GET /repositories/{ws}/{slug}/refs/branches/{name}` | head commit hash | Internal `resolve_ref`. Branch names with `/` cannot be used on `/src` — every tree/commit read uses this hash. |

---

## 2. Sync fetch → graph write

After the job is queued, Neuron fetches, then writes Falkor + Qdrant + the named-graph ledger.

### Repository

| API | Bitbucket returns | Graph |
|---|---|---|
| Same repo object as the picker (no extra call) | `uuid`, `full_name`, `main_branch` (swapped to the chosen branch), `private`, `description`, `html_url` | **`Repository`** node: `name`, `url`, `default_branch`, `private`, `search_text` (name + branch + description). **`SourceRecord`** via `MENTIONED_IN`. |

### Source files (HEAD snapshot)

| API | Bitbucket returns | Graph |
|---|---|---|
| `GET /repositories/{ws}/{slug}/src/{head}/{path}` (walk every directory) | directory entries or file path, size, last-modifying commit | Only `.py` / `.md`. Skip if size > `BITBUCKET_MAX_FILE_BYTES` (default 1MB). |
| `GET` same `src` URL with `Accept: text/plain` | file bytes | Decode UTF-8. Skip binary. |

**Node `SourceFile`:** `name` / `path`, `language`, `blob_sha` (that file's last-modifying commit), `url` (permalink at that commit), `search_text` = full current file.

**Edges**

| Edge | Meaning |
|---|---|
| `Repository -[:CONTAINS]-> SourceFile` | File is on this repo's ingested tree. |
| `SourceFile -[:MENTIONED_IN]-> SourceRecord` | Provenance. |
| `SourceFile -[:REFERENCES]-> WorkItem` | Only if file text contains an exact ticket key (`DATAOS-4346`). |

This is **today's tree**, not a commit's patch. Branch is metadata on the record, not part of the file identity.

### Commits (message + file list)

| API | Bitbucket returns | Graph |
|---|---|---|
| `GET /repositories/{ws}/{slug}/commits/{head}` | hash, message, author raw (`Name <email>`), date, html url, parents | **`Commit`**: `sha`, `authored_at`, `url`, `name` (`abc1234 — first line`). Parents are **not** stored. |
| `GET /repositories/{ws}/{slug}/diffstat/{sha}` **per commit** | JSON rows: `status` (added / modified / removed / renamed), `old.path`, `new.path`, `lines_added`, `lines_removed` | Paths + `+/-` appended to commit `search_text` under `Files:`. **No hunk / patch.** One failed diffstat leaves `files` empty; sync continues. |

**Node `Person` (commit author):** uid from **email** if present, else name. Props: `name`, `email`.

**Edges**

| Edge | When |
|---|---|
| `Repository -[:CONTAINS]-> Commit` | Always. |
| `Commit -[:AUTHORED_BY]-> Person` | Always. |
| `Commit -[:MODIFIES]-> SourceFile` | Diffstat path still exists as an ingested HEAD `.py`/`.md`. Evidence: `modified +12/-3`. Deleted or `.go` paths stay on the commit text only. |
| `Commit -[:IMPLEMENTS]-> WorkItem` | Message (or files block) contains an exact Jira key. |
| `Person -[:SAME_AS]-> Person` | Commit email matches a Jira (or other) person. |
| `Commit -[:MENTIONED_IN]-> SourceRecord` | Provenance. |

`search_text` example:

```
[SOURCE]
Kind: Commit
Name: be023b9deadb
Repository: rubik_/argus
Author: Aashish Verma

DATAOS-4346 Added ai_instructions,ai_caveats,ai_synonyms and example_queries to the asset search hits

Files:
  modified  src/argus/search/query/plan.py  +12/-3
```

### Pull requests

| API | Bitbucket returns | Graph |
|---|---|---|
| `GET /repositories/{ws}/{slug}/pullrequests?state=OPEN,MERGED,DECLINED` | id, title, description, state, author display name + uuid, source/dest branch, created/updated, html url | **`PullRequest`**: `name` (`PR #12 — title`), `state`, `source_branch`, `destination_branch`, `url`, `search_text` = title + description. **No diff, no review comments, no PR↔commit edge.** |

**Node `Person` (PR author):** uid from **display name** (Bitbucket PRs have no email). May not merge with the commit-author `Person`.

**Edges**

| Edge | When |
|---|---|
| `Repository -[:CONTAINS]-> PullRequest` | Always. |
| `PullRequest -[:AUTHORED_BY]-> Person` | Always. |
| `PullRequest -[:IMPLEMENTS]-> WorkItem` | Title/body contains an exact Jira key. |
| `PullRequest -[:MENTIONED_IN]-> SourceRecord` | Provenance. |

---

## 3. Shape after a sync

```
Repository
  ├─ CONTAINS → SourceFile          HEAD .py / .md text
  │                └─ REFERENCES → WorkItem     if key in file
  ├─ CONTAINS → Commit              message + diffstat paths
  │                ├─ AUTHORED_BY → Person      email-keyed
  │                ├─ MODIFIES → SourceFile     only if that path is still on HEAD
  │                └─ IMPLEMENTS → WorkItem     if key in message
  └─ CONTAINS → PullRequest         title + description
                   ├─ AUTHORED_BY → Person      name-keyed
                   └─ IMPLEMENTS → WorkItem     if key in title/body

every node ─ MENTIONED_IN → SourceRecord
Person ─ SAME_AS → Person           verified email only
```

File / commit / PR `search_text` is also embedded in Qdrant at write time (`_embed_now`). `semantic_status = NOT_APPLICABLE` — no Luna extraction.

---

## 4. Example (live `less_token`)

Repo `rubik_/argus`, branch `typesense`. One commit on that head:

> `be023b9` — *DATAOS-4346 Added ai_instructions,ai_caveats,ai_synonyms and example_queries to the asset search hits*  
> 2026-09-09, author Aashish Verma (`Name <email>`).  
> Diffstat: `src/argus/search/query/plan.py` modified `+4/-1`.

What Neuron writes:

```
Repository rubik_/argus
  ├─ CONTAINS → SourceFile  src/argus/search/query/plan.py     HEAD text (it's .py)
  ├─ CONTAINS → Commit      be023b9
  │     ├─ AUTHORED_BY → Person     email-keyed "Aashish Verma"
  │     ├─ MODIFIES → SourceFile    plan.py   evidence: modified +4/-1
  │     └─ IMPLEMENTS → WorkItem    DATAOS-4346
  │           (regex found DATAOS-4346 in the message — no LLM)
  └─ …other HEAD files / last-100 commits
```

`ce9b313` (*updated pipeline*) is the contrast: same repo, three files in `Files:` (`Dockerfile`, `Dockerfile.pipeline`, `pyproject.toml`), **no** `MODIFIES` (not `.py`/`.md` on HEAD), **no** `IMPLEMENTS` (no ticket key in the message).

Same key in later messages (`df38ac6`, `6b1811d`, …) each get their own `IMPLEMENTS` to the same WorkItem. The Jira side of `DATAOS-4346` is in `Jira.md`.

---

## 5. After write

| Step | What happens |
|---|---|
| Reconcile | Ledger keys for this repo's files / commits / PRs that were **not** in this fetch are deleted from the graph. |
| Orphans | Shared entities with no remaining `MENTIONED_IN` are removed. |
| Disconnect | `DELETE` connection deletes every `bitbucket:{connection_id}:*` record, then orphans. |

---

## 6. Bitbucket can give this — we do not call it

| API | Would give | Why skipped |
|---|---|---|
| `GET …/diff/{sha}` | Unified patch / hunks | Extra payload; `MODIFIES` + path + `+/-` is enough to know *which file*, not *which lines*. |
| `GET …/patch/{sha}` | Diff + commit header | Same as above. |
| `GET …/src/{sha}/{path}` at an **old** commit | File as it was then | We only walk **HEAD**. |
| `GET …/pullrequests/{id}/diffstat` or `/diff` | PR file list / patch | PR node is metadata only. |
| `GET …/commit/{sha}/comments` or PR comments | Review discussion | Not ingested. |
| Workspace / “all my repos” listing | Dropdown of workspaces | Bitbucket removed those endpoints (404 / 410). |

---

## 7. Limits (env)

| Variable | Default | Effect |
|---|---|---|
| `BITBUCKET_MAX_FILE_BYTES` | `1000000` | Larger `.py`/`.md` skipped. |
| `BITBUCKET_MAX_COMMITS_PER_SYNC` | `100` (cap 1000) | Commit list + **one diffstat call each**. |

Code: `connectors/bitbucket/api.py` (fetch), `graph/bitbucket_pipeline.py` (write), `demo_ui/backend/bitbucket_routes.py` (OAuth + job).
