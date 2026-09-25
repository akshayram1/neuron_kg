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
