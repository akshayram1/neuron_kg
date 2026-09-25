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

## 2.1 — RESOLVED: generic cross-encoder removed, no sentence-transformers/torch dependency
**Raised:** 25 Sep 2026 · **Resolved:** 25 Sep 2026 (same day, explicit instruction from Akshay: "we dont want ms-marco-MiniLM-L6-v2 pinned, batched, scoring correctly this please remove it")
**What changed:** `CrossEncoderReranker`, its pinned model/revision constants, and every test exercising it were removed from `graph/rerank.py`/`tests/test_rerank.py`. `sentence-transformers` (and its transitive `torch`/`transformers` deps, ~1.5GB) was removed via `uv remove sentence-transformers`. `graph/rerank.py`'s common interface (`Reranker` protocol, `RerankCandidate`, `RerankScore`, `select_final`) and the `LayaReranker` stub are unaffected and stay merged. 274 passed/12 skipped after removal (down from 277/15 — exactly the 3 non-gated + 3 integration-gated cross-encoder tests).
**Still open:** Phase 2's variant C ("B + a generic pretrained cross-encoder") now has no implementation to bake off at all. If/when a generic cross-encoder is wanted again, it needs a fresh decision on approach (e.g. a lighter ONNX Runtime path instead of `sentence-transformers`/torch) — not a resumption of the removed one.

---

