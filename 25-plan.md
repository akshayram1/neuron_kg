# Neuron — Retrieval, Ingestion and Temporal Upgrade Plan

**Status:** proposed, not started.
**Date:** 24 Sep 2026
**Scope:** `/Users/akshaychame/Animesh_sir_exp/neuron` only. Laya (`/Users/akshaychame/personal_exp/laya`) is used as a component *inside* Neuron, not as a separate system.
**Relationship to the existing `plan.md`:** the original `plan.md` is the locked design for Neuron v1 (FalkorDB, deterministic-first, no Graphiti). This document is the next plan on top of it. It does not reverse any v1 decision. Save it next to the original as `upgrade-plan.md` so both stay readable.

---

## How to read this document

| Section | What it answers |
|---|---|
| §1 Executive summary | What changes, in one page |
| §2 Where Neuron is today | Measured facts about the current system |
| §3 Problems | Each problem with symptom → root cause (file + function) → current behaviour → impact |
| §4 Sources reviewed | Every system we studied, what we take from it, what we reject, and why |
| §5 Comparison matrix | Capability by capability: Neuron today vs Graphiti vs DICE vs others vs Neuron target |
| §6 Design principles | The rules every change must follow |
| §7 Target architecture | Ingest flow and retrieval flow after the plan |
| §8 Phases 0–7 | The work: goal, current vs target, exact code changes, tests, exit criteria, risks, effort |
| §9 Data model changes | Every new field, relation and flag in one place |
| §10 Laya plan | Where Laya sits, what it needs to be trained on, how it is served |
| §11 Metrics | Definitions of every number the plan is judged by |
| §12 Config flags | Every new environment variable |
| §13 Risks | What can go wrong and the mitigation |
| §14 Non-goals | What this plan deliberately does not do |
| §15 Open questions | Decisions still to make |
| §16 Appendix | File map and code sketches |

Effort sizes used below are rough: **S** ≈ up to 1 day, **M** ≈ 2–4 days, **L** ≈ 1–2 weeks.

---

## 1. Executive summary

Neuron already does the hard, unglamorous parts correctly: deterministic identities, a content-hash ledger, exact-anchor cross-source edges, bi-temporal fact edges, `FactHistory`, a discard log, ontology-as-data and an LLM-free derivation engine. Ingestion cost was cut from ≈ $6.81 to ≈ $0.56 for the current corpus.

What is still weak is **what reaches the answer** and **what happens to knowledge after it is written**:

1. **Retrieval cuts too early.** Chat keeps the top 6 hits by rank from a pool of 200+ candidates. The right node is often in the pool and is cut at rank 7.
2. **Isolated nodes stay isolated.** Nodes with no edge (e.g. a Notion page that never states the ticket key) are never pulled in through the graph.
3. **Chat context is unbounded.** Whole file text goes into the prompt; one logged question used 54,835 input tokens on `gpt-5.6-sol` (≈ $0.22 input for one question).
4. **We cannot measure progress consistently.** `less_token` has 7 scored questions and `nilus_golden` has 41 scored regression/target questions, but they answer different questions and do not cover the full chat workload. Architecture switches have therefore still been judged on incompatible evidence.
5. **Text facts never expire.** API facts are superseded correctly, but LLM-extracted Decision/Term/System facts have no contradiction handling, and "when the document was written" is stored as "when the fact became true".
6. **Entity resolution is one threshold.** A single 0.9 cosine cut, no gray zone, a per-chunk cache, and a real risk of merging a decision with its own negation.

The plan fixes these in eight phases. The first four deliver most of the value:

| Phase | What | Why first |
|---|---|---|
| 0 | Refresh both current baselines and build a mixed 200–300 question eval set | Nothing else can be judged without comparable evidence |
| 1 | Token-bounded context, wide pool, 1-hop typed expansion, authority tiers | Largest retrieval and cost win, no new model needed |
| 2 | Add Laya relevance roles and bounded retry over the wide RRF pool | Rejects answer noise without throwing away useful graph or temporal bridges |
| 3 | Minimal SQLite review queue, then Laya triage in shadow and selective-ingestion changes only where measured gaps justify them | Gives later model decisions a safe landing place and avoids duplicating current ingestion work |
| 4 | Scoped entity identities + escalating resolution + polarity veto + review-only Decision merges | Stops namespace collisions and wrong merges |
| 5 | Writer contract first, then temporal facts for text: stated vs record time, classification-before-write, correction semantics and freshness | Answers both "what was true when" and "what did we believe when" |
| 6 | Hygiene job: isolated-node diagnostics, candidate links and pairwise duplicate review | Keeps the graph healthy as it grows |
| 7 | Path support, multi-hop second round, verified write-back | Only after 0–6 are measured |

What we take from other systems, in one line each:

- **Graphiti** — the date rule for invalidating a contradicted fact, including out-of-order backfill.
- **DICE** — escalating resolver with the exactly-one rule, polarity veto, four clocks, three kinds of conflict, admission gates, dry-run + audit for merges, query-time authority, two-hop candidate links.
- **CoEvoKG** — generate verifiable multi-hop questions from graph chains; path-support scoring; verified write-back.
- **GraphImmune** — measure isolated nodes and contradictions; non-destructive repairs behind approval.
- **NeuralMemory (brain)** — bounded retry when first-pass evidence is insufficient; the rest is rejected.
- **Laya** — the component that makes calibrated, cheap decisions at four points in Neuron.
- **Utopia** — already adopted in the Sep 15 upgrade; nothing new taken.

---

## 2. Where Neuron is today

All numbers below come from `cost.md` (10 Sep), `cost2.md` (11 Sep), `neuron-upgrade-report.md` (15 Sep) and the current code. Nothing here is estimated unless marked.

### 2.1 Data and graph (`less_token` graph)

| Item | Value |
|---|---|
| Source records | 400 (Bitbucket 365, Jira 29, Notion 6) |
| Graph nodes | 1,092 |
| Graph edges | 1,953 |
| Vectors in Qdrant | 426 points, 481,201 embedded tokens |
| Semantic entities (Decision / Term / System) | 13 / 15 / 7 — all from Notion |
| LLM extraction | Notion only; Jira, Bitbucket, GitHub are `NOT_APPLICABLE` |

### 2.2 Cost

| Item | Value |
|---|---|
| Full-corpus ingestion | ≈ $0.56 (was ≈ $6.81 with LLM extraction on every source) |
| Jira + Bitbucket ingestion | ≈ $0.0094 (embedding only) |
| Chat, illustrative (from `cost.md`) | ≈ $0.0011 per question, assuming luna and 3k input tokens |
| Chat, one logged real case | 54,835 input tokens on `gpt-5.6-sol` ≈ $0.22 input alone |

The two chat numbers disagree by ~200×. The illustrative figure assumed a model and context size the code does not use by default (`run_chat_turn` defaults to `CHAT_MODEL=gpt-5.6-sol`, and context is not bounded).

### 2.3 Retrieval quality (existing eval sets)

| Metric | Before 15 Sep | After 15 Sep |
|---|---|---|
| MRR | 0.3571 | 0.6000 |
| recall@8 | 0.4286 | 0.8571 |
| Passing questions | 3 / 7 | 6 / 7 |

Good progress, but the 7 scored `less_token` questions cannot distinguish a real improvement from noise: one question moves recall by 14 points. The repository also contains `eval/nilus_golden.jsonl` with 41 scored cases (31 controls and 10 ontology targets). That set is valuable as a regression gate, but it was designed for ontology adoption rather than general retrieval quality. Phase 0 keeps both sets and adds a mixed workload instead of replacing them with graph-generated questions alone.

### 2.4 What the current code does (the parts this plan touches)

| Area | File → function | Current behaviour |
|---|---|---|
| Hybrid search | `graph/search.py` → `hybrid_search` | BM25 per label + two global vector channels (name, content), RRF with `RRF_K=10`, `VECTOR_LEG_WEIGHT=3.0`, `per_method_limit=30`, returns `ranked[:limit]` |
| Chat retrieval | `graph/chat.py` → `retrieve` | Structured path first; else `hybrid_search(limit=search_limit)`; Wisdom/Finding lanes only when intent keywords match; named-person and time-window lanes prepended |
| Chat limit | `graph/chat.py` → `run_chat_turn` | `search_limit=6` |
| Evidence | `graph/chat.py` → `run_chat_turn` | Each block = `hit.summary` (full `search_text`) + all facts from `fetch_entity_detail` |
| Semantic extraction | `graph/semantic_pass.py` → `run_semantic_pass`, `_call_llm` | `LLM_MODEL` default `gpt-5.6-luna`, `LLM_BUDGET_PER_RUN=200`, `LLM_CONCURRENCY=6`; writes stay on the main thread; Jira and Bitbucket already use `selective_chunk_writes`, so some free text can be pending while low-signal chunks become `NOT_APPLICABLE` |
| Semantic dedup | `semantic_pass.py` → `_write_extraction` + `search.py` → `find_similar_uid` | Merge into an existing node if cosine similarity ≥ 0.9, else `semantic_uid` by normalized name |
| Semantic fact time | `semantic_pass.py` → `_write_extraction` | `valid_at = source_time` for every LLM fact; `confidence = 0.9` |
| Endpoint resolution | `semantic_pass.py` → `_resolve_endpoint` | Semantic labels by normalized name; own WorkItem via ledger; Person only via existing ASSIGNED_TO/REPORTED_BY/AUTHORED_BY neighbour; other WorkItems by name are rejected |
| Derived edges | `graph/derived.py` → `materialize_around` | `_shared_concept_documents` etc. write derived edges directly at confidence 0.6 |

---

## 3. Problems

Each problem is written as: **symptom** (what a user sees), **root cause** (where in the code), **current behaviour**, **impact**, and **fixed in** (phase).

### P1 — Retrieval cuts the right node at rank 7

- **Symptom:** chat answers from the wrong or incomplete evidence even though the right node exists and matches the question.
- **Root cause:** `run_chat_turn(search_limit=6)` → `retrieve(limit=6)` → `hybrid_search(... limit=6)` → `return ranked[:limit]`. The pool inside `hybrid_search` holds 200+ candidates (30 per fulltext label, `30 × labels` vector budget). The cut is purely by rank.
- **Current behaviour:** the answer model sees 6 blocks. Anything ranked 7th or lower is invisible, however relevant.
- **Impact:** recall is capped by rank position, not by relevance. This is the "sometimes the right match is 2nd, sometimes 5th, sometimes not in top 5" problem.
- **Fixed in:** Phase 1.1 (wide pool) + Phase 2 (probability cut).

### P2 — Scores cannot be thresholded

- **Symptom:** there is no way to say "keep everything relevant, drop the rest".
- **Root cause:** RRF score is `Σ weight / (RRF_K + rank + 1)`. It encodes rank only, not relevance. Two pools with very different quality produce the same score distribution.
- **Current behaviour:** the only possible cut is a fixed count.
- **Impact:** either too few blocks (misses) or too many (cost and noise). No middle ground.
- **Fixed in:** Phase 2 (Laya gives a per-candidate probability).

### P3 — Isolated nodes never arrive through the graph

- **Symptom:** a Notion page about a ticket and the ticket itself are treated as unrelated unless the page literally contains the key.
- **Root cause:** cross-source edges come only from exact anchors (`graph/resolver.py` → `resolve_exact_anchors`). There is no retrieval-time expansion: `_entity_evidence` shows a hit's facts (neighbour *names*), but neighbours never become candidates, and their text never reaches the prompt.
- **Current behaviour:** `flow.md` §8 records the live case: "Argus API gaps" page and `DATAOS-4346` have zero edges between them. Commit `ce9b313` ("updated pipeline") has neither `MODIFIES` nor `IMPLEMENTS`.
- **Impact:** the graph adds nothing for exactly the nodes that need it most. Retrieval depends on text alone for them.
- **Fixed in:** Phase 1.3 (1-hop expansion), Phase 6 (candidate links for isolated nodes), Phase 0.2 (hidden-edge test to measure it).

### P4 — Chat context is unbounded and expensive

- **Symptom:** slow answers; unpredictable cost per question.
- **Root cause:** `run_chat_turn` builds each block from the full `hit.summary`. For `SourceFile` that is the entire file. For a merge commit it is the full `Files:` list. There is no per-block or total budget.
- **Current behaviour:** one logged question: 54,835 input tokens; one merge commit took 46% of the context.
- **Impact:** ≈ $0.22 input per question on sol in that case; more blocks (P1 fix) would make it worse unless bounded first.
- **Fixed in:** Phase 1.2 (windowing + budget), Phase 1.6 (luna A/B).

