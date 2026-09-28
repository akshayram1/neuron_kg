# Cost & scale analysis

v2 snapshot taken 2026-09-11 against **`less_token`** (Falkor `neuron__less_token`,
Qdrant `neuron_entities__less_token`, ledger
`connector_ledger__less_token.sqlite3`). Same measurement method as `cost.md`
(Cypher, sqlite, tiktoken on stored text). Notion LLM $ comes from the
recorded `oauth_sync_runs` usage for run `06e6f42915f54c3b9797b8b0ed197c08`.

Read this next to `cost.md`. Same section numbers, same tables — the delta
is the story: Jira / GitHub / Bitbucket no longer call `run_semantic_pass`
(`NOT_APPLICABLE` + `_embed_now` only). **Notion is unchanged** (still Luna).

---

## 1. Data ingested (`connector_ledger__less_token.sqlite3`)

| Provider | Records | Breakdown |
|---|---|---|
| Bitbucket | 365 | 264 `source_file`, 100 `commit`, 1 `repository` |
| Jira | 29 | 28 `work_item`, 1 `project` |
| Notion | 6 | 5 `page`, 1 `workspace` |
| GitHub | 0 | not ingested |
| **Total** | **400** | same headcount as `cost.md` |

- `source_chunks`: **9** chunks, all `status = done` (Notion only). `cost.md`: **380**.
- Total chunk text: **50,964 characters**. `cost.md`: **865,953**.
- `semantic_status`: **5 `done`** (Notion pages) + **395 `not_applicable`**.
  `cost.md`: 351 `done` + 49 `not_applicable`.

## 2. Graph state (FalkorDB, `neuron__less_token`)

**Nodes: 1,092 total** (`cost.md`: 1,813)

| Label | Count | `cost.md` |
|---|---|---|
| SourceRecord | 400 | 400 |
| SourceFile | 264 | 264 |
| FactHistory | 245 | (not listed) |
| Commit | 100 | 100 |
| WorkItem | 29 | 29 |
| Term | 15 | **183** |
| Decision | 13 | **693** |
| Person | 11 | 8 |
| System | 7 | **128** |
| Document | 5 | 5 |
| Project | 1 | 1 |
| Repository | 1 | 1 |
| Workspace | 1 | 1 |

Every Decision / Term / System here traces to **Notion**. Jira and Bitbucket
contributed **zero** semantic entities.

**Edges: 1,953 total** (`cost.md`: 4,542)

| Relation | Count | `cost.md` |
|---|---|---|
| MODIFIES | 727 | (not present) |
| MENTIONED_IN | 575 | 1,785 |
| CONTAINS | 369 | 369 |
| AUTHORED_BY | 100 | 100 |
| EXTRACTED_FROM | 35 | **1,252** |
| PARENT_OF | 30 | 3 |
| REPORTED_BY | 28 | 28 |
| BELONGS_TO | 28 | 28 |
| IMPLEMENTS | 22 | 11 |
| DEFINES | 14 | 150 |
| ASSIGNED_TO | 11 | 11 |
| APPLIES_TO | 10 | **796** |
| SAME_AS | 2 | 2 |
| BLOCKS | 2 | 2 |

`MODIFIES` is new (commit diffstat → HEAD `.py`/`.md`). `FactHistory` /
`PARENT_OF` grew from changelog + Jira subtree. Semantic edges
(`EXTRACTED_FROM`, `APPLIES_TO`, `DEFINES`) collapsed to Notion-scale.

## 3. Vector store (Qdrant, `text-embedding-3-small`, 1536-dim)

`cost.md` embedded 1,009 points and **did not** embed `SourceFile`. v2 embeds
files at write time (`_embed_now`), so the vector set is mostly Bitbucket
file text.

Real tiktoken `cl100k_base` on live `n.search_text`:

| Label | Nodes | Tokens | `cost.md` tokens |
|---|---|---|---|
| WorkItem | 29 | 8,350 | 8,350 (28 nodes) |
| Document | 5 | 11,646 | 11,646 |
| Decision | 13 | 558 | **31,254** (693 nodes) |
| Term | 15 | 347 | **4,916** (183 nodes) |
| PullRequest | 0 | 0 | 0 |
| Commit | 100 | 24,842 | 5,875 (pre–`Files:` block) |
| SourceFile | 264 | 435,458 | **not embedded** |
| **Total** | **426** | **481,201** | **62,041** (1,009 pts, no files) |