## 3 — Phase 3.1/3.2 (Laya shadow mode + enforcement) skipped this wave, blocked on Laya packaging
**Raised:** 25 Sep 2026
**Blocks:** §3.1 ("run Laya `chunk_type` + `has_durable_fact` ... store the result") and §3.2 (enforcing a skip rule built from 3.1's output) — both explicitly require calling a real, working Laya model, which this repo has never had wired in (same open question as the `LayaReranker` stub in `graph/rerank.py` §2.1 — see that entry above).
**Question:** same as before — is `laya` becoming a vendored Neuron dependency, a sidecar service, or something else? Until that's answered, 3.1/3.2 can't be built for real without guessing at another fake-working stub.
**Assumption made for now:** this wave only builds §3.0 (review queue foundation, no Laya dependency) and §3.4 (making the *existing* 3 admission gates explicit/logged, with gates 4-6 left as documented placeholders for Phase 4/5 — also no Laya dependency). §3.3 (source eligibility) is explicitly conditional in the plan on measured gaps from the mixed eval set, which doesn't exist yet (still blocked on the Phase 0 data issue above) — skipped for the same reason, not attempted.

---

## Phase 4 — RESOLUTION LADDER LANDED: real behavior changes to communicate
**Raised:** 25 Sep 2026
**Not a question — a heads-up.** The full §4.0–§4.8 entity-resolution ladder is merged (`graph/semantic_pass.py`). Three real, deliberate behavior changes going forward, all plan-mandated, none migrated against existing graph data:
1. **System/Term/Decision get new uid schemes.** System/Term are now namespace-scoped (`make_uid(label, namespace_uid, normalized_name)`); Decision is now record+statement-scoped (`make_uid("Decision", record_key, normalized_statement)`), not name-scoped. Re-ingesting old content will MERGE onto a *different* uid than before for these three labels — existing nodes minted under the old global-name scheme won't be found by rung 2 and will look "new" until re-ingested or manually aliased. No backfill was attempted.
2. **Generic Term/System mentions now silently drop** (`DropReason.GENERIC_MENTION`) where they used to mint a node — a ledger-backed stoplist (`data`, `pipeline`, `source`, `table`, `service`, `api`, `system`, `config`, `batch source`, `source_table`, editable via `ledger.add_entity_stoplist_term`/`remove_stoplist_term`), plus a length/genericity floor.
3. **A cross-run Decision reference by bare name can now fail closed** (`ENDPOINT_UNRESOLVED`) if it wasn't also re-extracted as an entity within the same run — an expected consequence of Decision's identity no longer being name-only (see the "Decision fact-endpoint resolution" query below for the fallback that softens this *within* one run).
**Also new, currently inert:** a polarity-vetoed Decision merge now creates a `pending` review (`polarity_conflict_candidate`) via the §3.0 queue — auditable, but nothing consumes it yet (Phase 5 doesn't exist).

## 4.2 — Rungs 4/5 (vector candidates) will return nothing against real data until the namespace-payload gap closes
**Raised:** 25 Sep 2026
**Blocks:** nothing broken — the ladder still works, just always falls through to `resolved_by="new"` for System/Term/Decision today, which happens to be exactly what §4.3's "under-merge by default" policy wants anyway. Flagging so it's not mistaken for a bug later.
**Question:** same root cause as the earlier-flagged namespace gap in `vector_store.search_above` — no write path populates `namespace_uid` in the vector payload yet (`graph/jira_pipeline.py`, `graph/semantic_pass.py`'s own embedding writes, `graph/embed_batch.py`, `scripts/rebuild_vectors.py`). Until one of those is updated, rungs 4/5 query correctly but always come back empty on real data. Worth prioritizing that follow-up once Phase 4 is otherwise validated?
**Assumption made for now:** implemented rungs 4/5 to pass `namespace_uid` anyway (correct, forward-compatible), rather than omitting the filter as a workaround.

## 4.0 — Decision fact-endpoint resolution needs a name-based fallback within a run
**Raised:** 25 Sep 2026
**Blocks:** nothing broken — a narrow, documented edge case.
**Question:** `ExtractedFact` only ever carries a bare `subject_name`/`object_name`, never a Decision's full `statement`, so a fact referencing a Decision by name can't reconstruct the new record+statement-scoped identity key. The merged code adds a supplementary run-owned `decision_name_index: dict[name_norm, uid]` as a fallback, with last-write-wins if two different Decision statements share a name within one run. Is that fallback's behavior (silently picking the most-recently-resolved Decision with that name) acceptable, or should an ambiguous same-name-different-statement case be handled differently (e.g. logged, or left unresolved)?
**Assumption made for now:** last-write-wins, documented in code.

## 4.8 — Alias table write-side (review approval → alias) not wired
**Raised:** 25 Sep 2026
**Blocks:** the alias table (rung 3) is currently read-only in practice — nothing populates it yet except manual entries.
**Question:** the plan says aliases come from "approved `POSSIBLY_SAME_AS` reviews, approved Phase 6.4 pairwise merges, and manual entries." The natural place for the first ("approving a review calls `ledger.add_entity_alias`") is `demo_ui/backend/review_routes.py`'s approval endpoint — a small, generic addition since it applies across review types, not specific to `graph/semantic_pass.py`. Want this wired now as a quick follow-up, or held until there's a real producer of `possibly_same_as` reviews (which doesn't exist yet either, since that's also blocked on Laya)?
**Assumption made for now:** left unwired — read-side (rung 3 lookup) works, write-side doesn't yet.

---

## 3.0 — Review queue: rejection-identity shape is caller-supplied and unverified against a real caller
**Raised:** 25 Sep 2026
**Blocks:** nothing merged is broken (290 passed/12 skipped) — a design choice worth a sanity check once real callers exist.
**Question:** the merged `reviews`/`review_rejections` tables (`connectors/core/ledger.py`) treat `identity` as an opaque, caller-supplied string the ledger never parses out of `payload` — documented recommended shape is `f"{type}:{subject_uid}:{object_uid}"`, but nothing enforces it. Phase 4's `possibly_same_as` and Phase 5's `fact_update` (the two callers the plan names) don't exist yet, so this is unverified against a real caller. Fine as a contract, or should the ledger own identity derivation instead?
**Assumption made for now:** shipped as opaque/caller-supplied — reversible, just changes what future call sites pass as `identity=`.

## 3.0 — Review queue: no authenticated-reviewer identity yet
**Raised:** 25 Sep 2026
**Blocks:** nothing merged is broken — a gap worth knowing about before Phase 6's `BridgePanel` wires into this.
**Question:** `POST /api/reviews/{id}/approve|reject` takes `decided_by` as a required, unauthenticated query param (this app has no logged-in-user concept yet — `demo_ui/backend/access.py` only tracks per-connector OAuth scope). Anyone calling the endpoint can claim to be anyone. Acceptable for now (internal/demo tool), or does this need real auth before Phase 6 builds a UI on top of it?
**Assumption made for now:** left as an open string param; flagged for Phase 6, not fixed now since real auth is a larger, separate decision.

## 2.1 — Laya reranker is a stub; real integration needs package + checkpoint decisions
**Raised:** 25 Sep 2026
**Blocks:** the "D" variant (Laya `retrieval_relevance`) of Phase 2's four-way bake-off can't run yet.
**Question:** `LayaReranker.score()` in `graph/rerank.py` raises `NotImplementedError` on purpose rather than guessing at an unverified implementation. What's confirmed from reading `/Users/akshaychame/personal_exp/laya` (read-only): the exact trained question/state schema (`ingest/schema.py`'s `QUESTIONS["retrieval_relevance"]`) and the call shape Laya's own prototype uses (`graph_view/reranker.py`: `laya.Agent(MODEL_DIR, device="cpu").predict_batch(...)`). What's still needed to finish it for real: (1) a decision on whether the `laya` package becomes a Neuron dependency or stays an external/sidecar service, (2) making the trained checkpoint (`personal_exp/laya/model/laya-ingest/`) available to Neuron's runtime, (3) verifying `Agent.__init__`/`predict_batch`'s real signature directly against the `laya` package source (only seen second-hand via one caller so far). How do you want Laya packaged for Neuron — vendored dependency, sidecar service Neuron calls over a local API, or something else?
**Assumption made for now:** none — left as an honest `NotImplementedError` with the evidence documented in the class docstring, rather than shipping a guessed-at "working" implementation.

---

## 5.1 — `stated_dates`: sprint length and `dateparser` not added
**Raised:** 25 Sep 2026
**Blocks:** nothing merged is broken (316 passed, 0 failed) — two small judgment calls worth a look before this is wired into real writes.
**Question:** `graph/dates.py::stated_dates` handles "from next sprint" with a hardcoded `_SPRINT_LENGTH_DAYS = 14` — no real sprint-length config exists anywhere in this codebase to read from instead. Is 14 days a reasonable placeholder, or is there a real per-project sprint length this should read? Separately: `dateparser` (the dependency the plan names for relative-date parsing) isn't installed, so this was built as a narrower regex-only implementation instead (documented gaps: no general NLP phrasing like "a fortnight from signing", no fiscal quarters, English-only). Add `dateparser` as a real dependency, or is the regex-only scope acceptable?
**Assumption made for now:** 14-day sprint constant (easy to change in one place), regex-only date parsing (the seam to swap in `dateparser` is documented in the module).

---

## 5.0 — RESOLVED-STYLE NOTE: ~23 existing readers don't use the new live-fact predicate yet
**Raised:** 25 Sep 2026
**Blocks:** nothing today (harmless — nothing writes `assertion_status="corrected"`/`projection_status="pending_review"` yet except the new §5.0 primitives and their tests). Becomes real once §5.2 (`resolve_text_fact`) lands and starts writing those states for real.
**Question:** `graph/fact_predicates.py`'s `live_fact_cypher()` is the one correct "is this fact edge live" check (`invalid_at IS NULL AND assertion_status != 'corrected' AND projection_status = 'live'`), but ~23 existing `invalid_at IS NULL` call sites across `graph/chat.py`, `graph/derived.py`, `graph/inference.py`, `graph/graph_view.py`, `graph/expand.py`, `graph/history.py`, `graph/structured_query.py` still use the old, narrower check. `correct_fact` defends against this today (it also collapses the live edge's `invalid_at` to `valid_at`, so even an unconverted reader treats a corrected fact as not-live) — but `pending_review` facts (§5.2, not built yet) have no such defense, since a zero-width interval doesn't make sense for "not yet approved." Once §5.2 exists, should propagating the centralized predicate to these readers be part of that same task, or a dedicated follow-up?
**Assumption made for now:** none — `graph/chat.py` is off-limits to agents right now anyway (your in-progress Laya work), so this is naturally deferred, not actively worked around.

---

## 5.1 — `upsert_fact_edges` doesn't accept `valid_at_basis`/`invalid_at` yet; semantic_pass.py has a temporary stopgap
**Raised:** 25 Sep 2026
**Blocks:** nothing broken today — `graph/semantic_pass.py` (§5.1 wiring, merged) empirically confirmed `upsert_fact_edges` silently drops unrecognized row keys rather than erroring, so it added a small supplementary Cypher write (matched by `fact_uid`) right after the normal call, only when `valid_at_basis == "stated"` or an `invalid_at` resolved — zero extra graph calls for the common no-date case. Clearly marked `# STOPGAP` in the code with instructions to delete once fixed.
**Question:** `graph/writer.py`'s `upsert_fact_edges` needs two small additive `ON CREATE`/`ON MATCH` lines for `valid_at_basis`/`invalid_at` (same pattern as the other optional fields already there — `pinned`, `decay_class`, etc.). This naturally belongs with the §5.2 `resolve_text_fact` work (same file, in flight as this is written) — worth folding into that task's cleanup, or a dedicated one-line follow-up after?
**Assumption made for now:** the stopgap only persists `valid_at_basis` when it's `"stated"`, not the default `"record_time"` — a missing property should be read by any future reader as `"record_time"`. Flagging in case the repo owner would rather it always be persisted once `writer.py` is patched (trivial one-line change either way).

---

## 6 — Phase 6.5/6.6 (review UI, health dashboard) — deferred until backend lands, not blocked anymore
**Raised:** 25 Sep 2026 · **Updated:** 25 Sep 2026 (Laya frontend work committed as `e3ad611` — `App.tsx`/`api.ts` no longer off-limits)
**Not a question anymore** — this wave still builds the backend half of Phase 6 first (§6.1-§6.4: hygiene reporting, candidate generation, duplicate collection) before touching `BridgePanel.tsx`/the dashboard, simply because the UI needs to know the real shape of `link_candidate`/`duplicate_pair` review data before it can render it meaningfully — not because of any file restriction anymore.

---

## 6.2 — `_shared_concept_documents`'s ledger is inert until two callers are updated
**Raised:** 25 Sep 2026
**Blocks:** `graph/derived.py::_shared_concept_documents` (now produces `link_candidates` proposals instead of direct edges, per the plan) currently never fires in a real sync, since its two real callers (`graph/jira_pipeline.py`, `graph/resolver.py`) don't pass the new optional `ledger` parameter yet — it silently no-ops (logged) rather than crashing.
**Question:** small, mechanical follow-up needed: thread a real `ConnectorLedger` through those two call sites so this path actually activates. Worth doing now as a quick fix, or fine to leave dormant until someone next touches those files?
**Assumption made for now:** left as a documented no-op — safe (never silently reverts to the old direct-edge behavior), just inactive.

## 6.2 — Laya's `relation_type` vocabulary only partially maps onto Neuron's real relations
**Raised:** 25 Sep 2026
**Blocks:** nothing broken — real, verified Laya calls (`laya.Agent(...).predict_batch(...)`, confirmed live against the checkpoint) work correctly, but only 4 of Laya's 9 trained non-"none" labels (`owns`, `part_of`, `references`, `blocks`) map to a real Neuron relation; the rest (mostly Person-shaped labels that can't apply here anyway, since `Person` is hub-excluded) always yield "none" → no candidate written.
**Question:** is this mapping worth revisiting once real candidates are observed from production data, or is the conservative "map only what clearly translates" approach fine long-term?
**Assumption made for now:** conservative mapping, documented in `graph/link_candidates.py::_RELATION_MAP`, easy one-line change per label.

