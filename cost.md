# Cost & scale analysis

Snapshot taken 2026-09-10 against the live FalkorDB graph, Qdrant collection,
and `connector_ledger.sqlite3` / `logs/neuron.log` on this machine. Every
number below was measured directly (Cypher queries, sqlite queries, tiktoken
counts on the real stored text) — none of it is guessed. Where a number is
an estimate (LLM output tokens, chat cost), the method is stated so it can
be re-derived.

## 1. Data ingested (`connector_ledger.sqlite3`)

| Provider | Records | Breakdown |
|---|---|---|
| Bitbucket | 365 | 264 `source_file`, 100 `commit`, 1 `repository` |
| Jira | 29 | 28 `work_item`, 1 `project` |
| Notion | 6 | 5 `page`, 1 `workspace` |
| GitHub | 0 | not yet ingested in this environment |
| **Total** | **400** | |

- `source_chunks`: **380** chunks, all `status = done`
- Total chunk text: **865,953 characters**
- `semantic_status` on `source_records`: 351 `done` (went through the LLM),
  49 `not_applicable` (empty-description records, embedded directly —
  see `graph/jira_pipeline.py::_embed_now`)

## 2. Graph state (FalkorDB, `neuron` graph)

**Nodes: 1,813 total**

| Label | Count |
|---|---|
| Decision | 693 |
| SourceRecord | 400 |
| SourceFile | 264 |
| Term | 183 |
| System | 128 |
| Commit | 100 |
| WorkItem | 29 |
| Person | 8 |
| Document | 5 |
| Project | 1 |
| Repository | 1 |
| Workspace | 1 |

**Edges: 4,542 total**

| Relation | Count |
|---|---|
| MENTIONED_IN | 1,785 |
| EXTRACTED_FROM | 1,252 |
| APPLIES_TO | 796 |
| CONTAINS | 369 |
| DEFINES | 150 |
| AUTHORED_BY | 100 |
| REPORTED_BY | 28 |
| BELONGS_TO | 28 |
| ASSIGNED_TO | 11 |
| IMPLEMENTS | 11 |
| PARENT_OF | 3 |
| CAVEAT_OF | 2 |
| SAME_AS | 2 |
| BLOCKS | 2 |
| SUPERSEDES | 2 |
| DECIDED_BY | 1 |

## 3. Vector store (Qdrant, `text-embedding-3-small`, 1536-dim)

- **1,009 embedded points** across `WorkItem`, `Document`, `Decision`, `Term`,
  `PullRequest`, `Commit` (`graph/schema.py::VECTOR_LABELS`)
- Real per-label token count of the embedded `search_text` (tiktoken
  `cl100k_base` over the live `n.search_text` field on every node):

| Label | Nodes | Tokens |
|---|---|---|
| WorkItem | 28 | 8,350 |
| Document | 5 | 11,646 |
| Decision | 693 | 31,254 |
| Term | 183 | 4,916 |
| PullRequest | 0 | 0 |
| Commit | 100 | 5,875 |
| **Total** | **1,009** | **62,041** |

## 4. LLM extraction volume (`gpt-5.6-sol`, semantic pass)

Summed from every `semantic pass done: ... llm calls ...` line in
`logs/neuron.log` (the whole day, including reprocessing triggered by
mid-session changes — the code-comment-only chunking rewrite and the
parallel-extraction rollout both caused some chunks to be re-processed;
that reprocessing is real incurred cost, not double-counting):

- **437 LLM calls** total
- 1,433 entities extracted, 1,061 facts written, 138 rejected
  (failed the ontology allow-list or an unresolved endpoint), 405 records
  completed

Final steady-state (the 380 chunks that exist right now):

- **Input**: 206,169 tokens (tiktoken `cl100k_base` over every stored
  `source_chunks.text` row — avg 542.6 tokens/chunk)
- **Output** (real-content floor, not including JSON schema/field overhead):
  39,912 tokens — tiktoken over every `Decision`/`Term`/`System` name
  currently in the graph (5,332 + 516 + 433 tokens) plus every fact's
  `evidence` quote text on edges (33,631 tokens over 964 edges)
  - Actual billed output is higher than this floor because the model emits
    structured JSON (subject/relation/object typing, field names, quoting),
    not bare text — estimated **1.3–1.5×** the floor, i.e. ~52,000–60,000
    tokens

## 5. Pricing (fetched from `developers.openai.com`, 2026-09-10)

| Model | Input / 1M | Cached input / 1M | Output / 1M |
|---|---|---|---|
| `gpt-5.6-sol` (extraction) | $4 | $0.4 | $20 |
| `gpt-5.6-luna` (chat) | $0.2 | $0.02 | $1.2 |
| `text-embedding-3-small` | $0.02 | — | — |

Both chat/extraction models: 1,050,000 token context window, 922,000 max
input, 128,000 max output. Requests over 272K input tokens bill at 2×
input / 1.5× output for the *entire* request — irrelevant at this data
volume (largest single chunk is nowhere near that).

## 6. Cost breakdown

**Ingestion (extraction + embedding), steady-state (380 chunks / 1,009 vectors):**

| Item | Tokens | Rate | Cost |
|---|---|---|---|
| Extraction input | 206,169 | $4/1M | $0.825 |
| Extraction output (floor) | 39,912 | $20/1M | $0.798 |
| Extraction output (adjusted, ×1.5) | 59,868 | $20/1M | $1.197 |
| Embedding | 62,041 | $0.02/1M | $0.001 |
| **Total (floor → adjusted)** | | | **$1.62 – $2.02** |

**Scaled to the real 437 LLM calls made today** (437/380 = 1.15×, to account
for reprocessing during iteration):

**≈ $1.87 – $2.33** (~₹160 – ₹195 at ~₹85/USD)

**Chat (`gpt-5.6-luna`)**: not measured — `graph/chat.py` returns token
usage per turn to the API response (`tokenUsage` in `/api/chat`), and the
frontend accumulates it in browser `localStorage` (`App.tsx`), but nothing
is persisted server-side or logged, so a historical total can't be
reconstructed from this machine. Illustrative per-query cost at typical
retrieval-context size (~3,000 input tokens of evidence blocks + ~400
output tokens for an answer): `3,000×$0.2/1M + 400×$1.2/1M ≈ $0.0011` per
question — a few hundred chat turns would still cost under a dollar.

## 7. Code written (delta since the initial commit)

- Tracked files: **36 changed, +2,860 / −456 lines**
- New (untracked) files: **37 files, 5,499 lines**
- Total repository size now: **~15,571 lines** of Python + TS/TSX

## 8. Time

`logs/neuron.log` spans **00:24:25 → 21:32:58** (~21 hours of wall-clock
elapsed across the working session). Most of that is human back-and-forth
(debugging, live OAuth setup, waiting on Bitbucket's real API) rather than
active LLM/API processing — the 437 extraction calls and their surrounding
Bitbucket fetches are the actual "busy" time, and the parallel-extraction
change (`ThreadPoolExecutor`, `LLM_CONCURRENCY=6`) was added specifically
because that busy time was the bottleneck on the Bitbucket sync.

## Caveats

- LLM output token cost is an estimate (floor measured from real generated
  content, adjusted for JSON structure overhead) — an exact number would
  require the OpenAI usage dashboard or persisting `response.usage` from
  every call, which this codebase does not currently do server-side.
- The 437-call total includes reprocessing from mid-session pipeline
  changes; it is real spend, not an artifact of double-counting the same
  work.
- Chat cost is illustrative only — no historical total exists to measure.
