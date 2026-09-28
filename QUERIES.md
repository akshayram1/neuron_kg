# Open queries — 25-plan.md execution

Verified against the repository on 27 Sep 2026. This file now separates real
blockers from tuning decisions and completed items; stale implementation
blockers have been removed.

## Blocking external/data issues

### Local integration service port

The active Neuron FalkorDB container is healthy on host port `6380`, while
the checked-in local `.env` still says `FALKOR_PORT=6379`. Integration tests
therefore need `FALKOR_PORT=6380` in this environment (or the local `.env`
must be aligned deliberately). This is an environment mismatch, not a graph
or writer failure; no database was restarted or modified to diagnose it.

### Phase 0 — trustworthy baselines and mixed evaluation set

The evaluation harness is working, but the local data does not match the
golden sets:

- `neuron__nilus` contains 232 nodes using Laya's foreign
  `Entity`/`Source`/`Context` schema. The Nilus golden header expects 3,833
  Neuron-schema nodes. Laya's scripts use the same FalkorDB port and graph
  name, which explains the collision.
- `neuron__less_token` has zero FalkorDB nodes while its Qdrant collection
  has 425 orphaned vectors.
- the default graph is empty/under-populated compared with its recorded
  benchmark corpus.

No graph or collection was cleared, restored or overwritten. We still need
the authoritative ingestion source, backup, or intended FalkorDB instance
for `nilus` and `less_token`. Any repair must be scoped to the named graph
and its vector collection.

This blocks trustworthy Phase 0 baseline numbers, the 200–300 question frozen
mixed set, threshold calibration, and promotion decisions. Existing
regressions plus 44 chain questions are available, but ambiguous,
unanswerable and real-user slices cannot be labelled honestly against the
wrong graph.

### Phase 0.6 — two sync coverage counters are still partial

Bitbucket/GitHub `skipped_by_rule_count` includes files that are too large or
have no text. It does not include files rejected by the `.py`/`.md` extension
filter or the exact number of commits beyond the configured cap. The API
clients currently stop fetching at the cap, so an exact beyond-cap count
would require extra provider pagination or a provider-reported total that
these endpoints do not expose. Keep the current count labelled as partial.

## Measurement and product decisions

### Laya promotion remains measurement-gated

The package and checkpoint are real and wired for `retrieval_relevance`,
`chunk_type`, `has_durable_fact`, `same_entity`, `fact_update` and
`relation_type`. Code availability is no longer a blocker. These promotions
still require the plan's held-out evidence:

- reranking default-on threshold and deployment p50/p90;
- triage shadow lost-fact rate before `enforce` becomes a production default;
- entity/fact-update auto mode only after reviewed precision >= 0.95 and ECE
  <= 0.05; Decision merges always remain review-only.

Until those numbers exist, the conservative modes remain correct:
`NEURON_RERANK=off`, `NEURON_TRIAGE=shadow`,
`NEURON_RESOLVE_MODE=suggest`, `NEURON_FACT_UPDATE_MODE=suggest`.

### Existing-data migration and vector rebuild

New writes persist `namespace_uid` in graph nodes and vector payloads, and
Postgres/Qdrant filters now have matching write-side data. Existing vectors
predating this change do not gain it automatically. After the authoritative
graphs are restored, run the scoped identity migration described in
25-plan.md §9.5 and rebuild only that graph's vector collection.

### Date parsing scope

`stated_dates` is intentionally regex-based, English-only, and treats a
sprint as 14 days. There is no project sprint-length source in the current
schema. Adding a general date parser or per-project sprint configuration is
a product choice, not a code blocker.

### Review identity/authentication

Review identities remain caller-supplied opaque strings, and `decided_by`
is an unauthenticated string because the demo app has no user identity
system. This is acceptable for the current internal/demo deployment; a
multi-user deployment needs authentication and authorization before review
decisions are treated as accountable audit records.

### Candidate-link and merge tuning

These conservative defaults need production samples before changing:

- semantic link-candidate threshold `0.65`;
- only unambiguous Laya relations map into Neuron (`OWNS`, `PARENT_OF`,
  `REFERENCES`, `BLOCKS`);
- approved link provenance is the union of both nodes' source records;
- duplicate survivor reinforcement is the sum across live incident fact
  edges.

### Phase 7 conditional work

Local query embeddings (7.4) should start only if hosted query-embedding
latency is material. LLM multi-agent retrieval (7.5) should start only if
the restored mixed evaluation still shows multi-hop failures after recovered
hop validation. Neither is currently justified by valid measurements.

