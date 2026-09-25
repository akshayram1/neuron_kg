# Open queries — 25-plan.md execution

Questions, ambiguities and judgment calls surfaced while implementing the plan,
that need a decision from Akshay before the affected work can be considered
final. Each entry names the phase/section it blocks.

Format:

```
## <phase>.<section> — <short title>
**Raised:** <date>
**Blocks:** <what can't proceed / what was assumed instead>
**Question:** <the actual question>
**Assumption made for now:** <what the agent did so it could keep moving, if anything>
```

---

## 0.6 — Sync coverage: extension-filter and commit-cap skips not counted
**Raised:** 25 Sep 2026
**Blocks:** nothing merged is broken; `skipped_by_rule_count` is just incomplete for Bitbucket/GitHub.
**Question:** The merged sync-coverage report (`connectors/core/ledger.py`, `sync_coverage` table) populates `skipped_by_rule_count` from `files_too_large + files_without_text` for Bitbucket/GitHub. It does **not** count files dropped by the non-`.py`/`.md` extension filter (a silent `continue` inside the tree walk) or commits dropped by the "beyond N" cap — both live in `connectors/bitbucket/api.py` / `connectors/github_app/api.py`, which were out of scope for this task. Do you want those two counters added now (small follow-up touching the API clients), or leave it for whenever those files are touched for another reason?
**Assumption made for now:** shipped with the partial count (real, not fabricated) and documented the gap in code comments rather than guessing at the missing numbers.

**Note (not a query, for the record):** all four providers (Jira, Bitbucket, GitHub, Notion) get `provider_reported_total = None` — none of their list/search APIs return an aggregate total, so `fetched_count` vs `ledger_count` is the reliable comparison, not fetched-vs-reported. See `03c4679`.

---

