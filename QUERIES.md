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