## Resolved in code

- Generic cross-encoder removed by explicit decision; Laya plus RRF fallback
  is the supported reranking architecture.
- Laya is a pinned Neuron dependency and the real checkpoint adapter works.
- Phase 3 triage scores are stored in the ledger with measured would-skip and
  lost-fact yield; enforce mode records `LAYA_TRIAGE_SKIP`.
- Namespace-aware vector writes and rebuild data are wired.
- Laya gray-zone `same_entity` uses p >= 0.85 and margin >= 0.15, then creates
  a review by default.
- `possibly_same_as` approval executes a pairwise merge and writes a scoped
  alias; it no longer fails as a placeholder.
- `fact_update` is real and semantic ingestion classifies before writing.
- `upsert_fact_edges` persists `valid_at_basis`/`invalid_at`; the semantic
  stopgap query is gone and text facts use `revive=False`.
- Chat, expansion, structured queries, inference, history, graph view,
  derived rules and candidate/path checks use the centralized live-fact
  predicate, excluding corrected and pending-review facts.
- Shared-concept candidate generation receives the real ledger from both
  production callers.
- Phase 6 review UI, health dashboard and scheduled hygiene execution are
  implemented.
- Phase 7.1–7.3 path support, recovered-hop rejection and explicit verified
  write-back are implemented.

## New: review precision/ECE measurement script (25 Sep, run 3)

`scripts/measure_review_precision.py` built and merged (§10.4's ">= 0.95 precision, <= 0.05 ECE" promotion bar). Key finding, verified by reading code not guessed: **`fact_update` reviews can't have ECE computed today** — `classify_fact_update` already computes Laya's confidence (`choice, _confidence = ...`) but discards it (leading underscore, never returned, never reaches the review payload). Precision-only for that type until a 3-line fix (return the confidence, thread it through `_resolve_conflict`, add it to the payload). `possibly_same_as` genuinely has a usable confidence field (`top_p` from `same_entity`) and can reach a real PASS verdict. `duplicate_pair`'s score is a hand-weighted heuristic, not a Laya probability — shown but permanently capped at `ECE_NOT_COMPUTABLE`. `link_candidate` excluded entirely (separate table, not a `review`, and its stored confidence is a hardcoded 0.5 constant regardless).
**Question:** worth the 3-line fix to `graph/resolve_text_fact.py` now so `fact_update` can be calibration-measured too? Also: sample-size threshold was set to 200 (matching §10.4's literal "on >= 200 reviewed items" text) rather than a lower default — confirm that's the bar you want enforced.
**Assumption made for now:** none — precision-only reporting for `fact_update`/`duplicate_pair`, real ECE only where a genuine probability exists.

## New: Laya serving/latency benchmark, real measured numbers (25 Sep, run 3)

`scripts/benchmark_laya.py` built and merged (§2.3). Real numbers, real checkpoint, no mocking (`LAYA_MODEL_DIR`, real `laya` package):
- Cold start (incl. model load): **17.9s**. Warm p50/p90 at pool 10: **3.0s / 3.3s**. Pool 20: **6.2s / 6.4s** (matches the plan's own "~7.4s" estimate closely). **Pool 40 (the real production `POOL_SIZE`): p50 16.8s, p90 17.8s.** Peak memory 2.5GB.
- Fallback path independently reproduced as real, not just read from code: pointed a real `LayaReranker` at a missing checkpoint, confirmed `graph/chat.py`'s `try/except` caught the `RuntimeError` and `RetrievalTrace.fallback=True` with the real error message attached.
**Bottom line:** synchronous CPU Laya scoring at the real pool size costs ~17.8s p90 — this is the hard number behind why `NEURON_RERANK=off` is still the correct production default until batching/hardware/a smaller model changes it. Not a question, just the measured evidence §10.4/§2.3 asked for.
**Note:** candidates were synthetic-but-realistic text (real `best_window` windowing, synthetic node text) rather than pulled from a live graph, for iteration speed — flagged in case a graph-sourced re-run is wanted for the final go/no-go record.

## Verification

- Python: `503 passed, 169 skipped, 0 failed`.
- FalkorDB/Qdrant integration slice, with `FALKOR_PORT=6380` and isolated
  test graphs: `171 passed, 4 skipped, 0 failed`.
- Frontend: TypeScript + Vite production build passes.
- The four integration skips are Postgres-vector variants whose service
  configuration is not enabled for this test run.