### P5 — Progress cannot be measured on one representative benchmark

- **Symptom:** every approach "sort of works"; the team keeps switching architecture.
- **Root cause:** `eval/less_token_golden.jsonl` has 7 scored questions and `eval/nilus_golden.jsonl` has 41, but the latter is an ontology-adoption regression set. Neither is a representative mixed chat benchmark. There is no per-stage metric showing whether the evidence was absent, ranked out, or lost during context packing.
- **Current behaviour:** decisions between Neuron, Laya-graph, llmtoslm and NeuralMemory were made without one shared workload and split discipline.
- **Impact:** wasted weeks; no evidence to justify cost to management.
- **Fixed in:** Phase 0.

### P6 — Selective ingestion coverage is not measured

- **Symptom:** "why did we decide X" fails when X was decided in a Jira comment, a PR description or a commit message.
- **Root cause:** Jira and Bitbucket now call `selective_chunk_writes`, but the plan has no measured breakdown of which record types become pending, are filtered as deterministic/no-signal, or actually yield durable facts.
- **Current behaviour:** free text can already reach Pass B; coverage and yield by Jira description/comment, repository/PR description, commit message and SourceFile are not reported together.
- **Impact:** adding Laya triage or changing record eligibility could duplicate existing filters, raise cost, or remove useful evidence without a measured benefit.
- **Fixed in:** Phase 3 (shadow comparison against the existing selective filter; change record eligibility only for measured gaps).

### P7 — Entity resolution is one threshold with known risks

- **Symptom:** duplicate Decisions/Terms with slightly different wording; generic hub nodes (`batch source`, `source_table` in the Laya graph run).
- **Root cause:** `_write_extraction` → `find_similar_uid(max_distance=0.1)` is the only fuzzy step (merge iff similarity ≥ 0.9). Below that, a new node. `semantic_uids` is rebuilt per chunk.
- **Current behaviour:**
  - No gray zone: 0.89 is "new", 0.90 is "merge".
  - No negation check: "use Redis for rate limiting" and "do not use Redis for rate limiting" can embed above 0.9 and merge into one node.
  - No cross-chunk memory inside a run.
  - No filter for generic names before they become nodes.
  - No record of *how* each entity was resolved, so tuning is blind.
- **Impact:** split knowledge (under-merge), welded knowledge (over-merge), hub noise.
- **Fixed in:** Phase 4.

### P8 — Text facts have no temporal life

- **Symptom:** an old Notion decision and a newer one that replaced it are both presented as live.
- **Root cause:**
  - `_write_extraction` sets `valid_at = source_time`: record time is stored as world time.
  - `supersede_fact_edges` runs only for single-valued API relations. Nothing invalidates an LLM fact when newer text contradicts it.
  - `last_confirmed_at` has no rule limiting it to content confirmation.
- **Current behaviour:** text facts only ever accumulate.
- **Impact:** "what is true now" and "what was true in March" are unreliable for Decisions — the exact questions a company knowledge graph exists for.
- **Fixed in:** Phase 5.

### P9 — Graph health is not measured or maintained

- **Symptom:** nobody knows how many nodes are isolated, how many contradictions exist, or how many duplicates.
- **Root cause:** no hygiene job. `derived.py` writes `shared_concept` edges directly instead of as reviewable candidates. There is no review queue UI (the original `plan.md` §6a planned `BridgePanel.tsx` for this, not built).
- **Impact:** quality degrades silently with scale.
- **Fixed in:** Phase 6.

### P10 — Ingestion coverage gaps are invisible

- **Symptom:** "it's not in the graph" with no way to tell whether it was never fetched.
- **Root cause:** Notion `/v1/search` is documented as incomplete; Bitbucket ingests only HEAD `.py`/`.md` files and the last N commits (default 100); Jira keeps records that left the JQL.
- **Impact:** retrieval is blamed for misses that are really ingestion misses.
- **Fixed in:** Phase 0.6.

### P11 — Laya is trained but not wired, and its weakest questions have no real data

- **Symptom:** Laya exists as a separate graph experiment.
- **Root cause:** not integrated; `same_entity` and `fact_update` trained on synthetic data only; `retrieval_relevance` 0.90 is on synthetic validation; `laya.Agent.predict` takes one state per call (no batch API seen); CPU throughput measured at ~2.7 decisions/s; input capped at 512 tokens.
- **Impact:** Laya's calibrated confidence is unused; its accuracy on Neuron data is unknown.
- **Fixed in:** Phases 2, 3, 4, 5 (integration) and §10 (data plan).

### P12 — Too many parallel approaches

- **Symptom:** four repos (`neuron`, `laya`, `llmtoslm`, `brain`) solving overlapping problems.
- **Root cause:** no benchmark to pick a winner (P5).
- **Decision in this plan:** Neuron is the base. Laya becomes a component inside it. `llmtoslm` and `brain` are parked (see §4).

---

## 4. Sources reviewed

For each source: what it is, what it does well, what we take, what we reject, and why. "Take" always means *the idea*, reimplemented in Neuron's Python code. No external framework is added as a dependency.

### 4.1 Neuron v1 (this repo) — the base

- **What it is:** FalkorDB property graph, Qdrant vectors, SQLite ledger; deterministic Pass A for every source, LLM Pass B for Notion.
- **Strong:** deterministic `uid`s, content-hash KEEP, exact-anchor linking, bi-temporal live edges + `FactHistory`, discard log with reason codes, ontology as data (37 rules), LLM-free derivation engine, hybrid search tuned on real queries.
- **Weak:** P1–P11 above.
- **Decision:** keep everything; extend.

### 4.2 Utopia (Rust/Postgres) — already adopted

- **Taken on 15 Sep:** created/updated/unchanged/moved outcomes, content-keyed re-ingestion, supersede-not-delete, reason-coded discards, reverse-direction retry, "no relation is no relation", append-only history, flag-not-delete for vocabulary, forward-chaining reasoning, capped-derivation reporting.
- **Nothing new taken in this plan.**

### 4.3 Laya (`personal_exp/laya`) — the decision component

- **What it is:** 421M non-autoregressive classifier (ModernBERT-large + decision head). Answers choice / yes-no / score questions with calibrated confidence in one forward pass. Fine-tuned on synthetic + Nilus data; ECE ≈ 0.019 on validation.
- **Strong:** repeatable and calibrated; its batched `predict_batch` API can score a frozen candidate pool with the trained `retrieval_relevance` question.
- **Weak:** cannot generate or find entities; 512-token input; CPU throughput and real-Neuron relevance quality still need measurement; synthetic-only data for `same_entity` and `fact_update`.
- **Take:** four integration points — rerank (`retrieval_relevance`), triage (`chunk_type`, `has_durable_fact`), resolution gray zone (`same_entity`), fact revision (`fact_update`). Plus `relation_type` for candidate links.
- **Reject:** the separate Laya graph pipeline (`ingest_to_falkor.py`, Knowledge/Wisdom template layers). Neuron already has Finding/Wisdom and a better fact graph.

### 4.4 The "blocking + threshold + batch classifier" plan (pasted proposal)

- **What it is:** entity resolution by blocking on keys, threshold-based shortlist instead of top-k, one Laya batch, margin rule, second merge pass.
- **Strong:** correct shape for entity resolution; the margin rule and "no fixed top-k" are right.
- **Weak:** assumed resolution was needed across all 370 nodes; in Neuron most labels have deterministic `uid`s, so fuzzy resolution only matters for Person-across-sources (already email-based) and Decision/Term/System. Thresholds (0.72, 0.15 gap) are unvalidated.
- **Take:** blocking, threshold not rank, margin, deferred merge pass — applied to Phase 4 and, more importantly, to retrieval (Phase 2).

### 4.5 GraphImmune (hackathon repo)

- **What it is:** Streamlit + Neo4j "graph health" auditor with an OpenAI tool-calling agent.
- **Strong:** the idea of auditing the graph (orphans, contradictions, duplicates, unsupported claims) and non-destructive repairs (`ALIAS_OF`, `DISPUTED_WITH`, `needs_review`) behind approval.
- **Weak:** contradiction = hard-coded opposite-word dictionary; duplicates = all-pairs `SequenceMatcher` capped at 12; "outdated" = older than 365 days; health-score weights are hand-set; the agent is a thin LLM loop over the same functions.
- **Take:** isolated-node report, cardinality/contradiction check, `DISPUTED_WITH`, repairs behind review, a health dashboard (Phase 6).
- **Reject:** all detection code, the agent, the 365-day rule, the score formula.

### 4.6 DICE (Embabel, Kotlin/JVM) — README, design notes, user guide 0.2.0

- **What it is:** a proposition-based knowledge substrate: LLM extracts natural-language propositions, entities are resolved, propositions are revised against the store, then projected to graph / vector / Prolog / memory.
- **Strong:** the most carefully reasoned lifecycle and resolution design we reviewed.
- **Take:**
  - **Escalating resolver**, cheapest first, early stop, *exactly-one* rule, candidates accumulate for one final arbiter (Phase 4.2).
  - **Resolution level logging** as the tuning diagnostic (Phase 4.6).
  - **Tune toward under-merging** — a duplicate can be collected later, a wrong merge cannot be cleanly undone (Phase 4.3).
  - **Polarity veto** — never merge a claim with its negation (Phase 4.1).
  - **Run-level session cache** (Phase 4.4).
  - **Mention filtering** before resolution (Phase 4.5).
  - **Veto for non-mintable types** (already in `_resolve_endpoint`; Phase 4.7 makes it a tested rule).
  - **Four clocks**: created / content revised / metadata revised / last accessed; decay anchored on content only (Phase 5.6).
  - **Evidence accumulation** (`reinforceCount`) (Phase 5.7).
  - **Effective confidence** computed at query time; raw confidence never mutates (Phase 5.8).
  - **Three kinds of conflict**: revision, contradiction, world progression (Phase 5.3).
  - **Pinning** exempts a fact from decay and from contradiction demotion (Phase 5.8).
  - **Admission gates** as an explicit, observable list (Phase 3.4).
  - **Multi-signal duplicate collector** with polarity veto, **dry-run first**, **audit trace** (Phase 6.4).
  - **Query-time authority tiers** (Phase 1.4).
  - **Two-hop candidate links** with review state and neutral confidence (Phase 6.2).
  - **"Both entities" lookup** (`withAllEntities`) as a retrieval lane (Phase 1.7).
  - **Router honesty**: clamp depth/size, report unsupported instead of silent full scan (Phase 1.3).
- **Reject:** propositions as the system of record (means LLM extraction on all text — the cost we removed), LLM dream-loop abstraction, `LlmGraphProjector`, Prolog, Oracle/ToolOracle agent, agent memory buckets, hard `prune-stale`, the JVM stack.

### 4.7 Graphiti (getzep) — temporal facts

- **What it is:** Python temporal knowledge-graph framework. Neuron was originally built on it and deliberately left (original `plan.md` §1, §7).
- **Strong:** bi-temporal edges (`valid_at`/`invalid_at` + `created_at`/`expired_at`), `reference_time` per episode, and a deterministic date rule in `resolve_edge_contradictions` / `resolve_extracted_edge`:
  - if the two facts' windows do not overlap → both stand;
  - else if the old fact started earlier → old `invalid_at = new.valid_at`;
  - if an existing fact is *newer* than the incoming one (out-of-order backfill) → the incoming fact is born closed.
- **Weak:**
  - contradiction is binary (its own prompt example treats "engineer → senior engineer" as a contradiction, which is really world progression);
  - every rule requires both `valid_at` values; if either is missing, nothing is invalidated and both stay live;
  - an extra LLM call per new edge for timestamps (`_extract_edge_timestamps`);
  - present-tense facts get `valid_at = episode time`, conflating written-at with true-from;
  - invalidation candidates come from a hybrid search over the whole group, not a blocked set;
  - many LLM calls per episode — the reason Neuron left.
- **Take:** the date rule and out-of-order handling (Phase 5.2), relative-date resolution against `reference_time` (Phase 5.1, done with a library, not an LLM).
- **Reject:** returning to Graphiti; per-edge timestamp LLM calls; whole-graph invalidation search; binary contradiction.

### 4.8 CoEvoKG (arXiv 2608.01904, Peking University, Aug 2026)

