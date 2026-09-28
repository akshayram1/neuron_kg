# Auth API v1 story — events and findings

Guided demo: three projects, Jira + Notion + Bitbucket, four phases.
Fixtures: `story/`. Run: `bash scripts/run_story_demo.sh` → UI **Story demo**.

All fixture timestamps are **UTC**. Finding “Detected” times on a live run are
**record time** (when Neuron ingested the phase), not these world dates.

| Project | Role |
|---|---|
| Auth Service (`AUTH`, `story-labs/auth-service`) | Owns `POST /v1/auth` and `POST /v2/auth` |
| MCP Gateway (`MCP`, `story-labs/mcp-gateway`) | Consumer — `src/auth_client.py` |
| Argo Orchestrator (`ARGO`, `story-labs/argo-orchestrator`) | Consumer — `src/token_client.py` |

---

## Four stages at a glance

1. **Baseline:** Auth Service exposes v1 and v2, while MCP Gateway and Argo Orchestrator both still call `POST /v1/auth`.
2. **Deprecation:** AUTH-900 schedules the v1 removal, so live code dependencies place both MCP and Argo in the blast radius.
3. **Migration claim:** Jira and Notion say MCP moved to v2, but current Bitbucket code still calls v1, creating a claim-versus-code Finding.
4. **Code catches up:** MCP code moves to `POST /v2/auth`, making its mismatch and impact Findings stale while Argo remains affected.

---

## Events (world time)

| When (UTC) | Phase | Source | Event |
|---|---|---|---|
| 2026-08-01 09:00 | 1 Baseline | Jira AUTH-301 | Created: *Publish Auth API v1 contract*. Consumers: MCP, Argo. |
| 2026-08-03 08:30 | 1 | Jira MCP-101 | Created: *Integrate MCP Gateway with Auth API v1*. |
| 2026-08-04 11:20 | 1 | Jira ARGO-201 | Created: *Use Auth API v1 for workflow service tokens*. |
| 2026-08-05 15:00 | 1 | Bitbucket auth-service | Commit: `AUTH-301 publish POST /v1/auth production contract`. |
| 2026-08-05 15:30 | 1 | Jira AUTH-301 | Status → **Done** (Asha Rao). |
| 2026-08-09 16:30 | 1 | Bitbucket mcp-gateway | Commit: `MCP-101 integrate MCP Gateway with POST /v1/auth`. |
| 2026-08-09 16:45 | 1 | Jira MCP-101 | Status → **Done** (Mira Sen). |
| 2026-08-10 07:00 | 1 | Jira MCP-102 | Created: *Add retry policy around authentication calls*. |
| 2026-08-11 13:00 | 1 | Bitbucket argo | Commit: `ARGO-201 request workflow tokens from POST /v1/auth`. |
| 2026-08-11 13:15 | 1 | Jira ARGO-201 | Done. |
| 2026-08-12 09:40 | 1 | Jira ARGO-202 | Created: *Document authentication failure behavior*. |
| 2026-08-14 16:55 | 1 | Bitbucket mcp-gateway | Commit: `MCP-102 retry Auth API v1 transport failures`. |
| 2026-08-14 17:10 | 1 | Jira MCP-102 | Done. |
| 2026-08-15 09:55 | 1 | Bitbucket argo | Commit: `ARGO-202 document blocked workflow behavior`. |
| 2026-08-15 10:05 | 1 | Jira ARGO-202 | Done. |
| 2026-08-18 10:15 | 1 | Jira AUTH-302 | Created: *Introduce Auth API v2 without removing v1*. |
| 2026-09-01 11:45 | 1 | Bitbucket auth-service | Commit: `AUTH-302 add Auth API v2 alongside v1`. Both routes live. |
| 2026-09-01 12:00 | 1 | Jira AUTH-302 | Still **In Progress**. |
| 2026-09-01 12:10 | 1 | Notion Auth | *Auth Service Architecture*. |
| 2026-09-01 12:20 | 1 | Notion Auth | *Auth API v1 Contract*. |
| 2026-09-02 09:00 | 1 | Notion MCP | *MCP Gateway Architecture* (v1). |
| 2026-09-02 09:15 | 1 | Notion MCP | *MCP Authentication Runbook*. |
| 2026-09-02 10:00 | 1 | Notion Argo | *Argo Orchestrator Architecture*. |
| 2026-09-02 10:15 | 1 | Notion Argo | *Argo Authentication Failure Runbook*. |
| 2026-09-10 08:00 | 2 Deprecation | Jira AUTH-900 | Created: *Remove Auth API v1 on October 1* (Proposed). |
| 2026-09-10 14:30 | 2 | Jira AUTH-900 | Status → **Approved**. Blocks MCP-150 and ARGO-250. |
| 2026-09-10 14:40 | 2 | Bitbucket auth-service | Commit: `AUTH-900 mark POST /v1/auth deprecated` (`V1_SUNSET_DATE=2026-10-01`). Route still live. |
| 2026-09-10 14:45 | 2 | Notion Auth | *Auth API v1 Deprecation Plan*. |
| 2026-09-10 15:00 | 2 | Jira MCP-150 | Created **To Do**: migrate MCP to v2. Complete only when `auth_client.py` drops `/v1/auth`. |
| 2026-09-10 15:05 | 2 | Jira ARGO-250 | Created **To Do**: migrate Argo to v2. Argo stays a v1 consumer until `token_client.py` changes. |
| 2026-09-11 09:00 | 3 Claim | Jira MCP-150 | To Do → **In Progress** (Mira Sen). |
| 2026-09-12 11:00 | 3 | Jira MCP-150 | → **Done**. Text claims MCP now uses `POST /v2/auth`. Comment: check the repo separately. **No Bitbucket change.** |
| 2026-09-12 11:15 | 3 | Notion MCP | Architecture updated: “as of 2026-09-12 MCP uses v2… Bitbucket snapshot not yet checked.” |
| 2026-09-15 13:30 | 4 Catch-up | Bitbucket mcp-gateway | Commit: `MCP-150 migrate authentication client from v1 to v2` (`AUTH_API_VERSION = "v2"`). |
| 2026-10-01 | (scheduled) | AUTH-900 sunset | `/v1/auth` is supposed to be removed. **Not an ingested event.** |

