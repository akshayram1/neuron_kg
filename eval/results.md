# eval/results.md — Phase 0 baselines

One section per harness run, oldest first. Never averaged/combined across
datasets or across control/target subsets into one headline number — each
run and each tagged subset gets its own rows.

## Known data-availability issue (read before the numbers below)

The live FalkorDB instance this environment points at (`neuron-falkordb-1`,
`localhost:6380`, per `compose.story.yml` and `docker ps` — confirmed to be
*this* project's container, not a different one) does **not** currently
hold this codebase's knowledge-graph schema (`WorkItem` / `Document` /
`Decision` / `Term` / `SourceRecord` / … per `graph/schema.py`) under any of
the graph names the golden sets target:

| graph name | Falkor graph | nodes | what's actually there |
|---|---|---|---|
| `nilus` | `neuron__nilus` | 232 | wrong schema — labels `Source`/`Entity`/`Context` with properties (`chunk_confidence`, `durable_confidence`, `importance`, `preview`, `raw_json`, …) that don't match `graph/schema.py` at all. Looks like a different project's data sharing this FalkorDB instance/volume, not a stale Neuron snapshot. |
| `less_token` | `neuron__less_token` | 0 | empty. `connector_ledger__less_token.sqlite3` and Qdrant collection `neuron_entities__less_token` (425 points) exist, so the source data was ingested at some point, but the graph write never landed here (or was cleared). |
| `default` (used by `argus_golden.jsonl`, no `--graph` flag) | `neuron` | 0 | empty. Qdrant `neuron_entities` has only 32 points (cost.md's original ingestion had 1,009) — a small, unrelated fragment, not the full corpus. |
| `atlas` (unrelated to any golden set, checked only to rule out a naming mix-up) | `neuron__atlas` | 263 | same foreign `Source`/`Entity`/`Context` schema as `nilus`. |

This is an infrastructure/data-loading gap, not a retrieval-code issue, and
is out of this task's scope to fix (would mean re-running real Jira/
Bitbucket/Notion ingestion against provider APIs — a large, costly,
out-of-scope operation, and the user's own standing instruction is to never
use a full graph/data clear to fix a provider's messy state, i.e. not to
improvise a reload here either). The runs below are real, unmodified
executions of the current code against this environment's actual current
data — nothing is fabricated — but where the underlying graph is empty or
schema-mismatched, "0" measures "no matching data reachable", not "the
retrieval algorithm scored zero". See this task's QUERIES entry for the
open question this raises.

---

## nilus_golden.jsonl — 2026-09-25 06:29 UTC

- git sha: `b8c76e5`
- dataset: `eval/nilus_golden.jsonl`
- graph: `nilus` → falkor=`neuron__nilus` (232 nodes, 2344 edges), qdrant=`neuron_entities__nilus`
- flags: `--k 8 --graph nilus --with-chat --stage-metrics`
- model: `gpt-5.6-sol` (chat answer synthesis)

**all** (41 cases, 41 scored)

| metric | value |
|---|---|
| recall@k | 0.0000 |
| precision@k | 0.0000 |
| MRR | 0.0000 |
| candidate recall (presence) | 0.0000 |
| final recall (presence) | 0.0000 |
| rerank gap (candidate − final) | 0 |
| chain coverage (mean) | 0.0000 (no chain golden set wired in yet — always 0) |
| gold evidence in pack (recall) | 0.0000 |
| median input tokens | 10 |
| p90 input tokens | 14 |
| median output tokens | 0 |
| p90 output tokens | 0 |
| median $/question | 0.000040 |
| total $ (this run) | 0.0162 |
| median latency (ms) | 387.8 |
| p90 latency (ms) | 523.6 |

**control** (31 cases, 31 scored)

| metric | value |
|---|---|
| recall@k | 0.0000 |
| precision@k | 0.0000 |
| MRR | 0.0000 |
| candidate recall (presence) | 0.0000 |
| final recall (presence) | 0.0000 |
| rerank gap (candidate − final) | 0 |
| chain coverage (mean) | 0.0000 (no chain golden set wired in yet — always 0) |
| gold evidence in pack (recall) | 0.0000 |
| median input tokens | 10 |
| p90 input tokens | 15 |
| median output tokens | 0 |
| p90 output tokens | 0 |
| median $/question | 0.000040 |
| total $ (this run) | 0.0158 |
| median latency (ms) | 390.9 |
| p90 latency (ms) | 710.2 |

**target** (10 cases, 10 scored)