Commit tokens rose because each commit `search_text` now includes the
diffstat `Files:` list. File embeddings dominate the token count; they are
cheap at $0.02/1M.

## 4. LLM extraction volume (`gpt-5.6-sol`, semantic pass)

**Notion only.** No `semantic pass done:` volume for Jira/Bitbucket on this
graph.

Recorded usage, 5 Notion pages:

- **Input**: 38,074 tokens
- **Output**: 20,080 tokens (billed JSON, not a floor)
- Entities in graph from that pass: 13 Decision + 15 Term + 7 System

`cost.md` (all providers, including reprocessing): 437 LLM calls, 206,169
input tokens (chunk text), ~40k–60k output.

A new `graph/structured_query.py` (used by chat) is Cypher-only — no
ingestion LLM. It does not change this section.

## 5. Pricing (same as `cost.md`, `developers.openai.com` 2026-09-10)

| Model | Input / 1M | Cached input / 1M | Output / 1M |
|---|---|---|---|
| `gpt-5.6-sol` (extraction) | $4 | $0.4 | $20 |
| `gpt-5.6-luna` (chat) | $0.2 | $0.02 | $1.2 |
| `text-embedding-3-small` | $0.02 | — | — |

## 6. Cost breakdown

**Ingestion, this graph (400 records / 426 vectors / 5 Notion pages):**

| Item | Tokens | Rate | Cost | `cost.md` |
|---|---|---|---|---|
| Extraction input | 38,074 (Notion) | $4/1M | $0.152 | $0.825 (206k) |
| Extraction output | 20,080 (billed) | $20/1M | $0.402 | $0.80–$1.20 |
| Embedding | 481,201 | $0.02/1M | $0.010 | $0.001 (62k) |
| **Total** | | | **≈ $0.56** | **$1.62 – $2.02** (steady) / **$1.87 – $2.33** (437 calls) |

**≈ ₹48** at ~₹85/USD, vs `cost.md` **≈ ₹160 – ₹195**.

Jira + Bitbucket extraction is **$0**. Their remaining cost is embedding
only: (8,350 + 24,842 + 435,458) × $0.02/1M ≈ **$0.0094**.

If the old pass had run on that same Jira+Bitbucket embed volume (468,650
tokens) at Notion's observed output/input ratio (20,080/38,074 ≈ 0.527):

| Item | Cost |
|---|---|
| Input 468,650 × $4/1M | $1.87 |
| Output 468,650 × 0.527 × $20/1M | ≈ $4.94 |
| **Hypothetical old Jira+Bitbucket extract** | **≈ $6.81** |

vs **$0.0094** embed-only → on the order of **700×** cheaper for those two
providers. That is less work, not the same work cheaper (see trade-off).

**Chat (`gpt-5.6-luna`)**: still not persisted server-side. Same illustrative
per-query figure as `cost.md` (~$0.0011).

## 7. Code written (delta since the initial commit)

Not re-counted for this snapshot. See `cost.md` §7 for the 2026-09-10
line-count (36 tracked / +2,860, 37 untracked / 5,499).

## 8. Time

Not re-measured from `logs/neuron.log`. Bitbucket write of 100 commits +
`MODIFIES` on this graph was the slow wall-clock (tens of minutes), not
Luna. Notion's 5-page extract is the only remaining LLM busy-time.

## Caveats

- Notion output tokens here are **billed** (from the sync run), not the
  content-floor ×1.5 method in `cost.md` §4.
- Embedding totals are not 1:1 with `cost.md` §3: v2 adds 264 `SourceFile`
  vectors and richer commit `Files:` text.
- Chat cost still illustrative.
- Structured chat lookups do not add ingestion spend.

## The trade-off

Cheaper because Jira/GitHub/Bitbucket no longer produce Decision / Term /
System. “Why did we decide X” that used to come from a Jira comment or a
docstring will not have an extracted node — only raw `search_text` +
structural edges (assignee, `IMPLEMENTS`, `MODIFIES`).

If that starts to matter, re-enable `run_semantic_pass` on high-signal
text only (PR/commit messages), not every file.

## Local embeddings — evaluated, not adopted

Switching off `text-embedding-3-small` would save ~$0.01 at this volume
(~$9 at 1,000×). Would require recreate + RRF re-sweep. Worth it for
privacy, not cost.