- **What it is:** RL training of 3–8B search agents; a proposer writes multi-hop questions from KG chains, a solver is rewarded for correct answers *and* for a path that the graph supports; verified evidence is written back.
- **Strong:** verifiable questions from graph chains; path support as geometric mean of per-hop support (one unsupported hop sinks the score); ablation is honest (task generation +1.5, path reward +1.1, write-back +0.6 macro points).
- **Weak for us:** GRPO training on Wikipedia with GPU clusters; not our problem.
- **Take:** chain-generated evaluation category with no-leakage rules (Phase 0.3), path-support check on answers (Phase 7.1), verified write-back as candidate edges (Phase 7.3).
- **Reject:** RL training of any model.

### 4.9 NeuralMemory (`personal_exp/brain`)

- **What it is:** spreading-activation memory with a query-type cascade, 7 retrievers, RRF, lateral inhibition, Hebbian write-back on read.
- **Strong:** an additional bounded retrieval round when first-pass evidence is insufficient.
- **Weak:** ~1,150-line `query()`, silent exception swallowing, non-determinism by design (priming, session EMA), only top-5 fibers reach the context.
- **Take:** the bounded retry idea, driven by first-pass evidence sufficiency rather than a learned tool router (Phase 2.2).
- **Reject:** everything else. Park the repo.

### 4.10 llmtoslm

- **What it is:** export Neuron's accepted extractions and fine-tune a small generator (Qwen/MiniCPM, LoRA/MLX).
- **Decision:** park. After Phase 3, LLM extraction runs only on triaged high-signal chunks; the remaining volume does not justify training and maintaining a generator. Revisit only if Phase 3 measurements show extraction cost is again the bottleneck.

---

## 5. Comparison matrix

"Neuron target" is what Neuron will do after this plan. The source column says where the idea comes from.

### 5.1 Retrieval

| Capability | Neuron today | Graphiti | DICE | Others | **Neuron target** | Idea from |
|---|---|---|---|---|---|---|
| Lexical + vector fusion | BM25 + 2 vector channels, tuned RRF | Hybrid RRF recipes | Router over vector / entity / graph / temporal / hybrid | NeuralMemory: 7 retrievers + RRF | Keep as is | Neuron |
| Candidate cut | Fixed top 6 by rank | Top-k | `topK` clamped | NeuralMemory: top-5 fibers | Pool of 40, calibrated scorer threshold plus diversity/token/latency caps | Pasted plan + Phase 2 trial |
| Reranker | None | Optional learned scorer | None | — | Laya `retrieval_relevance` assigns query-local evidence/bridge roles; RRF remains fallback | Laya |
| Graph expansion | None (neighbour names only) | BFS in some recipes | Neighbourhood walk with depth clamp and authority floor | NeuralMemory: spreading activation | 1 hop, typed edges, hub labels excluded, per-seed cap, authority floor | DICE |
| Query routing | Structured regex path + intent keywords | By search config | By `RetrievalMode` | NeuralMemory cascade | Run every applicable lane; deterministic applicability rules replace a learned tool router | Neuron + NeuralMemory critique |
| Trust at read time | Prompt rule only | — | Authority tiers at query time | — | `extraction_method` → tier; used in expansion and tie-breaks | DICE |
| Two-entity lookup | — | — | `withAllEntities` | — | Records `MENTIONED_IN` by both named nodes | DICE |
| Context budget | None | — | — | NeuralMemory: value-per-token budget | Per-block token window + total token budget | NeuralMemory / Neuron logs |
| Answer verification | Model self-reports `used_sources` | — | Rationale projector | CoEvoKG: path support | Path-support check (Phase 7) | CoEvoKG |

### 5.2 Ingestion and extraction

| Capability | Neuron today | Graphiti | DICE | Others | **Neuron target** | Idea from |
|---|---|---|---|---|---|---|
| What goes to the LLM | Chunks admitted by the existing deterministic `selective_chunk_writes` policy across supported sources | Every episode, several calls | Every chunk | Laya: classify first | Existing selective policy plus Laya only if shadow evaluation improves cost without unacceptable fact loss; source coverage changed only from measured gaps | Neuron + Laya |
| Admission gates | Evidence verbatim, relation allow-list | — | Confidence, evidence, trust, merge, conflict, projection gates; observable | — | Explicit ordered list of 6 gates, all logged | DICE |
| Schema strictness | Strict via axioms | Custom entity/edge types | STRICT / DEFAULT / RELAXED | — | Keep strict | Neuron |
| Concurrency | LLM concurrent, writes serial | Concurrent | Extraction concurrent, resolution serial | — | Keep | Neuron = DICE |

### 5.3 Entity resolution

| Capability | Neuron today | Graphiti | DICE | Others | **Neuron target** | Idea from |
|---|---|---|---|---|---|---|
| Identity / ladder | Name-derived UID, then vector ≥ 0.9 | Search + LLM dedupe | Exact → normalized → partial → fuzzy → vector → LLM verify → LLM bake-off | Pasted plan: block → shortlist → classifier | System/Term identity scoped by namespace; Decision identity scoped to source record + statement; normalized → alias → vector exactly-one → gray zone → review | Neuron + DICE + Laya |
| Exactly-one rule | No | No | Yes | Margin rule (pasted plan) | Yes | DICE |
| Gray zone | None | LLM decides | LLM decides | `possibly_same_as` (Laya doc) | `POSSIBLY_SAME_AS` + review queue | Laya doc |
| Negation guard | None | None | Polarity veto | — | Polarity veto before any merge | DICE |
| Run memory | Per chunk | Per episode | Per run (`InMemoryEntityResolver`) | — | Per run | DICE |
| Mention filter | None | — | Schema / context filters | — | Stoplist + specificity check for Term/System | DICE |
| Diagnostics | None | — | `ResolutionLevel` per decision | — | `resolved_by` per entity + distribution per sync | DICE |

### 5.4 Temporal facts and lifecycle

| Capability | Neuron today | Graphiti | DICE | Others | **Neuron target** | Idea from |
|---|---|---|---|---|---|---|
| World time | `valid_at` / `invalid_at` | `valid_at` / `invalid_at` | Optional validity window | — | Keep, plus `valid_at_basis` (`stated` / `record_time`) | Neuron + Graphiti critique |
| Record time | `first_seen_at`, `FactHistory.observed_*` | `created_at` / `expired_at` | Four clocks | — | Four clocks: `first_seen_at`, `last_confirmed_at` (content only), `metadata_revised_at`, `last_retrieved_at` | DICE |
| API fact supersession | `supersede_fact_edges` + `FactHistory` | Via contradiction | — | — | Keep | Neuron |
| Text fact supersession | None | LLM contradiction + date rule | Reviser: identical / similar / contradictory / generalizes / unrelated | Laya `fact_update` | Laya `fact_update` + Graphiti date rule | Graphiti + Laya |
| Conflict kinds | — | Binary | Revision / contradiction / world progression | — | `corrects` / `contradicts` / `newer_state` | DICE |
| Unknown dates | `ended_unknown` exists | Nothing happens | — | GraphImmune: 365-day rule | `ended_unknown` + `attested_from`; never guess | Neuron |
| Out-of-order backfill | API only via changelog | Incoming fact born closed | — | — | Incoming fact born closed | Graphiti |
| Decay | None | None | Effective confidence, hysteresis, pinning | GraphImmune: age | Decay class by `chunk_type`, shown not hidden, hysteresis, `pinned` | DICE + Laya |
| Evidence accumulation | `source_record_keys` array | Episodes list | `reinforceCount`, `absorbEvidence` | — | Distinct supporting records = reinforce count | DICE |

### 5.5 Hygiene, review and evaluation

| Capability | Neuron today | Graphiti | DICE | Others | **Neuron target** | Idea from |
|---|---|---|---|---|---|---|
| Isolated-node metric | None | — | — | GraphImmune: orphan ratio | Per-label isolated counts per sync | GraphImmune |
| Candidate links | `derived.py` writes directly | — | Two-hop discovery, review state | Laya doc: cross-record worker | Two-hop + embedding candidates → Laya `relation_type` → review | DICE + Laya |
| Duplicate cleanup | Write-time only | Write-time LLM | Multi-signal collector, dry-run, trace | — | Multi-signal collector for Decision/Term, dry-run + trace | DICE |
| Contradiction audit | — | — | Contradiction pass | GraphImmune | Cardinality check from axioms + `DISPUTED_WITH` report | GraphImmune + axioms |
| Review queue | Planned (`BridgePanel.tsx`), not built | — | Review state on links | GraphImmune: repair workbench | Minimal SQLite queue + CLI/endpoint before model suggestions; UI later | Neuron plan + GraphImmune |
| Golden set | 7-question `less_token` set + 41-question Nilus ontology regression set | — | — | CoEvoKG: chain-generated | 200–300 mixed questions: regressions, real workload, unanswerable and chain categories | CoEvoKG + real workload |
| Stage metrics | Final MRR / recall only | — | — | — | Candidate recall, rerank recall, hidden-edge recall, cost per question | This plan |

---

## 6. Design principles

Every change in §8 must satisfy all of these. A change that breaks one needs an explicit exception in its phase.

1. **Store always; gate edges and model calls.** No model decides whether a record is kept. Raw text is always embedded and searchable. Models decide only which edges exist, which chunks reach the LLM, and what reaches the answer.
2. **Use calibrated relevance within explicit resource limits.** Prefer a tuned score threshold for relevance, then apply diversity, latency and token caps. A fixed count remains a safety bound, not the primary relevance decision.
3. **Measure before and after.** Every phase names the metric it moves (§11) and records a before/after row.
4. **Deterministic first, model last.** A regex, an index lookup or an axiom always runs before Laya; Laya always runs before an LLM.
5. **Never guess time.** If a date is not stated, it is not invented. Unknown is a value (`ended_unknown`, `undated`).
6. **Mark, never delete.** Supersede, dispute, flag, decay. Deletion only through the existing disconnect/reconcile paths.
7. **Under-merge rather than over-merge.** Duplicates are recoverable; wrong merges are not.
8. **Suggest before auto.** Every new model decision starts in shadow or suggest mode and is promoted to auto-accept only after review data shows it is safe.
9. **Everything observable.** Each gate, rung and skip writes a reason to the ledger or logs, so "why is this missing" has an answer.
10. **Do not break what works.** The structured path, `find_named_persons`, time-window lanes, the axioms gate, `evidence_in_chunk`, and the discard log are untouched unless a phase says otherwise.

---

## 7. Target architecture

### 7.1 Ingestion (after the plan)

```
Provider API
  │
  ├─ ledger hash compare ── KEEP → stop
  │
  ▼  INSERT / UPDATE
PASS A (deterministic, unchanged)
  SourceRecord → structural nodes/edges → exact anchors → embed search_text
  │
  ▼  chunks saved for Pass B (Notion + NEW: Jira description/comments, PR/commit messages)
GATE 1  existing selective policy + Laya shadow triage              (Phase 3)
  │       skip → still embedded, drop reason LAYA_TRIAGE_SKIP
  ▼
LLM extraction (_call_llm, unchanged prompt/schema)
  │
  ▼
GATE 2  evidence verbatim       evidence_in_chunk                    (exists)
GATE 3  relation allowed        axioms.resolve_direction             (exists)
  │
  ▼
ENTITY IDENTITY + RESOLUTION                                         (Phase 4)
  System/Term: namespace + name → alias → vector exactly-one → review
  Decision: source record + normalized statement → review-only cross-record merge
  │
  ▼
GATE 4  merge candidate         duplicate → reinforce, no new node   (Phase 4/5)
GATE 5  conflict classification before any write                      (Phase 5)
          Laya fact_update + Graphiti date rule
GATE 6  projection eligibility  low confidence → candidate, not live (Phase 5)
  │
  ▼
write edges (`revive=False` for text/history) + FactHistory + ledger + discard log
```

### 7.2 Retrieval (after the plan)

```
question
  │
  ├─ 1. structured path (resolve_structured) ── complete → answer    (exists)
  ├─ 2. run all applicable lanes: RRF pool, person, pair, temporal,
  │      Wisdom and Finding                                          (Phase 1 + 2)
  ├─ 3. Laya `retrieval_relevance` over text + linked/temporal facts (Phase 2.1)
  │      roles: direct evidence · bridge · temporal · irrelevant
  ├─ 4. if evidence is thin: expand only direct/temporal/bridge seeds
  │      and rescore; at most two bounded rounds                     (Phase 2.2)
  ├─ 5. direct + temporal evidence only; bridge/rejected candidates
  │      never reach the answer model                               (Phase 2.2)
  ├─ 6. evidence: best token window per block + total token budget   (Phase 1.2)
  │      facts show valid_at_basis, last_confirmed, decay class      (Phase 5)
  ├─ 7. answer LLM (sol or luna by A/B)                             (Phase 1.6)
  └─ 8. (Phase 7) path-support verification
```