| metric | value |
|---|---|
| recall@k | 0.0000 |
| precision@k | 0.0000 |
| MRR | 0.0000 |
| candidate recall (presence) | 0.0000 |
| final recall (presence) | 0.0000 |
| rerank gap (candidate − final) | 0 |
| chain coverage (mean) | 0.0000 (no chain golden set wired in yet — always 0) |
| gold evidence in pack (recall) | 0.0000 |
| median input tokens | 10 |
| p90 input tokens | 11 |
| median output tokens | 0 |
| p90 output tokens | 0 |
| median $/question | 0.000040 |
| total $ (this run) | 0.0004 |
| median latency (ms) | 368.0 |
| p90 latency (ms) | 421.7 |

---

## less_token_golden.jsonl — 2026-09-25 06:30 UTC

- git sha: `b8c76e5`
- dataset: `eval/less_token_golden.jsonl`
- graph: `less_token` → falkor=`neuron__less_token` (0 nodes, 0 edges), qdrant=`neuron_entities__less_token`
- flags: `--k 8 --graph less_token --with-chat --stage-metrics`
- model: `gpt-5.6-sol` (chat answer synthesis)

**all** (7 cases, 7 scored)

| metric | value |
|---|---|
| recall@k | 0.0000 |
| precision@k | 0.0000 |
| MRR | 0.0000 |
| candidate recall (presence) | 0.0000 |
| final recall (presence) | 0.0000 |
| rerank gap (candidate − final) | 0 |
| chain coverage (mean) | 0.0000 (no chain golden set wired in yet — always 0) |
| gold evidence in pack (recall) | 0.0000 |
| median input tokens | 11 |
| p90 input tokens | 15 |
| median output tokens | 0 |
| p90 output tokens | 0 |
| median $/question | 0.000044 |
| total $ (this run) | 0.0003 |
| median latency (ms) | 335.0 |
| p90 latency (ms) | 395.8 |

---

## argus_golden.jsonl — SKIPPED (2026-09-25)

`argus_golden.jsonl` is run with no `--graph` flag (see
`scripts/sweep_retrieval_params.py`'s own usage example), so it targets the
`default` graph slug: Falkor graph `neuron` (base name, no suffix), Qdrant
collection `neuron_entities`.

Checked before running: `neuron` has **0 nodes** in the live FalkorDB
instance, and `neuron_entities` has only **32** Qdrant points (cost.md's
original full ingestion had 1,009). Per this task's instructions ("only if
its graph/collection is actually reachable ... note it as skipped instead"),
this is not reachable — a real invocation would only measure an empty graph,
not retrieval quality, and would look like a fabricated "0 for everything"
baseline. No `evaluate_retrieval.py` run was made for this dataset.

---


---

## Before/after Laya accuracy check — 25 Sep 2026

**Requested by Akshay while Laya integration (`NEURON_RERANK=laya`) was in progress, uncommitted, in `graph/chat.py`/`graph/rerank.py`.** Real runs, not simulated — 44 chain questions (`eval/chain_golden.jsonl`, schema-mapped to `query`/`expected_uids` for this harness in a throwaway file, not committed), `--graph story-20260917-050343-5d26 --k 8 --with-chat --stage-metrics`, the only graph in this environment whose schema matches the real ontology.

- **Before** (clean `git worktree` at commit `653d723`, no Laya code path exists): `candidate_recall=0.4773`, `final_recall=0.0909`, `MRR=0.01388`, `gold_evidence_recall=0.0909`, total $0.612.
- **After** (working tree, `NEURON_RERANK=laya`, `LAYA_MODEL_DIR=.../personal_exp/laya/model/laya-ingest`): **identical** numbers, total $0.623.

**Numbers are identical because Laya never actually scored anything.** Reproduced directly with full logging: 31/44 cases reach the Laya code path (the other 13 return early via the structured-lookup resolver, expected); all 31 raise `TypeError: best_window() missing 1 required positional argument: 'encoder'` at `graph/chat.py:1043` (`_rerank_candidates` calls `best_window(question, serialized, 300)` — `graph/text_window.py::best_window`'s signature is `(question, text, tokens, encoder)`, no default for `encoder`). The exception is caught by the existing fallback handler (`chat.py:1358`, "laya fallback scoring failed; using RRF order", `trace.fallback=True` on all 31), so retrieval silently reverts to plain RRF every time — the fallback mechanism itself works correctly, it's just masking a real bug upstream of it.

**Secondary finding (uncommitted to the comparison, but real):** Qdrant collection `neuron_entities__story-20260917-050343-5d26` returns 404 — doesn't exist. Vector search leg is dead for this graph; only fulltext contributes. Affects both runs equally, so it doesn't bias the comparison, but caps both numbers below what a working vector leg would give.

**Tertiary, unrelated to the bug:** the `laya` package itself isn't installed in this venv (only in `personal_exp/laya`'s own venv) — even with the `encoder` bug fixed, real Laya inference still can't run here without a packaging decision (same open question in `QUERIES.md`).

**Not fixed by me** — this is Akshay's in-progress, uncommitted code; flagging rather than editing it.
