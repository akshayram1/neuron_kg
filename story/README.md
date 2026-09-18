# Neuron software-change story fixtures

This directory contains synthetic, deterministic connector output for three
projects:

1. **Auth Service** provides `Auth API v1` and `POST /v1/auth`.
2. **MCP Gateway** consumes the API and calls that endpoint.
3. **Argo Orchestrator** consumes the same API and calls the same endpoint.

The fixtures are intentionally organized as a story rather than as one static
snapshot. Load `baseline/` first, then apply each directory under `events/` in
numeric order.

## Data contract

The provider JSON uses the field names emitted by the existing connector
adapters:

- Jira objects match `JiraSite`, `JiraProject`, `JiraIssue`, `JiraPerson`, and
  `JiraChange` from `connectors/jira/api.py`.
- Notion pages match `NotionPage` from `connectors/notion/api.py`.
- Bitbucket objects match `BitbucketWorkspace`, `BitbucketRepository`,
  `BitbucketFile`, `BitbucketFileChange`, `BitbucketCommit`, and
  `BitbucketPullRequest` from `connectors/bitbucket/api.py`.

Each JSON file has a small fixture envelope (`fixture_version`, connection or
workspace information, and arrays of connector objects). The objects inside
those arrays can be passed directly to their corresponding dataclass
constructors after converting nested objects and lists to dataclasses and
tuples.

Bitbucket `files[].content` is the authoritative connector payload. A matching
repository tree is also included next to each JSON bundle so the scenario is
easy to read and can later support filesystem-based tests. The current
connector only accepts `.py` and `.md`, so the fixture uses those extensions.

## Scenario

### `baseline/`

All three sources agree:

```text
Auth Service ──PROVIDES_API──► Auth API v1
MCP Gateway  ──CONSUMES_API──► Auth API v1
Argo         ──CONSUMES_API──► Auth API v1

MCP source   ──CALLS_ENDPOINT──► POST /v1/auth
Argo source  ──CALLS_ENDPOINT──► POST /v1/auth
```

### `events/01_auth_v1_deprecation/`

`AUTH-900` schedules removal of v1 on 2026-10-01. MCP and Argo both receive
migration tickets. The expected result is a breaking-change signal affecting
both consumers.

### `events/02_mcp_migration_claim/`

Jira and Notion say MCP has completed the v2 migration, but the most recently
ingested MCP Bitbucket file still calls `/v1/auth`. The expected result is a
documentation/code mismatch. The old dependency must not be silently closed.

### `events/03_mcp_code_catches_up/`

The MCP repository changes from `/v1/auth` to `/v2/auth`. The mismatch should
resolve and MCP should leave the v1 blast radius. Argo should remain affected.

### `events/04_auth_v1_removed/`

Auth Service removes `/v1/auth` after a readiness page incorrectly claims all
consumers migrated. Argo's unchanged production client still calls the removed
route and workflows begin failing. The expected result is a critical,
materialized impact Finding, a new documentation/code mismatch, and a
consumer-readiness Wisdom proposal.

## Stable identities

All IDs, timestamps, URLs, Jira keys, commit hashes, and Bitbucket UUIDs are
fixed. This makes the fixtures suitable for repeatable graph assertions and
golden tests. No object refers to a real company or external account.

## Run the guided demo

```bash
bash scripts/run_story_demo.sh
```

Open `http://localhost:8000`, click **Story demo**, and run the five phases in
order. The first action creates a fresh graph and ingests the baseline. The
next actions introduce the deprecation, the conflicting migration claim, and
matching code change, then the v1 removal that exposes Argo. **Reset demo** removes that story graph from
PostgreSQL/pgvector and FalkorDB without touching other graphs.

The fixtures replace only remote Jira, Notion, and Bitbucket fetching. Their
payloads still pass through the connector dataclasses, canonical records,
hash/change planning, chunking, embeddings, extraction, ontology validation,
temporal Falkor writes, impact rules, and findings persistence.

The script uses isolated host ports `55432` (PostgreSQL) and `6380`
(FalkorDB), so it does not replace services already using the conventional
ports. Set `STORY_RUN_LLM=false` only when you want a faster deterministic
smoke test; the default runs the semantic extraction pass as well.