## 0 (blocks 0.1 exit criteria) — `nilus` and `less_token` graphs in local FalkorDB don't match the golden sets
**Raised:** 25 Sep 2026
**Blocks:** Phase 0.1's "refresh baselines" exit criterion — the numbers it produces for `nilus`/`less_token` are not measuring what the plan/golden sets think they're measuring.
**Question:** verified independently (not just from the chain-golden agent's report):
- `eval/nilus_golden.jsonl` header says the `nilus` graph should have **3,833 nodes / 7,007 edges** (Jira + Bitbucket + Notion, this repo's real ontology: `WorkItem`, `Document`, `Decision`, `DOCUMENTS`, `IMPLEMENTS`, etc.).
- The live FalkorDB graph actually named `neuron__nilus` on this machine (port 6380) has **232 nodes**, all under labels `Entity`/`Source`/`Context` with a single generic `REL` relation type — a completely different schema, not this repo's ontology at all. It looks like leftover data from an unrelated experiment (possibly `personal_exp` work) using the same FalkorDB instance.
- `neuron__less_token` and `neuron__argus` both exist but have **0 nodes** — empty.
- `backups/neuron-pre-sync-20260909-173926.nq` (9 Sep) exists but its manifest doesn't obviously name `nilus`/`less_token` either — I did not attempt to restore from it without checking with you first, since restoring/overwriting graph data is hard to reverse. Per [[feedback_scoped_cleanup]] I also won't touch other graphs while fixing this one.

So the `eval/results.md` baseline numbers the harness agent (Phase 0.1) produces against `nilus` and `less_token` right now will be near-meaningless (near-zero recall) — not because of a retrieval bug, but because the expected data isn't loaded in this environment. **What I need from you:** do you have the real `nilus`/`less_token` ingestion source (a sync job to rerun, a different backup file, or a different FalkorDB instance/port this was meant to point at) so we can reload the real data before trusting Phase 0's baseline? Until then I'll record the harness's numbers as "environment data missing" rather than as real regression baselines, and Phase 1+ comparisons will need to be re-run once real data is loaded.
**Assumption made for now:** none taken beyond recording the discrepancy — no data was modified, no graph was cleared or restored.

**Confirmed independently, and it's wider than nilus/less_token (25 Sep, after merging the harness agent's Phase 0.1/0.5 work):**
- All three real runs (`nilus`, `less_token`, plus a check of the no-flag default graph that `argus_golden.jsonl` targets) hit live FalkorDB/Qdrant/OpenAI for real and scored **0.0000 on every metric** — the harness itself works correctly (real latency, real token counts, real $ figures logged in `eval/results.md`), the zeros are a data problem, not a code bug.
- Default graph `neuron`: 0 nodes in FalkorDB; its Qdrant collection `neuron_entities` has only **32 points** vs `cost.md`'s original **1,009**.
- `neuron__less_token`: 0 nodes in FalkorDB, but Qdrant `neuron_entities__less_token` has **425 points** — vectors exist with no matching graph nodes.
- `neuron__nilus`: 232 nodes of the foreign `Entity`/`Source`/`Context` schema in FalkorDB, and Qdrant `neuron_entities__nilus` has **1,682 points** — also mismatched with what 232 foreign-schema nodes would produce.
- There's also a `neuron_entities__prof` Qdrant collection (8 points) not tied to any golden set I can find — unexplained, flagging in case it's meaningful.

This reads as a shared, partially-corrupted local FalkorDB + Qdrant environment across at least three graphs (default, `nilus`, `less_token`), not one bad graph. I have not touched any data — no clear, no restore, no reload — pending your direction, and any fix, once you tell me what the correct source is, should stay scoped to the one graph/collection pair being fixed per [[feedback_scoped_cleanup]], not a blanket reset.

**Root cause found (25 Sep, resolves the "where did the foreign schema come from" half of this):** `/Users/akshaychame/personal_exp/laya`'s own ingest scripts (`ingest/ingest_to_falkor.py`, `ingest/ingest_raw_to_falkor.py`, `ingest/build_knowledge_layers.py`, `ingest/enrich_fact_graph.py`) all default to `--port 6380` — the same FalkorDB container Neuron uses — and `graph_view/server.py`'s own docstring says *"Interactive viewer for the Laya-built `neuron__nilus` FalkorDB graph."* Laya deliberately wrote its synthetic `Entity`/`Source`/`Context` experiment data into a graph literally named `neuron__nilus` on the shared instance, which collides with (and has overwritten) Neuron's real `nilus` golden-set graph name. Same likely explanation for `neuron__atlas`. This does **not** explain `less_token`/default `neuron` being empty (0 nodes) with orphaned Qdrant vectors — that's a separate gap, still open. **Still need from you:** is it safe to clear/reset just `neuron__nilus` (and `neuron__atlas` if unused by Neuron) so a real Neuron nilus re-ingestion can use that name again, or does Laya still need that data live at that name? Either way this would be a scoped delete of exactly those two graphs, never a blanket clear, per [[feedback_scoped_cleanup]] — and I won't do it without your go-ahead since Laya's own data would be destroyed.

---

## 2.1 — New heavy dependency: sentence-transformers/torch (~1.5GB) for the cross-encoder
**Raised:** 25 Sep 2026
**Blocks:** nothing merged is broken (254 passed/12 skipped, 23/23 pass with the real model downloaded and scored) — flagging for visibility since it's a meaningful environment change, not a code correctness question.
**Question:** `graph/rerank.py` (Phase 2.1) added `sentence-transformers>=6.1.0` via `uv add`, pulling in `torch==2.14.0` and `transformers==5.17.0` as transitive deps — a large addition to this repo's dependency footprint (previously zero ML-framework deps). It pins `cross-encoder/ms-marco-MiniLM-L6-v2` at a specific HF revision (Apache-2.0, ~22M params, ~20ms/pair CPU inference after a ~17s one-time model download). Is this the dependency/model choice you want for the real bake-off (vs. e.g. a smaller ONNX-exported model to avoid the torch dependency entirely, or deferring the pin until Phase 2.3's real latency benchmarking), or should this be revisited before Phase 2.2 wires it into `chat.py`'s live retrieval path?
**Assumption made for now:** shipped as-is; nothing wired into the live retrieval path yet (that's Phase 2.2, not started) so this is inert until then.

## 2.1 — Laya reranker is a stub; real integration needs package + checkpoint decisions
**Raised:** 25 Sep 2026
**Blocks:** the "D" variant (Laya `retrieval_relevance`) of Phase 2's four-way bake-off can't run yet.
**Question:** `LayaReranker.score()` in `graph/rerank.py` raises `NotImplementedError` on purpose rather than guessing at an unverified implementation. What's confirmed from reading `/Users/akshaychame/personal_exp/laya` (read-only): the exact trained question/state schema (`ingest/schema.py`'s `QUESTIONS["retrieval_relevance"]`) and the call shape Laya's own prototype uses (`graph_view/reranker.py`: `laya.Agent(MODEL_DIR, device="cpu").predict_batch(...)`). What's still needed to finish it for real: (1) a decision on whether the `laya` package becomes a Neuron dependency or stays an external/sidecar service, (2) making the trained checkpoint (`personal_exp/laya/model/laya-ingest/`) available to Neuron's runtime, (3) verifying `Agent.__init__`/`predict_batch`'s real signature directly against the `laya` package source (only seen second-hand via one caller so far). How do you want Laya packaged for Neuron — vendored dependency, sidecar service Neuron calls over a local API, or something else?
**Assumption made for now:** none — left as an honest `NotImplementedError` with the evidence documented in the class docstring, rather than shipping a guessed-at "working" implementation.

## 2.1 — `select_final`'s token-budget cap has no data to work with yet
**Raised:** 25 Sep 2026
**Blocks:** nothing yet (Phase 2.4 tuning, not started) — a heads-up for whoever wires Phase 2.2.
**Question:** the plan lists `min_keep`/`max_keep`/token budget together as `select_final`'s hard caps, but `RerankScore` carries no token count, so token-budget enforcement was left out of `select_final` and left to the future `chat.py` caller (which already has candidate text + a real prompt budget from Phase 1.2's packing work). Fine as designed, or do you want `RerankCandidate`/`RerankScore` extended with a token count now so `select_final` can enforce the budget itself?
**Assumption made for now:** threshold → diversity (documented no-op, dedupe-by-uid only — the plan names "diversity" with no concrete algorithm) → min_keep/max_keep, implemented and tested; token budget deferred to the caller.