## 6.2 — Semantic-candidate similarity threshold (0.65) and provenance shape (union vs. shared-only)
**Raised:** 25 Sep 2026
**Blocks:** nothing — both are working, reversible defaults.
**Question:** `find_semantic_candidates` defaults to `similarity_threshold=0.65` (the plan gives exact numbers for the resolution ladder's 0.90/0.75 rungs but not this one) — a real but loose review-queue filter, looser than the merge-adjacent rungs since nothing here auto-merges. Separately, `apply_approved_link_candidate`'s provenance is the **union** of both endpoints' `MENTIONED_IN` records (the plan's "provenance = both nodes' records" is ambiguous between union and shared-only intersection). Are both defaults right, or was shared-only intersection intended for provenance?
**Assumption made for now:** 0.65 threshold, union provenance — both documented in code, one-line changes if wrong.

## 6.4 — Node-level "reinforced" definition for survivor selection
**Raised:** 25 Sep 2026
**Blocks:** nothing — real, tested, working.
**Question:** the plan says merge survivor = "most reinforced, then oldest" but `reinforce_count` (§5.7) is defined per fact-edge, not per node. The duplicate collector sums `reinforce_count` across every live fact edge touching a node (excluding `MENTIONED_IN`) as the node-level proxy. Reasonable, or was a different aggregation intended (e.g. count of distinct `MENTIONED_IN` source records instead)?
**Assumption made for now:** sum of live-edge `reinforce_count`, documented in `graph/duplicate_collector.py::_node_reinforcement`.

