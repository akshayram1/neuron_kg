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