---

## 8. Phases

Each phase lists: **goal**, **problems fixed**, **current vs target**, **changes** (file → function), **tests**, **exit criteria**, **risks**, **effort**.

---

### Phase 0 — Measurement first

**Goal:** a benchmark large enough to tell real improvements from noise, with per-stage numbers.
**Fixes:** P5, P10; prepares measurement for P1–P4.

| | Current | Target |
|---|---|---|
| Evaluation | 7 scored `less_token` questions; 41 scored Nilus ontology-regression questions; small Argus set | 200–300 mixed questions, while preserving each existing set as a separately reported regression slice |
| Stage metrics | Final MRR / recall@8 | Candidate recall, rerank recall, post-packing evidence recall, MRR, hidden-edge recall, tokens and $ per question |
| Coverage | Unknown | Provider count vs ledger count per sync |

#### 0.1 Refresh the current baselines (S)

Run the current code without retrieval changes on:

- all 41 scored entries in `eval/nilus_golden.jsonl`, preserving separate `control` and `target` results;
- all scored entries in `eval/less_token_golden.jsonl`;
- `argus_golden.jsonl` as a separate compatibility slice where its graph is available.

Record date, git SHA, graph snapshot/collection, flags, model, candidate/final metrics, token usage, latency and cost in `eval/results.md`. Do not combine datasets with different purposes into one headline score.

#### 0.2 Mixed evaluation set (M)

Build a 200–300 question set with an explicit `category` and evidence annotations:

1. **Existing regressions** — retain the Nilus, `less_token` and Argus cases without rewriting their expected answers.
2. **Real workload questions** — de-identified chat questions where available, plus hand-written equivalents covering key/name lookup, explanation/"why", temporal, person/activity and overview queries.
3. **Ambiguous questions** — cases with multiple plausible entities or insufficient qualifiers. The expected result may be a clarification rather than a node.
4. **Unanswerable questions** — the evidence is absent from the authorized graph. The expected result is abstention; these measure false-positive answers.
5. **Chain questions** — generated multi-hop questions as a distinct category, never treated as the whole benchmark.

Split dev/test by project or connected evidence group so near-duplicate records and adjacent chain nodes cannot cross the split. Tune thresholds on dev only. Keep a frozen hand-labelled test set and report every category separately.

Each mixed-set row records `category`, `project_or_evidence_group`, `expected_mode` (`answer|abstain|clarify`), zero or more acceptable `answer_uids`, and labelled support spans/fact UIDs. This makes post-packing evidence recall and abstention measurable without pretending every question has exactly one node answer.

#### 0.3 Chain-generated category — new `scripts/build_chain_golden.py` (M)

1. **Sample chains** from asserted edges only (`r.derived = false`, `r.invalid_at IS NULL`), 2–3 hops, along the relations that carry meaning:
   ```
   Document -DOCUMENTS-> WorkItem <-IMPLEMENTS- Commit -MODIFIES-> SourceFile
   WorkItem -PARENT_OF-> WorkItem -ASSIGNED_TO-> Person
   Decision -APPLIES_TO-> System <-APPLIES_TO- Decision
   ```
   Exclude `Repository`, `Project`, `Workspace`, `SourceRecord` as intermediate nodes (hubs). One chain per start node per relation pattern to avoid over-sampling popular nodes.
2. **Generate one question per chain** with one LLM call (luna is enough). Prompt rules, taken from CoEvoKG:
   - generate both indirect questions and realistic anchored questions; mark the variant explicitly;
   - the answer is exactly one node on the chain (not always the last one);
   - answering requires at least two hops;
   - one concise answer.
3. **Deterministic leakage filter:** reject if any chain node's `name`, `issue_key`, `path`, `sha` prefix or `pr_ref` appears in the question.
4. **Hand check** 20–30 questions; drop the generator prompt changes into the script so the set is reproducible.
5. **Output** `eval/chain_golden.jsonl`:
   ```json
   {"id": "c-0142", "question": "...", "answer_uid": "...", "chain_uids": ["...", "...", "..."],
    "chain_relations": ["DOCUMENTS", "IMPLEMENTS"], "hops": 2, "generator": "chain-v1"}
   ```
6. **Split** by project/evidence group using the same split map as the mixed set. Never tune on test.

Cost: ~300 short LLM calls, one time.

#### 0.4 Hidden-edge test — in the eval harness (S)

For each chain question, run retrieval twice: normal, and with one chain edge excluded from expansion (pass `exclude_edges={(from_uid, rel, to_uid)}` to the expansion step added in Phase 1.3). Report recall of both endpoints with the edge hidden. Before Phase 1 there is no expansion, so the two runs are identical — that is the baseline.

This is the direct measurement of the isolated-node problem (P3).

#### 0.5 Stage and packing metrics — the harness that calls `retrieve()` (S)

Record per question:

| Field | Meaning |
|---|---|
| `candidate_uids` | Everything that entered the pool (before rerank) |
| `final_uids` | What reached the answer prompt |
| `packed_uids` | Nodes with at least one evidence span that survived the final token pack |
| `gold_in_candidates` | Answer node present before rerank |
| `gold_in_final` | Answer node present after rerank |
| `gold_evidence_in_pack` | A labelled supporting span, fact or citation survived packing; a node whose useful evidence was truncated counts as a miss |
| `gold_rank_final` | Rank in final list (for MRR) |
| `chain_coverage` | Fraction of chain nodes in final list |
| `input_tokens`, `output_tokens`, `usd` | From `TokenUsage` |

Keep `retrieve()` as the retrieval measurement boundary, then separately measure the evidence pack produced by `run_chat_turn`. Candidate recall and post-packing evidence recall must never be conflated.

#### 0.6 Sync coverage report — connector routes (S)

After each sync, log and store in the ledger: provider-reported total (when the API gives one), fetched count, ledger count, skipped-by-rule count (e.g. Bitbucket non-`.py`/`.md`, commits beyond N). Surface it in `SyncProgress.tsx`.

**Exit criteria:** current-code baseline rows recorded for Nilus and `less_token`; a frozen mixed test split exists; every result reports candidate, final and post-packing evidence recall plus MRR, abstention accuracy, median/p90 tokens, latency and cost.
**Risks:** generated questions too easy or too unlike real use → report them as a separate slice and require real, ambiguous and unanswerable categories in the frozen test set.

---

### Phase 1 — Retrieval without new models

**Goal:** stop cutting relevant nodes, pull neighbours in through the graph, and bound the context.
**Fixes:** P1 (partially), P3 (partially), P4.

Implementation order inside this phase is 1.2 first, then 1.1/1.3: the token pack must be bounded before a wider pool or expansion can reach production.

| | Current | Target |
|---|---|---|
| Pool reaching the cut | 6 | 40 |
| Neighbours as candidates | No | 1 hop, typed, capped |
| Evidence per block | Full `search_text` | Best token window with a labelled supporting span where available |
| Total context | Unbounded | Explicit input-token budget |
| Trust in expansion | None | Authority floor by tier |

#### 1.1 Wide pool — `graph/chat.py` → `retrieve` (S)

In the `else` branch (no knowledge-layer intent), call `hybrid_search(..., limit=POOL_SIZE)` with `POOL_SIZE = int(os.getenv("NEURON_POOL_SIZE", "40"))`. `search.py` needs no change; it already returns `ranked[:limit]`.

Until Phase 2 lands, the final cut is `candidates[:search_limit]` as today, so behaviour does not change unless `NEURON_RERANK` is set. This lets Phase 0 measure candidate recall@40 immediately.

#### 1.2 Token-based context windowing and packing — `graph/chat.py` → `run_chat_turn` (M)

- New helper `best_window(question, text, tokens, encoder)` (shared with Phase 2, lives in `graph/text_window.py`): create overlapping token windows, score them by lexical/query-term overlap, and return the best window. Preserve token boundaries and return the text unchanged when it fits.
- Block text = `best_window(question, hit.summary, NEURON_BLOCK_TOKENS=500, encoder=model_encoder)`.
- Facts per block capped at `NEURON_FACTS_PER_BLOCK=25`, asserted before derived.
- Total evidence budget `NEURON_CONTEXT_TOKENS=10000` by default, leaving explicit room for the system prompt, question and answer. Count tokens with the answer model's tokenizer; do not infer tokens from characters.
- Pack evidence spans and facts within the budget rather than accepting or dropping only whole nodes. Record which labelled support spans survived.
- Keep the existing per-block `evidence` log line; add `window_tokens`, `packed_tokens`, `truncated` and `gold_support_preserved` in eval mode.

#### 1.3 One-hop expansion — new `graph/expand.py` (M)

```python
EXPAND_RELS = ["IMPLEMENTS", "DOCUMENTS", "PARENT_OF", "REFERENCES",
               "MODIFIES", "APPLIES_TO", "DEFINES"]
HUB_LABELS  = {"Repository", "Project", "Workspace", "Person", "SourceRecord"}
MAX_SEEDS, PER_SEED = 8, 4

def expand_neighbors(graph, seed_uids, scope, providers, *,
                     min_tier="derived", exclude_edges=frozenset()):
    # one Cypher query, both directions, live edges only, ACL via MENTIONED_IN
    # returns SearchHit(methods=[f"graph:{rel}"], score=0.0)
    # asserted edges first, then derived; PER_SEED cap; hub labels skipped
    # edges in exclude_edges skipped (used by the hidden-edge test)
```

- Seeds = first `MAX_SEEDS` of the pool. Not the whole pool, or expansion floods the candidates.
- Clamp: never more than `MAX_SEEDS × PER_SEED` new candidates; log when the cap is hit (router honesty, from DICE).
- `CONTAINS`, `BELONGS_TO`, `AUTHORED_BY`, `ASSIGNED_TO` are excluded as expansion paths: they lead to hubs. Assignee information still reaches the answer through `_entity_evidence` facts.
- Candidates = pool + new neighbours, de-duplicated by uid.

#### 1.4 Authority tiers — `graph/expand.py` + `graph/chat.py` (S)

| `extraction_method` | Tier |
|---|---|
| `deterministic`, `exact_anchor`, `changelog` | `primary` |
| `llm` | `secondary` |
| `derived` | `derived` |
| missing | `unknown` (lowest) |

Expansion takes `min_tier`: `primary` for lookup questions (structured-ish, named key), `derived` otherwise. The tier is also passed into the evidence block so the answer model sees it. No stored data changes; the tier is computed at read time (DICE: trust policy changes more often than data).

#### 1.5 Knowledge layers into the pool — `graph/chat.py` → `retrieve` (S, after Phase 2)

Today Wisdom/Finding lanes run only when `_WISDOM_INTENT` / `_FINDING_INTENT` keywords appear. After Phase 2, add the top 5 Wisdom and top 5 Finding hits to every candidate pool and let the reranker decide. Keep `_actionable_wisdom_hits` (status filter) and `_linked_finding_hits` (lineage). Do not do this before rerank exists — they would be drowned or would crowd the fixed cut.

#### 1.6 Chat model A/B — config only (S)

After 1.2, run the dev split with `CHAT_MODEL=gpt-5.6-luna` and `gpt-5.6-sol`. Compare answer accuracy (LLM-judged against `answer_uid` + human spot check of 30) and $ per question. Pick per deployment.

#### 1.7 Two-entity lane — `graph/chat.py` → `retrieve` (S)

If the question contains two anchors that resolve to nodes (two ticket keys, a key and a person, a key and a file path — reuse `graph/bridge/anchors.py` regexes and `find_named_persons`), fetch the `SourceRecord`s that both nodes are `MENTIONED_IN` and the edges between them, and prepend them as candidates with `methods=["pair"]`. DICE `withAllEntities`: claims mentioning both endpoints are the claims about the pair.

**Tests:** unit tests for `best_window` (short text unchanged, token limit respected, window contains most terms), `expand_neighbors` (hub labels skipped, per-seed cap, derived after asserted, `exclude_edges` honoured, ACL filter applied), packing (hard token limit, deterministic order, support-span accounting).
**Exit criteria:** on the dev split vs Phase 0 baseline — candidate recall@40 improves materially, hidden-edge recall rises, post-packing evidence recall does not fall more than 2 points below final-node recall, and median input tokens per question ≤ 12k. Do not hard-code a 15-point gain before the refreshed baseline is known.
**Risks:** larger pool with the old fixed cut changes nothing (expected until Phase 2); expansion adds noise (mitigated by per-seed cap and by Phase 2).
**Effort:** M overall.

---

### Phase 2 — Laya relevance gate with bounded bridge expansion