After phase 1, both consumers call `/v1/auth`. After phase 2, AUTH-900 is live and both are in the blast radius. After phase 3, MCP *docs* say v2 while *code* is still v1. After phase 4, MCP code is v2; Argo is still v1.

---

## Findings (after all four phases)

Live board from a full Story demo run (semantic pass on). **2 open · 8 total.**

“Detected” on that run was ingest clock (17 Sep 2026), not the table above.

### Open

| Finding | Kind | Why it is correct |
|---|---|---|
| **MCP Gateway authentication dependency migrated to Auth API v2** | Semantic `architecture_change` (several source hits consolidated) | Jira/Notion claimed the move on **12 Sep**; Bitbucket commit + HEAD file confirm `POST /v2/auth` on **15 Sep**. Code/commit are authoritative for implementation. 4 sources. |
| **Argo Orchestrator may be affected** | Deterministic `cross_project_impact` | AUTH-900 `DEPRECATES` Auth API v1, and Argo still has a live `CALLS_ENDPOINT` to `POST /v1/auth`. 3 sources. |

### Stale / resolved

| Finding | Why it went stale |
|---|---|
| **MCP Gateway migration claim conflicts with code** | Phase-3 mismatch. Phase-4 Bitbucket now agrees with Jira/Notion. Reason: *Current Bitbucket code now agrees with the Jira/Notion migration claim.* |
| **MCP Gateway may be affected** | Same impact rule as Argo. MCP’s live `CALLS_ENDPOINT` to `/v1/auth` was superseded. Reason: *Current code no longer supports the dependency path that produced this impact.* |
| **MCP Gateway authentication dependency migrated to Auth API v2** (earlier copy) | Consolidated into the canonical open finding; evidence kept. |
| **MCP Gateway migrated its authentication dependency from v1 to v2** | Same consolidation. |
| **Migrate authentication client from v1 to v2** | Commit-only fragment; folded into the canonical finding. |
| **Authentication endpoint changed to v2** | File-level fragment; folded into the canonical finding. |

---

## Expected vs this board

| After phase | Expected (`story/manifest.json`) | Board |
|---|---|---|
| 1 Baseline | No breaking-change signal | No AUTH-900 impact yet |
| 2 Deprecation | MCP **and** Argo in blast radius | Both `… may be affected` open |
| 3 Claim | Docs/code mismatch; do not close MCP’s v1 call | `migration claim conflicts with code` open; MCP still impacted |
| 4 Catch-up | Mismatch gone; MCP leaves blast radius; **Argo stays** | Mismatch stale; MCP impact stale; Argo impact **open**; extra open finding that MCP did migrate (true) |

The extra open “MCP migrated to v2” finding is Luna, not the blast-radius rule. It is factually right after phase 4.

Rules: `connectors/story/loader.py` → `recompute_findings` (`MAY_IMPACT` / `… may be affected`) and `_recompute_mismatches`. Consolidation: `graph/profiles.py` `topic_key`.