## 2.1 — `select_final`'s token-budget cap has no data to work with yet
**Raised:** 25 Sep 2026
**Blocks:** nothing yet (Phase 2.4 tuning, not started) — a heads-up for whoever wires Phase 2.2.
**Question:** the plan lists `min_keep`/`max_keep`/token budget together as `select_final`'s hard caps, but `RerankScore` carries no token count, so token-budget enforcement was left out of `select_final` and left to the future `chat.py` caller (which already has candidate text + a real prompt budget from Phase 1.2's packing work). Fine as designed, or do you want `RerankCandidate`/`RerankScore` extended with a token count now so `select_final` can enforce the budget itself?
**Assumption made for now:** threshold → diversity (documented no-op, dedupe-by-uid only — the plan names "diversity" with no concrete algorithm) → min_keep/max_keep, implemented and tested; token budget deferred to the caller.

---

## 1.4 — `min_tier` can never be `"primary"` in today's control flow
**Raised:** 25 Sep 2026
**Blocks:** nothing broken; the "primary for lookup questions" half of §1.4's expansion trust floor is currently dead code.
**Question:** in `graph/chat.py`'s `retrieve()`, `expand_neighbors()` is only ever reached when `structured is None` (a non-`None` `resolve_structured()` result already returns earlier in the function) — but the plan's §1.4 "primary for lookup questions" branch is keyed off exactly that same `structured`-result signal. So expansion's `min_tier` is always `"derived"` in practice today; the `"primary" if structured is not None else "derived"` conditional is implemented but unreachable. Is there a different signal you want for "this is a lookup-shaped question" (e.g. a regex/anchor check independent of whether structured resolution fully answered it), or is always-`"derived"` fine until Phase 2.5's query-type router exists?
**Assumption made for now:** implemented the conditional as documented, dead-but-ready code, rather than hardcoding a bare `"derived"` constant that would need rediscovering later.

## 1.7 — two-entity lane prepends entity nodes, not literal SourceRecords
**Raised:** 25 Sep 2026
**Blocks:** nothing broken — flagging a deviation from the plan's literal wording, not a bug.
**Question:** the plan says to "fetch the `SourceRecord`s that both nodes are `MENTIONED_IN`... and prepend them as candidates." `SourceRecord` nodes have no `uid` (keyed by `record_key`) and no raw text content (only `content_hash`) — see `graph/writer.py::upsert_source_records` — so they can't literally be prepended as scoreable/evidence-block candidates the way other `SearchHit`s are. The two-entity lane instead prepends the two resolved **entity** nodes themselves (`methods=["pair"]`), whose evidence naturally surfaces via `_entity_evidence`, with their summary prefixed by the names of the `SourceRecord`s that mention both. Is this the right substitution, or was something else intended (e.g. surfacing the `SourceRecord`'s own text/link directly in the evidence block, which would need `SourceRecord` given a synthetic uid/content shape first)?
**Assumption made for now:** entity-node substitution, since it's the closest reachable approximation of "the shared evidence between these two nodes" with the data `SourceRecord` actually carries today.