**Goal:** close the rank-cut gap while keeping query-rejected nodes available as bounded graph or temporal bridges.
**Fixes:** P1, P2; starts P11.
**Implementation status (25 Sep 2026):** the real batched Laya adapter, query-local roles, all-lane pool, conditional bounded expansion, score/rescore flow, RRF fallback and evaluation trace are implemented. Threshold calibration, deployment latency measurement and promotion remain open.

Run three variants over the same frozen questions, initial candidate UIDs, candidate text windows and final token budget:

| Variant | Retrieval / scoring |
|---|---|
| A | Current RRF and top-6 behavior |
| B | Phase 1 wide pool + unconditional one-hop expansion, RRF final order |
| C | All applicable lanes → Laya roles → conditional bounded expansion → Laya rescore |

Report category-level candidate recall, final recall, post-packing evidence recall, bridge recovery rate, MRR, answer/abstention accuracy, calibration, p50/p90 latency, peak memory and cost. CPU and GPU are separate operational profiles. Promote C only when it improves evidence and answer quality within the deployment latency budget; RRF remains the failure fallback.

#### 2.1 Laya scorer and query-local roles — `graph/rerank.py` (M)

The scorer accepts frozen `(question, candidate_window)` records and returns a probability plus model/version metadata. The candidate window contains the node text and a small ACL-filtered preview of linked and temporal facts, including historical intervals relevant to `at` / `as_of`. Log every score for evaluation and calibration.

Use the trained schema exactly: state `{"question", "node"}` and question `retrieval_relevance`. Load one process-level `laya.Agent` lazily and call `predict_batch`; do not invent an untrained role-choice prompt.

Assign roles by deterministic policy around the probability and retrieval method:

- `direct_evidence` — clears the tuned threshold, or is a deterministic person/pair match;
- `temporal_context` — came from the applicable time-window lane and remains visible even when semantic wording is weak;
- `bridge_candidate` — a capped set below the direct threshold, retained only as expansion seeds; prefer candidates above the bridge floor, then backfill from the strongest rejected candidates so a weak first hop cannot make a valid second hop unreachable;
- `irrelevant` — excluded from both the answer and expansion.

Rejection is query-local. Never store “rejected” on a node or fact, and never use a previous query's rejection as a permanent negative signal.

#### 2.2 Two-pass retrieval — `graph/chat.py` → `retrieve` (M)

1. Return immediately for a complete structured result.
2. Run every applicable lane: wide hybrid RRF, Wisdom, Finding, pair, named-person and time-window. Applicability is deterministic; there is no learned tool selector.
3. Score the deduplicated pool once with Laya.
4. If accepted evidence is below `NEURON_RERANK_MIN_DIRECT`, expand only direct, temporal and bridge candidates with the typed authority-bounded graph expansion.
5. Score only newly discovered neighbours. Repeat up to `NEURON_RERANK_MAX_ROUNDS`; never revisit a UID.
6. Send only direct and temporal candidates to token packing. Bridge and irrelevant candidates never reach the answer model.

On scorer/package/checkpoint failure, log the failure and fall back to the Phase 1 RRF + expansion order. The structured path is never model-gated.

#### 2.3 Serving and latency (S–M)

- Measure first-pass pool size, candidates scored per retry, retry rate, cold start, warm p50/p90, batch throughput and peak memory on deployment-class hardware.
- Use Laya's real `predict_batch`; verify order and score equality against single-item inference before promotion.
- Cap bridge seeds, per-seed neighbours, retry rounds, accepted blocks and final evidence tokens independently.
- If synchronous Laya misses the product latency target, keep `NEURON_RERANK=off` while retaining the RRF path; do not silently lower recall by shrinking the initial pool without measurement.

#### 2.4 Tune and fine-tune (M)

- Tune the direct threshold, bridge floor, bridge limit, minimum accepted evidence and retry count on the grouped dev split.
- Labels must distinguish direct evidence, useful bridge, temporal context and irrelevant. Gold nodes/spans are direct positives; a node absent from one annotated chain is **not automatically irrelevant**, because another valid evidence path may exist.
- Review hard negatives before training. Evaluate once on the frozen grouped test split and report first-pass success separately from bridge-recovered success.

**Tests:** exact Laya instruction/state equality; deterministic batched order; query-local roles; temporal/exact preservation; rejected-node bridge recovery; bridges never reach the answer; bounded retries and dedupe; scorer-failure RRF fallback; final token-budget safety.
**Exit criteria (test split):** C improves post-packing evidence recall and answer/abstention accuracy over B, bridge recovery adds true answers without unacceptable false positives, and deployment p90 meets the product target. Otherwise keep Phase 1 RRF and record that Laya did not earn promotion.
**Risks:** synthetic-trained Laya relevance may not transfer to Neuron; bridge expansion can amplify weak seeds; CPU serving can dominate latency. Mitigate with query-local roles, strict caps, real labels, batching and explicit no-promotion/fallback behavior.
**Effort:** M.

---

### Phase 3 — Minimal review queue and measured ingestion triage

**Goal:** give later model suggestions a safe review path, then test whether Laya improves the existing selective-ingestion policy.
**Fixes:** P6; continues P11.

| | Current | Target |
|---|---|---|
| Chunks to LLM | Chunks admitted by `selective_chunk_writes` | Existing admission plus Laya only if shadow results justify it |
| Jira / Bitbucket extraction | Selective chunks can already become pending | Eligibility changed only for record types with measured missed durable facts |
| Skipped chunks | — | Still embedded, logged as `LAYA_TRIAGE_SKIP` |
| Review | No shared queue | Minimal SQLite queue with CLI or small API; frontend later |
| Gates | 2 implicit | 6 explicit, ordered, logged |

#### 3.0 Minimal review queue — ledger + CLI/small endpoint (S)

Create `reviews(id, type, payload, state, decided_by, decided_at, created_at)` in SQLite with `pending`, `approved` and `rejected` states. Provide either a small CLI or the two endpoints `GET /api/reviews` and `POST /api/reviews/{id}/approve|reject`. Cache rejection identities so the same proposal is not recreated. This supports `possibly_same_as`, `fact_update` and later link/duplicate candidates. `BridgePanel` and dashboard work stay in Phase 6.

#### 3.1 Shadow mode against the current policy — `graph/semantic_pass.py` (S)

For every chunk admitted by the existing selective policy, run Laya `chunk_type` + `has_durable_fact` and store the result in the ledger (`triage_type`, `triage_durable_p`, `triage_model`). Do not change admission in shadow mode. Also sample chunks rejected by the existing policy so its own false-negative rate can be estimated rather than treating it as ground truth.

After one full sync, compute: for chunks Laya *would* skip, how many facts did the LLM write? That number is the cost of enforcing triage.

#### 3.2 Enforce — same place (S)

Skip rule (initial, tuned from 3.1):

```
skip if triage_type in {"noise", "scheduling"}
     or (triage_type == "discussion" and triage_durable_p < 0.3)
     or (triage_durable_p < 0.15)
```

Skipped chunks: `ledger.commit_chunk(..., SemanticStatus.DONE)` with drop reason `LAYA_TRIAGE_SKIP` recorded via `record_drops`. The record's own embedding is already written in Pass A; nothing is lost from search.

Add `DropReason.LAYA_TRIAGE_SKIP` to `connectors/core/ledger.py`.

#### 3.3 Change source eligibility only from measured gaps (M, conditional)

First report pending/skipped/yield/cost by source and record type: Jira project/issue description and comments; Bitbucket repository/PR descriptions, commit messages and SourceFiles. The current pipelines already invoke `selective_chunk_writes`, including SourceFiles, so this is an audit of actual behavior rather than a blanket reopen.

Only add or remove eligibility when the mixed evaluation and sampled skipped chunks demonstrate a gap. Preserve `LLM_BUDGET_PER_RUN`; the pending queue drains large backlogs over several runs. Any proposal to exclude SourceFiles must show that they add cost without unique durable facts or retrieval evidence.

#### 3.4 Admission gates as an explicit list — `semantic_pass.py` (S)

Document and log in this order (each writes a `DropReason` when it rejects):

| # | Gate | Where | Status |
|---|---|---|---|
| 1 | Existing selective admission + optional Laya triage | before `_call_llm` | existing + conditional new |
| 2 | Evidence verbatim | `evidence_in_chunk` | exists |
| 3 | Relation allowed / direction | `axioms.resolve_direction` | exists |
| 4 | Merge candidate | resolution ladder (Phase 4) | new |
| 5 | Conflict classification | `resolve_text_fact` (Phase 5) | new |
| 6 | Projection eligibility | low-confidence facts written as candidates | new |

**Tests:** review state transitions and rejection dedupe; skip rule unit test; skipped chunk still marked DONE and has a drop row; source-type coverage accounting matches the existing selective policy.
**Exit criteria:** review proposals have an auditable home; shadow run shows ≤ 5% of accepted durable facts come from would-be-skipped chunks; enforcement reduces calls materially without degrading the mixed eval; source-type extraction yield and ingestion cost are reported.
**Risks:** triage too aggressive → tune from shadow data; eligibility changes increase cost → require measured yield and keep the budget cap.
**Effort:** M.

---

### Phase 4 — Entity resolution ladder

**Goal:** fewer duplicates, zero wrong merges, and a way to see how resolution behaves.
**Fixes:** P7.

| | Current | Target |
|---|---|---|
| Identity | label + normalized name | System/Term: namespace + normalized name; Decision: source record + normalized statement |
| Steps | normalized name → vector ≥ 0.9 | scoped exact identity → alias → vector exactly-one → gray zone → review |
| Gray zone | none | 0.75–0.9 → `POSSIBLY_SAME_AS`, review |
| Negation | not checked | veto |
| Memory | per chunk | per run |
| Diagnostics | none | `resolved_by` per entity, distribution per sync |

All changes live in `graph/semantic_pass.py` → `_write_extraction`, around the existing `find_similar_uid(...) or semantic_uid(...)` call, plus `graph/vector_store.py` for a thresholded candidate query. First replace global name identity with the scoped rules below; similarity must never override scope silently.

#### 4.0 Scoped identity keys — do this before fuzzy resolution (M)

- **System and Term:** `uid = make_uid(label, namespace_uid, normalized_name)`. Use project when the source maps unambiguously to one project; otherwise use workspace/connection namespace. Store `namespace_uid` explicitly. The same name in two namespaces is not the same entity by default.
- **Decision:** `uid = make_uid("Decision", source_record_key, normalized_statement)`. A Decision is an assertion from a source, not a globally name-keyed entity. Reworded or cross-record Decisions may be proposed as duplicates, but can merge only after review.
- **Other semantic types:** retain their current deterministic identity until a type-specific scope is defined.
- Aliases are namespace-aware: `entity_aliases(label, namespace_uid, alias_norm, uid, source)`.

#### 4.1 Polarity veto (S)

Before any merge (vector or Laya), compare negation markers in the two texts:

```python
_NEG = re.compile(r"\b(not|no longer|don't|do not|never|instead of|rejected|avoid|deprecated|stop(ped)? using)\b", re.I)

def polarity_conflict(a: str, b: str) -> bool:
    return bool(_NEG.search(a)) != bool(_NEG.search(b))
```

If `polarity_conflict`, never merge; create a new node and, for Decisions, hand the pair to Phase 5 as a conflict candidate. This closes a real risk: two decisions that differ only by "not" embed very close together, possibly above the current 0.9 cut.

#### 4.2 The scoped ladder (M)

```
1. mention filter (4.5)                   → drop; DropReason.GENERIC_MENTION
2. scoped identity hit                    → existing node          resolved_by = "scoped_exact"
3. scoped alias table hit                 → existing node          resolved_by = "alias"
4. vector: candidates with sim ≥ 0.90
     exactly one, no polarity conflict    → existing node          resolved_by = "vector"
     more than one                        → go to 6 with all of them
5. vector: candidates with 0.75 ≤ sim < 0.90 → go to 6
6. Laya same_entity on every candidate (blocked by label + namespace)
     top p ≥ 0.85 and top − second ≥ 0.15
       Decision                           → new node + review proposal        resolved_by = "review_required"
       NEURON_RESOLVE_MODE=suggest        → new node + POSSIBLY_SAME_AS(top) resolved_by = "laya_suggest"
       NEURON_RESOLVE_MODE=auto           → existing System/Term only        resolved_by = "laya"
     else                                 → new node                         resolved_by = "new"
```

- "Exactly one" rule (DICE): a rung is confident only if it returns exactly one match. Two exact hits are not confident.
- Candidates are **blocked by label and namespace** and fetched with `vector_store.search_above(label, namespace_uid, embedding, min_similarity, limit=20)` — threshold with a resource ceiling of 20.
- Laya state: `{"mention": name, "mention_context": best_window(chunk, 400), "candidate": node.name, "candidate_profile": node.search_text[:400]}` (matches the Laya schema comment).
- Start in `suggest` mode. System/Term resolution may later promote to auto under §10.4. Decision merges remain review-only regardless of model precision.

#### 4.3 Under-merge by default (policy)

Thresholds are set so that doubt produces a new node. Duplicates are collected later by Phase 6.4 (auditable, reversible in review). A wrong merge mixes two entities' facts and cannot be cleanly split.

#### 4.4 Run-level cache (S)

Move `semantic_uids` from a local in `_write_extraction` to a per-run dict keyed by the scoped identity tuple and owned by `run_semantic_pass`. Writes are already serial on the main thread, so no locking is needed.

#### 4.5 Mention filter (S)

For `Term` and `System` only:
- stoplist (`data`, `pipeline`, `source`, `table`, `service`, `api`, `system`, `config`, `batch source`, `source_table`, …) kept in the ledger like the axioms, editable without deploy;
- minimum length 3 and at least one non-generic token;
- optionally Laya `entity_type == "other"` with confidence ≥ 0.8 → drop.

Rejected mentions are recorded (`DropReason.GENERIC_MENTION`) and counted, like `record_miss` does for relations.

#### 4.6 Diagnostics (S)

Log `resolved_by` for every entity; aggregate per sync into the ledger (`resolution_stats`: label × resolved_by × count). Read it the DICE way:

| Pattern | Meaning | Action |
|---|---|---|
| Mostly `scoped_exact` / `alias` | healthy, free | none |
| Many `laya_suggest` with many candidates | recall too wide | raise 0.75 floor |
| Many `new` for things that look duplicate | recall too narrow | add aliases, lower floor |
| Many polarity vetoes | decisions being reversed | check Phase 5 conflict output |

#### 4.7 Veto for API-only types (S)

`_resolve_endpoint` already refuses to create Person / WorkItem / Commit from text. Add a test per label that an LLM mention of an unknown Person, WorkItem, Commit, PullRequest, SourceFile or Repository is rejected with `ENDPOINT_UNRESOLVED`, never minted.

#### 4.8 Alias table (S)

A ledger table `entity_aliases(label, namespace_uid, alias_norm, uid, source)` filled by approved `POSSIBLY_SAME_AS` reviews, approved Phase 6.4 pairwise merges, and manual entries. Step 3 of the ladder reads it.

**Tests:** same System/Term name in two namespaces produces different UIDs; repeated scoped name reuses a UID; Decision UID includes source record + statement; Decision merge always creates a review; polarity veto blocks merge; exactly-one rule; suggest mode never changes UID; scoped run cache; stoplist drop recorded.
**Exit criteria:** on a hand-labelled set of 100 Decision/Term pairs — duplicates remaining down ≥ 30% vs today; wrong merges = 0; resolution stats visible per sync.
**Risks:** Laya `same_entity` weak on real data → stays in suggest mode; stoplist too broad → counted drops make it visible.
**Effort:** M.

---

### Phase 5 — Temporal facts for text

**Goal:** text facts get a correct time, get closed when replaced, and show their age when nothing replaces them.
**Fixes:** P8.

| | Current | Target |
|---|---|---|
| `valid_at` for LLM facts | `source_time` always | Stated date when present; else `source_time` with `valid_at_basis = "record_time"` |
| Write behavior | Matching edge is always revived | Text/history writes use `revive=False`; classify before write |
| Replacement of text facts | Never | `resolve_text_fact`: Laya `fact_update` + explicit state-change vs correction operations |
| Conflict kinds | — | `duplicate`, `extends`, `unrelated`, `newer_state`, `corrects`, `contradicts` |
| Unknown end | `ended_unknown` field exists, unused for text | Used, with `attested_from` |
| Age | Not shown | Decay class + last confirmed shown in evidence |
| Clocks | `first_seen_at`, `last_confirmed_at` (loose) | Four clocks with strict update rules |

#### 5.0 Writer contract — prerequisite, before temporal classification (M)

Change `graph/writer.py` before enabling any text-fact lifecycle behavior:

1. **`upsert_fact_edges(..., revive: bool = True)`** — keep `True` for existing deterministic current-state callers. Text facts and historical/backfill ingestion pass `revive=False`. On a match with `invalid_at IS NOT NULL`, `revive=False` may attach new provenance/confirmation metadata but must not clear `invalid_at` or change the closed world interval. Define `confirm` separately enough that it cannot accidentally reopen history.
2. **`close_fact(fact_uid, valid_to, reason="superseded")`** — target exactly one live fact, archive its pre-change state into `FactHistory`, then set its world-time `invalid_at = valid_to`. Reject a missing/ambiguous `fact_uid` and an invalid interval. This is for `newer_state`, where the old fact was true until the replacement became valid.
3. **`correct_fact(fact_uid, corrected_by, observed_to)`** — archive the assertion, set `assertion_status="corrected"`, `corrected_by`, `correction_observed_at=observed_to`, and close the record-time belief interval in `FactHistory`. A corrected assertion is excluded from every "what was true" world-time query, including dates before the correction, because it was never true. It remains available to "what did we believe as of" record-time queries until `observed_to`.
4. **Classification before mutation** — find candidates and decide `duplicate|extends|unrelated|newer_state|corrects|contradicts` before inserting or updating the incoming edge. Only then call `upsert_fact_edges`, `close_fact`, `correct_fact`, or create a review proposal.
5. **Attestation fields** — `attested_from` remains an ISO timestamp consumed by `time_axis.py`. Store provenance separately as `attested_by_record`. Never put a record key in a date field.
6. **Read contract** — ordinary world-time readers require `assertion_status != "corrected"` and `projection_status = "live"`. Record-time history may return a corrected assertion only inside its archived `observed_from ≤ as_of < observed_to` interval. Apply these predicates centrally so chat, entity detail, history and graph views cannot drift.

`newer_state` and `corrects` must remain distinct in storage and reads:

| Kind | World axis | Record axis |
|---|---|---|
| `newer_state` | old fact valid until `new.valid_at`; `close_fact(old, new.valid_at)` | both observations retained normally |
| `corrects` | old assertion marked corrected and never returned as true | old belief visible only until `correction_observed_at` |

#### 5.1 Stated vs record time — `semantic_pass.py` → `_write_extraction` (M)

- New helper `graph/dates.py` → `stated_dates(evidence, reference_time)`: explicit ISO / `12 March 2026` / `Mar 2026` via regex; relative (`last week`, `from next sprint`, `since Q2`) via `dateparser` with `RELATIVE_BASE = source_time`. No LLM (Graphiti spends an LLM call per edge here).
- If a start date is stated: `valid_at = stated`, `valid_at_basis = "stated"`.
- Else: keep `valid_at = source_time` (so existing `holds_at` / `held_at` filters keep working) and set `valid_at_basis = "record_time"`.
- If an end is stated ("until the migration", "was replaced on 4 Aug"): set `invalid_at` only when a date resolves; otherwise `ended_unknown = true`.
- Chat prompt addition in `graph/chat.py` → `SYSTEM_PROMPT`: facts with `record_time` basis are described as "recorded in <source> on <date>", not "true since <date>".

Why not change `valid_at` semantics outright: `graph/time_axis.py` and every temporal query read `valid_at`. A basis flag is additive and safe; a semantic change needs a migration.

#### 5.2 `resolve_text_fact` — classify before write in `graph/writer.py` (L)

Candidates (blocking — not a graph-wide search like Graphiti):

```cypher
MATCH (s)-[r:<same relation>]->(o {uid: $object_uid})
WHERE r.invalid_at IS NULL AND r.fact_uid <> $new_fact_uid
RETURN r, s
UNION
MATCH (s {uid: $subject_uid})-[r:<same relation>]->(o)
WHERE r.invalid_at IS NULL AND r.fact_uid <> $new_fact_uid
  AND <relation is single-valued per axioms>
RETURN r, o
```

For Decisions, also include live Decisions that `APPLIES_TO` the same target.

Decision (Laya `fact_update` on each candidate, state = `{"existing_fact", "existing_valid_from", "new_evidence", "new_timestamp"}` as in the Laya schema) is computed before the incoming edge is mutated:

```python
def resolve_text_fact(new, old, kind):
    if kind == "duplicate":
        confirm(old, at=new.source_time, revive=False)  # provenance/reinforcement; never reopen
        return
    if kind in ("extends", "unrelated"):
        return
    if old.pinned:                                  # DICE: pinned facts are never demoted
        link_disputed(old, new); return
    if kind == "contradicts":
        link_disputed(old, new)                     # both live, DISPUTED_WITH
        return
    if kind == "corrects":
        correct_fact(old.fact_uid, corrected_by=new.fact_uid,
                     observed_to=new.source_time)   # old was never world truth
        return
    # newer_state (Graphiti date rule)
    if old.valid_at and new.valid_at:
        if windows_disjoint(old, new):
            return
        if old.valid_at < new.valid_at:
            close_fact(old.fact_uid, valid_to=new.valid_at, reason="superseded")
        else:
            # Incoming historical fact is written closed and must not revive a matching edge.
            new.invalid_at = old.valid_at
    else:
        mark(old, ended_unknown=True, attested_from=new.source_time,
             attested_by_record=new.source_record_key)  # never put a key in a date field
```

- `close_fact()` and `correct_fact()` reuse the same archival primitives as API supersession but target one `fact_uid` and accept explicit timestamps. Do not call subject-wide `supersede_fact_edges` for text facts.
- `NEURON_FACT_UPDATE_MODE=suggest` (default): `duplicate` may safely reinforce the old fact; `extends`/`unrelated` may write normally with `revive=False`; `newer_state`, `corrects` and `contradicts` create a review proposal and keep the incoming assertion `pending_review`, excluded from live world-time answers. Approval atomically applies `close_fact`/`correct_fact`/`DISPUTED_WITH` and activates the incoming assertion. Rejection preserves an audit row but does not project the candidate as live.
- `auto` is allowed only after review precision meets §10.4. The writer operation and incoming activation must share one transaction or compensating ledger state so a partial failure cannot leave both assertions live accidentally.
- Conflicts involving a polarity veto from Phase 4.1 go straight to this function with `kind` from Laya.

#### 5.3 Laya `fact_update` split (M, training)

Current options: `duplicate`, `updates`, `contradicts`, `extends`, `unrelated`. Split `updates` into:
- `newer_state` — both were true at different times (DICE "world progression"): "Aashish owned it, now Priya owns it";
- `corrects` — the old statement was wrong (DICE "revision"): "the limit is 100, not 1,000 as stated earlier".

Retrain with ≥ 3 phrasings per pattern (lesson from the Laya writeup), real pairs from the Phase 5.2 review queue as they accumulate.

#### 5.4 `DISPUTED_WITH` — ontology axioms (S)

Add as a **structural** relation (symmetric, not assertable by the LLM). Written only by `resolve_text_fact` and Phase 6.3. Chat evidence shows both sides with their sources; the existing prompt rule on source authority decides which to prefer.

#### 5.5 Freshness policy (M)

At write time, store `decay_class` on the fact edge from the chunk's Laya `chunk_type` (Phase 3.1 already computes it):

| `chunk_type` | `decay_class` | Considered fresh for |
|---|---|---|
| `status_update` | `fast` | 21 days |
| `action_item` | `task` | until the linked WorkItem is closed |
| `decision` | `slow` | 180 days, unless replaced |
| `fact_statement` | `durable` | 365 days |
| other | `slow` | 180 days |

Nothing is hidden or deleted. The evidence line shows `last confirmed 2026-05-12 · status_update · may be outdated`. Prompt rule: if a fact is past its freshness window, say it was confirmed on that date and that current validity is not confirmed.

Hysteresis (DICE): a fact becomes `stale` at 1.0× its window and returns to `fresh` only when reconfirmed (a `duplicate` from new evidence), never by time.

#### 5.6 Four clocks (S)

| Field | Updated when | Never updated when |
|---|---|---|
| `first_seen_at` | first write | — |
| `last_confirmed_at` | new evidence restates the fact (`duplicate`) or a re-sync with changed content re-asserts it | KEEP syncs, re-scoring, status or flag changes |
| `metadata_revised_at` (new) | status, pin, dispute, review decisions | — |
| `last_retrieved_at` (new, on node) | node reaches the final answer set | — |

Check `upsert_fact_edges` `ON MATCH` today sets `last_confirmed_at`; restrict it to content-changing writes.

#### 5.7 Reinforcement (S)

`reinforce_count` = `size(r.source_record_keys)` (distinct supporting records). Used as a small tie-break boost in rerank (Phase 2) and shown in evidence (`seen in 3 records`).

#### 5.8 Effective confidence and pinning (S)

Stored `confidence` never changes. At read time: `effective = confidence × freshness_factor` where `freshness_factor` is 1.0 when fresh, 0.7 when stale. `pinned: bool` on fact edges and nodes exempts from staleness and from demotion in 5.2. Pinning is a review-UI action.

#### 5.9 Temporal questions in the golden set (S)

Add 30–50 questions built from `FactHistory` rows and from closed text facts: "who owned X in March", "what was decided about Y before the August change". Reuse the Phase 0 generator with a temporal template.

**Tests:** `revive=True/False`; a historical write cannot reopen a closed edge; `close_fact` affects exactly one fact and uses the supplied world time; `correct_fact` disappears from all world-time reads but remains in record-time history until `observed_to`; `attested_from` always parses as ISO and record provenance uses `attested_by_record`; classification happens before mutation; `stated_dates`; every conflict kind including out-of-order/missing dates; pinned facts; suggest mode; KEEP does not bump confirmation.
**Exit criteria:** temporal questions measured; number of subject pairs with two contradicting live text facts reported and falling; no regression on non-temporal questions.
**Risks:** Laya `fact_update` weak (synthetic-only) → suggest mode + review data; `dateparser` false positives → only accept dates inside the evidence span.
**Effort:** L.

---

### Phase 6 — Hygiene job and review queue

**Goal:** measure and repair graph health continuously.
**Fixes:** P9, P3 (remaining).

New module `graph/hygiene.py`, run after each sync and nightly; every mutating step has `dry_run=True` by default.

#### 6.1 Isolated-node report (S)

Per sync, count and list:
- `Document` with no `DOCUMENTS` edge;
- `Decision` with no `APPLIES_TO`;
- `Commit` with no `IMPLEMENTS`;
- `Term` / `System` with degree 1 (only `EXTRACTED_FROM`).

Store counts in `hygiene_runs`; show trend in the dashboard.

#### 6.2 Candidate links for isolated nodes (M)

Two candidate sources, both producing *candidates*, not edges:
- **two-hop** (DICE): pairs (A, B) with no edge but a shared non-hub neighbour Z (Term, System, Decision); exclude labels in `HUB_LABELS` and any Z with degree > 50;
- **embedding**: for each isolated Document/Decision, WorkItems/PRs above a similarity threshold (`search_above`, ceiling 10).

Each candidate → Laya `relation_type` with state `{"text": best_window of A's text, "head": A.name, "tail": B.name}`. If relation ≠ `none` and confidence ≥ 0.6 → write to `link_candidates` with `derived_rule = "two_hop" | "semantic_candidate"`, `confidence = 0.5` (DICE neutral), review state `pending`. Approved → real edge with `derived: true`, `extraction_method: "derived"`, provenance = both nodes' records.

Also move `derived.py` → `_shared_concept_documents` onto this path: produce candidates, not direct edges.

#### 6.3 Cardinality and contradiction audit (S)

From the axioms, list relations declared single-valued. Report any node with two live edges of such a relation (should not happen; a hit is a bug or an unresolved conflict). Report open `DISPUTED_WITH` pairs.

#### 6.4 Multi-signal duplicate collector (M)

For Decision and Term, candidate pairs from `search_above(sim ≥ 0.8)`; score:

| Signal | Weight |
|---|---|
| vector similarity | 0.4 |
| lexical overlap of names (Jaccard) | 0.2 |
| same `APPLIES_TO` / `DEFINES` targets (Jaccard) | 0.2 |
| shared `source_record_keys` | 0.2 |
| polarity veto | veto |

Pairs ≥ 0.6 become independent review proposals. Do **not** merge connected components transitively: A≈B and B≈C does not prove A≈C. An approved pairwise merge chooses a survivor (most reinforced, then oldest), absorbs edges and `source_record_keys`, and records `merged_into` plus a trace row. Recompute remaining candidates after every approved merge. Decisions always require review; approved System/Term pairs may later feed the namespace-aware alias table (4.8).

#### 6.5 Review queue UI — extend the Phase 3 queue (M)

Add `demo_ui/frontend/src/components/BridgePanel.tsx` over the existing Phase 3 SQLite queue/API. One queue, typed items:

| Type | From | Approve does |
|---|---|---|
| `possibly_same_as` | 4.2 | merge + namespace-aware alias; Decision requires explicit review |
| `fact_update` | 5.2 | apply proposed close / dispute |
| `link_candidate` | 6.2 | write derived edge |
| `duplicate_pair` | 6.4 | merge that approved pair, then recompute candidates |

Endpoints from the original `plan.md` §6a.1 (`GET /api/reviews`, `POST /api/reviews/{id}/approve|reject`). Rejections are cached so the same pair is not proposed again (flag, not delete — Utopia lesson).

#### 6.6 Health dashboard (S)

Isolated ratio per label, open disputes, cardinality violations, review queue size by type, resolution stats (4.6), plus the latest eval row (§11). This is the page to show when justifying cost.

**Exit criteria:** isolated Documents ratio falling sync over sync; zero cardinality violations; review queue has items of every type; every live merge has a trace.
**Effort:** L overall.

---

### Phase 7 — After 0–6 are measured

Only start an item here when the eval shows the gap it closes.

#### 7.1 Path-support check (M) — CoEvoKG

After the answer, take the cited blocks (`used_sources`) and score each adjacent pair: 1.0 if an edge connects them, 0.7 if one's name appears in the other's text, 0.5 if both appear in one `SourceRecord`, via one intermediate node at 0.8× the weaker hop, else ε. Path score = geometric mean. Below `NEURON_PATH_MIN=0.4`, mark the answer "low support" in the UI.

#### 7.2 Path validation for Laya-recovered hops (M)

Phase 2 already performs the bounded multi-hop retry. If evaluation shows false bridge recoveries, require each recovered answer candidate to have path support ≥ 0.4 with its direct/temporal/bridge seed before it can reach packing. Keep the Phase 2 round and seed caps; do not add another planner or retrieval round here.

#### 7.3 Verified write-back (S) — CoEvoKG

Only on an explicit thumbs-up and path support ≥ 0.6, for cited node pairs with no edge, create a `link_candidate` with `derived_rule = "query_verified"`. Silence or absence of a correction is not verification. The candidate still goes through the review queue. Expect a small effect (CoEvoKG ablation: +0.6 points, the smallest of its three parts).

#### 7.4 Local query embedding (S)

Serve `text-embedding-3-small`-compatible query embeddings locally only if latency matters. Query and stored vectors must be the same model. DICE measured ~300 ms per hosted query embedding.

#### 7.5 LLM multi-agent retrieval (L) — conditional

Only if the golden set shows multi-hop questions failing after 7.2, and behind a flag, evaluated on the same set.

---

## 9. Data model changes (all phases)

### 9.1 Fact edge properties

| Property | Phase | Values / meaning |
|---|---|---|
| `valid_at_basis` | 5.1 | `stated` \| `record_time` \| `api` (deterministic facts) |
| `decay_class` | 5.5 | `fast` \| `task` \| `slow` \| `durable` |
| `metadata_revised_at` | 5.6 | set on status / pin / dispute / review changes |
| `pinned` | 5.8 | bool, default false |
| `corrected_by` | 5.2 | `fact_uid` of the correcting fact |
| `assertion_status`, `correction_observed_at` | 5.0 | `active` \| `corrected`; separates false assertions from later state changes |
| `projection_status` | 5.2 | `live` \| `pending_review` \| `rejected`; only `live` participates in ordinary world-time answers |
| `ended_unknown`, `attested_from` | 5.2 | `attested_from` is always an ISO timestamp |
| `attested_by_record` | 5.0 | source record key proving the attestation; never parsed as a date |
| `extraction_method` | 5.2 / 6.2 | adds `laya` for model-decided links (value exists in the schema doc; start using it) |
| `derived_rule` | 6.2 / 7.3 | adds `two_hop`, `semantic_candidate`, `query_verified` |
| `merged_into` | 6.4 | survivor uid after a live duplicate merge |

`FactHistory` copies `assertion_status`, `corrected_by`, `correction_observed_at`, `projection_status`, `valid_at_basis`, `attested_from` and `attested_by_record` when an assertion is archived. This preserves the distinction between past world state and past belief.

### 9.2 Node properties

| Property | Phase | Meaning |
|---|---|---|
| `last_retrieved_at` | 5.6 | last time the node reached a final answer set |
| `pinned` | 5.8 | exempt from staleness |
| `resolved_by` | 4.6 | how the node was first resolved (diagnostic) |
| `namespace_uid` | 4.0 | project or workspace/connection scope for System and Term identity |

### 9.3 Relations

| Relation | Phase | Kind |
|---|---|---|
| `POSSIBLY_SAME_AS` | 4.2 | structural, symmetric, review-only |
| `DISPUTED_WITH` | 5.4 | structural, symmetric, written by `resolve_text_fact` and hygiene |

Both are added to the axioms as non-assertable by the LLM.

### 9.4 Ledger (SQLite) tables / columns

| Table / column | Phase | Purpose |
|---|---|---|
| `source_chunks.triage_type`, `triage_durable_p`, `triage_model` | 3.1 | Laya triage result |
| `reviews(id, type, payload, state, decided_by, decided_at, created_at)` | 3.0 | minimal shared review queue |
| `DropReason.LAYA_TRIAGE_SKIP`, `GENERIC_MENTION` | 3.2, 4.5 | new drop reasons |
| `entity_aliases(label, namespace_uid, alias_norm, uid, source)` | 4.8 | namespace-aware alias rung |
| `mention_stoplist(label, term)` | 4.5 | editable stoplist |
| `resolution_stats(run_id, label, resolved_by, count)` | 4.6 | diagnostics |
| `fact_update_proposals(...)` | 5.2 | suggest-mode actions |
| `link_candidates(...)` | 6.2 | candidate links with review state |
| `collector_trace(...)` | 6.4 | per-decision merge audit |
| `hygiene_runs(...)` | 6.1 | isolated / dispute / violation counts |
| `sync_coverage(...)` | 0.6 | provider vs ledger counts |

### 9.5 Migration notes

`valid_at` keeps its current semantics (§8 Phase 5.1 explains why). Backfill `valid_at_basis = "api"` for deterministic edges and `"record_time"` for existing LLM edges. Backfill `assertion_status = "active"`; world-time readers must explicitly exclude `corrected` assertions while record-time history may still include them.

Scoped identity is a real migration, not just an optional field. Start with a dry-run mapping from each existing System/Term to a project or workspace namespace using its source records. Create new scoped nodes and rewire provenance/fact edges in a traced migration; do not mutate UIDs in place. Put ambiguous mappings and every cross-record Decision merge into the Phase 3 review queue. Preserve old UIDs as aliases/redirects until graph edges, ledger references and Qdrant points have been rebuilt and verified.

---

## 10. Laya plan

### 10.1 Where Laya runs in Neuron

| Question | Type | Phase | Call site | Mode at start |
|---|---|---|---|---|
| `retrieval_relevance` | noul | 2 | `graph/rerank.py` ← `chat.retrieve` | measured shadow, then conditional default |
| `chunk_type` | choice | 3, 5.5 | `semantic_pass.run_semantic_pass` | shadow |
| `has_durable_fact` | noul | 3 | same | shadow |
| `same_entity` | noul | 4 | `semantic_pass._write_extraction` | suggest |
| `fact_update` | choice | 5 | `writer.resolve_text_fact` | suggest |
| `relation_type` | choice | 6 | `hygiene` candidate links | candidate only |
| `entity_type` | choice | 4.5 | mention filter (optional) | log only |

Not used by Neuron: `importance`, `contains_pii` (possible later for ACL/redaction), knowledge/wisdom composite scores from the Laya repo.

### 10.2 Real training data, per question

| Question | Today | Real data source in this plan |
|---|---|---|
| `retrieval_relevance` | synthetic (0.90 synthetic val) | evidence-labelled Phase 0 dev pairs + logged scores; unlabelled non-chain candidates are not negatives |
| `chunk_type`, `has_durable_fact` | Nilus chunks + synthetic | Phase 3.1 shadow: did the LLM extract a fact from this chunk |
| `same_entity` | synthetic only | Phase 4 review decisions on `POSSIBLY_SAME_AS` |
| `fact_update` | synthetic only | Phase 5 review decisions; new `newer_state` / `corrects` split |
| `relation_type` | Nilus edges | Phase 6 link-candidate reviews |

Hold out a **hand-labelled test set of 200–300 decisions** across these questions that never enters training. Report accuracy and calibration (ECE) per question on it.

### 10.3 Serving

- One process-level `laya.Agent`, loaded lazily; device from `LAYA_DEVICE` (`cuda` / `mps` / `cpu`).
- Model path from `LAYA_MODEL_DIR` (currently `laya/model/laya-ingest`).
- Timeout per call and a fallback that never blocks chat or ingest.
- Per-question temperature (the Laya writeup notes one shared temperature for all yes/no questions softens the others) — apply per-question calibration after inference, fitted on the hand-labelled set.
- Use the public `predict_batch` API with stable input/output order; verify scores against single-item inference before promotion.

### 10.4 Promotion rule (suggest → auto)

A Laya decision type moves from suggest to auto only when, on ≥ 200 reviewed items, precision of the accepted class ≥ 0.95 at the chosen threshold, and the ECE on the hand-labelled set ≤ 0.05. Decision entity merges are permanently excluded from auto-promotion.

---

## 11. Metrics

| Metric | Definition | Moved by |
|---|---|---|
| Candidate recall@40 | share of questions whose `answer_uid` is in the pool before rerank | 1.1, 1.3, 1.7 |
| Final recall | share whose `answer_uid` reaches the answer prompt | 2 |
| Post-packing evidence recall | share where a labelled supporting span/fact survives the final token pack | 1.2, 2 |
| Rerank gap | candidate recall − final recall | 2.4 |
| Bridge recovery rate | questions missed on pass one whose labelled evidence is accepted after a bounded bridge hop | 2.2 |
| MRR | mean reciprocal rank of `answer_uid` in the final list | 2 |
| Chain coverage | mean share of chain nodes in the final list | 1.3, 2 |
| Hidden-edge recall | recall of both chain endpoints when one chain edge is hidden | 1.3, 6.2 |
| Answer accuracy | LLM-judged vs `answer_uid`, with 30-question human spot check | all |
| Abstention accuracy | correct refusal/clarification on unanswerable or ambiguous questions | all |
| Tokens / $ per question | from `TokenUsage` | 1.2, 1.6 |
| p50/p90 latency | scorer and end-to-end chat, on deployment-class CPU and separately on GPU | 2.3 |
| LLM calls / $ per sync | ingest | 3 |
| Lost-fact rate | facts from would-be-skipped chunks ÷ all facts (shadow) | 3.1 |
| Duplicate rate | duplicates in a 100-pair labelled sample | 4, 6.4 |
| Wrong-merge count | merges later rejected in review | 4 |
| Live contradictions | subject pairs with two contradicting live text facts | 5 |
| Temporal accuracy | accuracy on temporal golden questions | 5 |
| Isolated ratio | isolated nodes ÷ nodes, per label | 6 |

Results go to `eval/results.md`, one row per run: date, git sha, flags, dev and test values. Thresholds are tuned on dev only.

---

## 12. Config flags

| Flag | Default | Phase | Meaning |
|---|---|---|---|
| `NEURON_POOL_SIZE` | 40 | 1.1 | candidates before the cut |
| `NEURON_BLOCK_TOKENS` | 500 | 1.2 | token window per evidence block |
| `NEURON_FACTS_PER_BLOCK` | 25 | 1.2 | facts per block |
| `NEURON_CONTEXT_TOKENS` | 10000 | 1.2 | total evidence-token budget |
| `NEURON_EXPAND` | on | 1.3 | 1-hop expansion |
| `NEURON_EXPAND_SEEDS` / `_PER_SEED` | 8 / 4 | 1.3 | expansion caps |
| `CHAT_MODEL` | `gpt-5.6-sol` | 1.6 | existing; A/B with luna |
| `NEURON_RERANK` | off | 2 | `off` \| `laya` |
| `NEURON_RERANK_MIN_P` | 0.5 | 2 | direct-evidence probability cut (tune on dev) |
| `NEURON_RERANK_BRIDGE_MIN_P` / `_BRIDGE_LIMIT` | 0.2 / 4 | 2 | bounded bridge seed policy |
| `NEURON_RERANK_MIN_DIRECT` / `_MAX_KEEP` | 2 / 12 | 2 | retry trigger and accepted-evidence cap |
| `NEURON_RERANK_MAX_ROUNDS` | 2 | 2 | maximum bounded expansion/rescore rounds |
| `NEURON_RERANK_FACTS` | 6 | 2 | ACL-filtered linked/temporal facts in each scorer window |
| `NEURON_RERANK_TIMEOUT_S` | 5 | 2.3 | fallback trigger once serving moves to an interruptible worker |
| `LAYA_MODEL_DIR`, `LAYA_DEVICE` | — / cpu | 2–6 | serving |
| `NEURON_TRIAGE` | shadow | 3 | `off` \| `shadow` \| `enforce` |
| `NEURON_RESOLVE_MODE` | suggest | 4 | `suggest` \| `auto` |
| `NEURON_RESOLVE_GRAY_MIN` | 0.75 | 4 | gray-zone floor |
| `NEURON_FACT_UPDATE_MODE` | suggest | 5 | `suggest` \| `auto` |
| `NEURON_HYGIENE_DRY_RUN` | true | 6 | mutating hygiene steps |
| `NEURON_PATH_MIN` | 0.4 | 7 | low-support flag |

Existing flags unchanged: `LLM_MODEL`, `LLM_BUDGET_PER_RUN`, `LLM_CONCURRENCY`, `EMBEDDING_MODEL`.

---

## 13. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Chain-generated questions are easier than real questions | medium | mixed frozen set; report real, ambiguous, unanswerable and chain slices separately |
| Laya relevance underperforms RRF on real Neuron data | medium | controlled A/B/C trial; keep Laya off unless it earns promotion |
| CPU latency makes rerank unusable | high on CPU | benchmark batching on deployment hardware; RRF fallback |
| Triage drops real knowledge | medium | shadow mode first; lost-fact rate gate; skipped chunks still searchable |
| Source eligibility changes raise Pass B cost | medium | require measured yield, retain the budget cap, and report cost per source type |
| Wrong entity merges | low (with plan) | namespace identity, source-scoped Decisions, polarity veto, exactly-one, review-only Decision merges |
| `fact_update` closes valid facts | medium | suggest mode; pinning; FactHistory keeps everything |
| Historical ingest revives a closed fact | medium without writer change | `revive=False`, classification before write, targeted `close_fact` tests |
| Correction appears as past truth | medium without explicit status | `assertion_status=corrected`; world reads exclude it while record history retains prior belief |
| Date parsing invents dates | low | only dates found inside the evidence span; basis flag |
| Expansion floods candidates with hub noise | medium | typed rels only, hub labels excluded, per-seed cap, rerank |
| Review queue grows faster than people review it | high | promote to auto per §10.4; prioritize by reinforce count |
| Scope creep into new architectures | high (history) | §14 non-goals; every new idea must name the §11 metric it moves |

---

## 14. Non-goals

- Returning to Graphiti or adopting DICE, NeuralMemory or any other framework as a dependency.
- LLM extraction on every file or every chunk.
- Unbounded or open-ended graph expansion at query time.
- Running more than one Laya question per candidate at query time.
- Auto-accepting `same_entity` or `fact_update` before §10.4 is met.
- Auto-merging Decisions across records; those merges always require review.
- Hard deletion of stale facts.
- RL training of any model; fine-tuning a generator (`llmtoslm`).
- LLM multi-agent retrieval before Phase 7.2 is measured.
- Further work on `brain` (NeuralMemory) or the standalone Laya graph.

---

## 15. Open questions

1. **Retrieval promotion:** Phase 2 decides whether the Laya two-pass path beats the RRF fallback within the deployment latency target. GPU availability is an input, not an assumption.
2. **Chat model:** sol vs luna after context bounding (Phase 1.6 answers it).
3. **Who reviews?** The queue needs an owner and a weekly slot, or suggest mode never promotes.
4. **Source-type yield:** measured durable-fact yield and cost for Jira comments, PR descriptions, commit messages and SourceFiles determine any Phase 3.3 eligibility change.
5. **Freshness windows** in 5.5 are starting values; confirm with the team what "stale" means for status updates vs decisions.
6. **Coverage fixes** (Notion search incompleteness, Bitbucket file types, commit depth) are reported in Phase 0.6 but not fixed here; decide after seeing the numbers.

---

## 16. Appendix

### 16.1 File map

| File | Phases | Change |
|---|---|---|
| `scripts/build_chain_golden.py` (new) | 0.3, 5.9 | chain sampling, question generation, leakage filter |
| eval harness (calls `retrieve` + evidence packer) | 0.1–0.5 | refreshed baselines, grouped mixed set, stage/packing metrics, hidden-edge runs |
| `demo_ui/backend/*_routes.py` | 0.6 | coverage counts |
| `graph/chat.py` | 1.1, 1.2, 1.5, 1.7, 2.2, 5.1, 5.5 | pool, budget, lanes, rerank wiring, prompt rules |
| `graph/text_window.py` (new) | 1.2, 2.1, 4.2, 6.2 | `best_window` |
| `graph/expand.py` (new) | 1.3, 1.4 | expansion, tiers |
| `graph/rerank.py` (new) | 2 | Laya adapter, relevance roles and score logging |
| `graph/semantic_pass.py` | 3.1–3.4, 4.0–4.6, 5.1, 5.5 | measured triage, scoped identity, ladder, dates, decay class |
| `connectors/core/ledger.py` | 3.0–3.2, 4.5–4.8, 5.2, 6.x | minimal review queue, drop reasons, aliases and audit tables |
| `graph/jira_pipeline.py`, `graph/bitbucket_pipeline.py` | 3.3 | source eligibility changes only where measurements justify them |
| `graph/vector_store.py` | 4.2, 6.2, 6.4 | namespace-aware `search_above` (threshold + ceiling) |
| `graph/writer.py` | 5.0, 5.2, 5.6 | `revive`, classification-before-write, `close_fact`, `correct_fact`, clock rules |
| `graph/dates.py` (new) | 5.1 | `stated_dates` |
| axioms (`graph/axioms.py` + ledger) | 4.2, 5.4 | `POSSIBLY_SAME_AS`, `DISPUTED_WITH` |
| `graph/derived.py` | 6.2 | shared-concept → candidates |
| `graph/hygiene.py` (new) | 6 | report, candidates, audit, collector |
| review CLI or small backend route | 3.0 | minimal review operations |
| `demo_ui/frontend/src/components/BridgePanel.tsx`, `demo_ui/backend/bridge_routes.py` | 6.5 | UI over the existing review queue |
| dashboard component (new or in `SyncProgress.tsx`) | 0.6, 6.6 | health + eval numbers |

### 16.2 Order of work

```
Phase 0  ── refresh Nilus + less_token baselines · mixed grouped eval · packing metrics
   │
Phase 1  ── token budget · pool 40 · typed expansion · tiers · pair lane · luna A/B
   │
Phase 2  ── all applicable lanes · Laya roles · bounded bridge expansion · RRF fallback
   │
Phase 3  ── minimal review queue · triage shadow → conditional enforce · measured source eligibility
   │
Phase 4  ── scoped identity · Decision review-only merge · ladder · stoplist · stats · aliases
   │
Phase 5  ── writer contract · classify-before-write · state vs correction · dates · freshness · clocks
   │
Phase 6  ── isolated diagnostics · candidate links · pairwise collector · review UI · dashboard
   │
Phase 7  ── path support · recovered-hop validation · write-back · (local embedding) · (multi-agent, conditional)
```

Phases 0–2 are the first milestone and choose the retrieval architecture from measurements. Phase 3 installs the minimal review foundation before entity or temporal suggestions. Phases 4–6 carry identity, lifecycle and graph-health work. Phase 7 waits for measured gaps.

### 16.3 One-line summary per source

| Source | One thing we take |
|---|---|
| Neuron v1 | everything — this plan extends it |
| Utopia | already taken (15 Sep) |
| Laya | calibrated decisions at four call sites |
| Pasted blocking plan | threshold, not rank; margin rule |
| GraphImmune | measure isolated nodes; repairs behind review |
| DICE | escalating resolver, polarity veto, four clocks, three conflict kinds, gates, collector |
| Graphiti | date rule for invalidation, out-of-order backfill |
| CoEvoKG | golden set from graph chains; path support |
| NeuralMemory | bounded retry when first-pass evidence is insufficient |
| llmtoslm | parked |
