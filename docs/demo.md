# Argus knowledge map — Jira × Notion × `rubik_/argus` (`typesense`)

Fetched 2026-09-09 via Jira MCP (`rubikai.atlassian.net`) and Notion MCP. Repo evidence from a local clone of `git@bitbucket.org:rubik_/argus.git` branch `typesense` @ `be023b9`.

---

## 1. Jira — DATAOS-3833 and descendants

### Project + board

| Field | Value |
|---|---|
| Project key | `DATAOS` |
| Project name | DataOS 2.0 |
| Project id | `11186` |
| Issue browse | https://rubikai.atlassian.net/browse/DATAOS-3833 |
| Project browse | https://rubikai.atlassian.net/browse/DATAOS |
| Software boards (MCP does not return a board id) | https://rubikai.atlassian.net/jira/software/c/projects/DATAOS/boards |

### Parent — DATAOS-3833

| Field | Value |
|---|---|
| Key | [DATAOS-3833](https://rubikai.atlassian.net/browse/DATAOS-3833) |
| Title | Converse across Data Products through an org-wide Meaning layer |
| Type | Epic |
| Status | To Do |
| Assignee | unassigned (reporter: Soumadip De) |

**Full description (verbatim):**

```
## Outcome

Ask questions across data products in plain business language — through an org-wide Meaning layer that converges what each product measures with what the organization knows about it. Governed vocabulary, cross-product scope, answers cited back to concepts and products. Epic 1 made one product conversational; Epic 2 makes the whole landscape addressable.

## The Problem Today

Teams build semantic definitions locally — that is right. But when someone asks _"How much revenue is at risk?"_ or _"Where is Customer used?"_, the answer spans products, definitions, and dependencies the question never names. That connective knowledge — what terms mean org-wide, what depends on what, which product holds which measure — lives in people's heads and scattered docs, not in anything queryable. Cross-product questions still have no surface.

## The Solution

Teams continue authoring semantic definitions inside each data product — domain expertise stays local. The platform collects what products publish and converges it into org-wide meaning: stable business concepts, cross-product relationships, and approved links from language to the assets that realize it. That converged layer is what makes conversation across data products possible — resolving ambiguous questions into which products and measures matter, returning answers with citations, and refusing to guess when vocabulary or scope is unresolved.

Operating model: **decentralized generation, centralized convergence**.
```

### DATAOS-4346 vs DATAOS-3833

**DATAOS-4346 is not a direct child of DATAOS-3833.**

It is a **Sub-task of DATAOS-3839**, and DATAOS-3839 is a **Task whose parent is DATAOS-3833**.

```
DATAOS-3833  (Epic)
 └── DATAOS-3839  (Task)  "Power data product discovery for Agents and UI"
      └── DATAOS-4346  (Sub-task)  "Argus API — close Glossary & Context Cards gaps (Notion matrix)"
           assignee: Aashish Verma
           status: In Progress
```

`typesense` HEAD commit is exactly this ticket:

```
be023b9 DATAOS-4346 Added ai_instructions,ai_caveats,ai_synonyms and example_queries to the asset search hits
```

That commit implements one 4346 acceptance line: “extend default search projection for `ai_instructions`, `ai_caveats`, `ai_synonyms`”.

---

### All descendants (28 issues including the epic)

JQL used: `"Epic Link" = DATAOS-3833 OR parent = DATAOS-3833 OR parentEpic = DATAOS-3833`.

#### Direct children of DATAOS-3833

##### DATAOS-3839 — Task — To Do — Soumadip De

**Title:** Power data product discovery for Agents and UI

Argus Harvest should become the Platform UI catalog/search layer (including semantic search), replacing Hera for data-product metadata. Hera stays for source-level metadata; Harvest owns the data-product catalog, glossary powering, and the foundation Meaning/Grounding build on.

##### DATAOS-3840 — Story — To Do — unassigned

**Title:** Terms — attach business context to your Data Products

Central org Terms experience: harvested vocabulary tree, drill-down from a term to every assigned product/asset/member, then limited steward Enrich. Phase 1 read-only, Phase 2 limited CRUD, Phase 3 quality/governance/Ask. Product name decision (28 Jul): **Terms**.

##### DATAOS-3841 — Story — To Do — unassigned

**Title:** Org-wide business meaning layer

Ship Meaning (stable concepts/relations) and Grounding (steward-approved bindings). Sprint goal quote: “make Argus Dev Complete while also ensuring its Release Readiness.” Terms-as-strings are not enough.

##### DATAOS-3842 — Story — To Do — unassigned

**Title:** GUI for Cross-product conversations — trusted and traceable

Platform chat across all data products, powered by Harvest + Terms + semantic layer. Resolve language org-wide, cite concepts/products, refuse to guess when unresolved.

##### DATAOS-4043 — Story — Done — Soumadip De

**Title:** [PoC] Terms-as-Ontology structural fit — Helix replay

Internal PoC: test whether steward-enriched Terms can carry org-wide meaning without a separate SKOS graph, by replaying Helix scenarios. Out of scope: shipping glossary UI.

##### DATAOS-4051 — Story — To Do — Soumadip De

**Title:** Governance readiness + FedRAMP/CVE posture — make Argus governable and Compliance-ready

Second Front / FedRAMP inventory, RapidFort images, 0 critical / 0 high CVEs. “Argus production is September” — not bound to the 17 Aug IKS deadline.

##### DATAOS-4191 — Task — To Do — Akshay Chame

**Title:** Burn the Data Product MCP into Argus

Description is empty in Jira.

##### DATAOS-4205 — Task — To Do — unassigned

**Title:** Add a Data Steward role for operating Argus terms

Heimdall use-case so Enrich writes (`PATCH /argus/api/v1/terms/{fqn}`, synonym/relationship POST/DELETE) are steward-only; browse stays on Use Argus Service.

#### Under DATAOS-3839 (discovery / Harvest)

##### DATAOS-4175 — Sub-task — To Do — unassigned

**Title:** UI ↔ Argus requirements and gaps document

Shareable gap matrix for Glossary + Context Cards. Points at the Notion page and repo extracts `release/2026-08/argus-glossary-context-cards-gaps.md` and `release/2026-08/ui-argus-requirements-gaps.md`. Those files are **not** on `typesense`.

##### DATAOS-4181 — Sub-task — To Do — unassigned

**Title:** Search — UI requirements meet + API acceptance criteria

Description is empty.

##### DATAOS-4182 — Sub-task — To Do — unassigned

**Title:** Lineage — UI requirements meet + API acceptance criteria

Description is empty.

##### DATAOS-4184 — Sub-task — Done — Soumadip De

**Title:** Onboard Argus to IKS [Dev Env]

Description is empty.

##### DATAOS-4185 — Sub-task — To Do — unassigned

**Title:** Argus Ask — cross-DP metadata Q&A

Description is empty.

##### DATAOS-4186 — Sub-task — To Do — unassigned

**Title:** Vulcan posts qualifying events to Argus webhook

Vulcan must push `plan_updated` and `run_finished` so Argus can refresh without manual harvest. Does **not** replace Hera sync. Duplicate-safe. Latency SLA lives on 4189.

##### DATAOS-4187 — Sub-task — To Do — unassigned

**Title:** SRE deployment runbooks for Argus on intribeiks

Description is empty.

##### DATAOS-4189 — Sub-task — To Do — unassigned

**Title:** After a qualifying event, harvest pulls meta and indexes the data product within 10 seconds

Description is empty.

##### DATAOS-4190 — Sub-task — To Do — unassigned

**Title:** Context Cards — UI requirements meet + API acceptance criteria

Close Argus API gaps blocking Context Card drawers. Gap source is the same Notion page. P0 = unified entity read by ARN or expanded search projections.

##### DATAOS-4346 — Sub-task — In Progress — Aashish Verma

**Title:** Argus API — close Glossary & Context Cards gaps (Notion matrix)

Single backend ticket to implement or document every Gap/Partial in the Notion matrix (FastAPI 1.0.2 baseline). Includes the AI-fields projection that `typesense` HEAD implements.

#### Under DATAOS-3840 (Terms)

##### DATAOS-3865 — Sub-task — In Progress — Aashish Verma

**Title:** Terms v1 — browse, drill-down, and enrich existing terms

August: org term tree, usage drill-down, Enrich (descriptions/synonyms/relationships). Create/delete/status/audit deferred to 3866.

##### DATAOS-3866 — Sub-task — To Do — unassigned

**Title:** Phase 2 — Steward toolkit: create terms, status lifecycle, delete orphaned, audit feed

Create terms (auto-stub ancestors), delete orphaned only, draft→approved→deprecated, audit feed. Reassign/remove from assets stays in company Jira, not UI.

##### DATAOS-3867 — Sub-task — To Do — unassigned

**Title:** Phase 3 — Enable Steward to set rules and actions on Terms

Term-driven quality mandates, cross-product impact reporting, Ask/Review integration.

#### Under DATAOS-3841 (meaning layer)

##### DATAOS-4151 — Sub-task — In Review — Aashish Verma

**Title:** Removing/Fixing Ciritcal Vulnerabilities from argus

Audit/fix critical+high CVEs so Argus is release-ready. Zero critical/high on rescan.

##### DATAOS-4326 — Sub-task — DEV COMPLETE — unassigned

**Title:** Duplicate synonym/relationship add raises IntegrityError instead of 409 Conflict

Pre-check in `TermService.add_synonym` / `add_relationship` and raise `ConflictError` (HTTP 409) instead of raw Postgres `UniqueViolationError`.

#### Under DATAOS-4051 (compliance)

##### DATAOS-4192 — Sub-task — To Do — unassigned

**Title:** Create SRE deployment runbooks for Argus

Description is empty.

##### DATAOS-4193 — Sub-task — To Do — Nikhil Singh

**Title:** Build and implement Argus policy posture on the platform

Add Heimdall/policy coverage for Argus endpoints (`POST /argus/api/v1/tenants`, `GET /argus/api/v1/domains`, `PATCH …`, etc.).

##### DATAOS-4194 — Sub-task — DEV COMPLETE — Aashish Verma

**Title:** Harden Argus - Zero High and Zero Critical CVEs

Description is empty.

#### Under DATAOS-4191 (MCP)

##### DATAOS-4606 — Sub-task — To Do — Akshay Chame

**Title:** Expose MCP Prometheus instruments on /mcp/metrics via a dedicated registry

MCP series collided with Argus HTTP RED on `/metrics`. Isolate `mcp_*` on a dedicated registry at `GET /mcp/metrics`. Cites commit `7ae4320` on branch `typesense`.

---

## 2. Notion pages

### Page A — Argus API gaps — Glossary & Context Cards

| Field | Value |
|---|---|
| Exact title | `📋 Argus API gaps — Glossary & Context Cards` (property title: `Argus API gaps — Glossary & Context Cards`) |
| URL | https://app.notion.com/p/3bac5c1d487681bb934ee7f108e9dd18 |
| Last edited | `2026-09-09T11:46:04.985Z` |
| Type | Page (not a database) |
| Child pages | Yes — untitled wrapper `3bcc5c1d487680f29af2d8624e5fbe9a` → untitled working page `3bcc5c1d4876802a915af55fb77b38fb` (Page B). Page B itself has child `Build with AI`. |

#### Section-by-section summary

**Design references (Figma).** Table of Glossary + six Context Card frames (DP, Model, Perspective, Metric, Column, Measure/Dimension).

**Page: Glossary — August v1 intent (verbatim):**

> Browse the org term tree, search terms, drill into usage across the catalog, and **Enrich** existing terms (description, synonyms, relationships). Creating terms, status lifecycle, and governance audit UI are **not** in v1 — though several write APIs already exist for a later phase.

Glossary gap table:

| UI need | Argus today | Status |
|---|---|---|
| Steward display (name, email) | `GET /api/v1/terms/{fqn}` returns `steward` as identity id | **Partial** |
| Create new term | `POST /api/v1/terms` exists | **Out of Terms page v1 scope** (Only DPs will be able to create new terms) |
| Status lifecycle | `POST /api/v1/terms/{fqn}/status` exists | **Out of v1 scope** |
| Governance audit feed | `GET /api/v1/terms:events` and `GET /api/v1/terms/{fqn}/events` exist | **Out of v1 scope** |

**v1 Term Enrichment - no API gap (verbatim):**

> For completeness — these **are supported** and are not listed as gaps: `PATCH /api/v1/terms/{fqn}` (description, display name, steward, external URL), `POST`/`DELETE` … `/synonyms`, `POST`/`DELETE` … `/relationships`, reverse lookup `GET …/links`, batch hydrate `GET /api/v1/terms:resolve`.

**Page: Context Cards — architectural note (verbatim):**

> Argus exposes most drawer data through **search hit documents** (a trimmed field projection) plus separate **lineage** and **joins** reads. There is **no** single “get entity by ARN for drawer” API. Any gap below is worsened by needing multiple calls and client-side assembly.

Then embeds Page B as a child block.

**Data product drawer** — Partial: version (indexed, not projected), engine (not on DP search hits), freshness (harvest time ≠ data freshness), good-for/not-for/caveats (indexed, excluded from projection), owners (ids only). Gap: quality, policies, usage, gateway public GET, repo/issue/reference links.

**Model drawer** — Partial: measure/dimension lists need follow-up search; `ai_instructions` / `ai_caveats` / `ai_synonyms` exist in index but **not** in default projection (this is what `be023b9` on `typesense` addresses); lineage teaser needs `/lineage`.

**Metric drawer** — Partial: `example_queries` / `example_descriptions` not in default projection; segments via `member_type:=segment`.

**Column / measure / dimension** — Partial: description/AI fields have limited search projection.

**Perspective drawer** — Gap: `perspective` kind not shipped; no Everyone/Only Me visibility field. “Perspective context cards are **outside** Global Search scope but may still be built as a drawer surface.”

**Cross-cutting priorities:** P0 unified entity-by-ARN; P1 health strip / usage / gateway / good-for text; P2 owner display names; P2 perspective entity.

#### Decisions / rationale / assumptions / caveats (Page A)

- **v1 scope decision:** create term, status lifecycle, and audit UI are out of v1 even though APIs exist.
- **Create-term ownership:** “Only DPs will be able to create new terms”.
- **Drawer architecture:** search-hit projection + separate lineage/joins, no get-by-ARN.
- **Steward ids:** Argus will not resolve identity ids; Platform UI / identity service must.
- **Usage:** “Observability / BI — not in Argus catalog. Hide or stub in v1.”
- **Freshness caveat:** “harvest timestamp ≠ data freshness.”
- **Perspective:** outside Global Search; “Curated `perspective` kind is planned; **not shipped**.”

Inline comments (discussions):

- “can expose an endpoint responding complete dp deltails with its respective assets and members”
- “product row + all assets + each asset’s members. No lineage, no joins. Clients keep using `/lineage` and `/joins` (path or `?arn=` forms)”
- “We may keep it out of scope for v1 until data product usage section is built in the data product details page”
- “Argus doesn’t know about perspectives. Vulcan does. If we need perspective search, we need to build that in vulcan”

---

### Page B — untitled working page (`3bcc5c1d4876802a915af55fb77b38fb`)

| Field | Value |
|---|---|
| Exact title | MCP title: `New page`. Property `"title": ""` (untitled). |
| URL | https://app.notion.com/p/3bcc5c1d4876802a915af55fb77b38fb |
| Last edited | `2026-09-09T11:45:38.683Z` |
| Type | **Page, not a database.** The `?d=3bcc5c1d4876805aabf5001c514301c2` query param is a **discussion id**, not a database view. It opens this comment: “Argus has a `source_type` value, but as far as I understand, this `source_type` is a **Vulcan model classification**, not a warehouse / engine name.” |
| Child pages | Yes — [Build with AI](https://app.notion.com/p/3c3c5c1d48768022957eda83d7f5615b) (unrelated Vulcan Review / builder-skills mock; last edited `2026-09-08T10:22:20.255Z`) |

There are **no database rows**. Content is three markdown/toggle sections with tables.

#### Section-by-section

**1. Data points the UI needs (Data product drawer)** — Figma-aligned inventory: header (name/version/engine), health strip (quality/freshness/policies), body (description/domain/alignment/terms/good-for/not-for/caveats), usage (query count/time/active users), links (gateway/repo/issue tracker/references), people (owners/emails), lineage preview, actions (More details / Explore).

**2. ALL data points in `argus-raw-quries`** — 20-field SQL projection on `search_documents` (`id`, `entity_type`, `arn`, DP/asset ids+arns+names, `name`, `display_name`, `description`, `tags`, `terms`, `classification`, `kind`, `domain`, `product_status`, `owner`, `source_type`). Query-only extras: `semantic_score`, `lexical_score`, `rrf_score`, `total_matched`.

**3. UI requirements vs `argus-raw-quries`** — per-field Satisfied / Partial / Missing / N/A. Notable: several cells are marked **Satisfied** in the Status column while Notes say the field is **not** in the SQL (version, engine, good-for, gateway, repo). Treat Status as stale vs Notes.

#### Decisions / rationale / assumptions / caveats (Page B)

Discussion on `d=3bcc5c1d4876805aabf5001c514301c2` (verbatim):

> Argus has a `source_type` value, but as far as I understand, this `source_type` is a **Vulcan model classification**, not a warehouse / engine name. Its values would be like seed, sql, metric, semantic, external, rollup, etc.
>
> we need a value for engine type.
>
> let me know if my assumptions about the `source_type` field are incorrect

Other comments:

- “Can we take Quality from Vulcan APIs”
- “do you need the Explore nav link in the Data Product object?” / reply: “Yes this is UI routing”

Body notes (verbatim):

> No `updated_at`, `last_harvested_at`, or model health counts in SELECT. But even these fields don’t seem likely to be related to the data product’s freshness.

> Not in the `argus-raw-quries` SQL (Typesense search has `alignment`; this projection does not)

> FQNs present; we need the display name instead. `display_name` from the terms object.

> Drawer expects roster (`users[]`); one id, no display names

> Ready via `/lineage/data-products?arn=`; not part of search SQL

---

## 3. Cross-reference — exact strings

### a) Do the Notion pages mention a Jira key?

**No.** Neither Page A nor Page B body contains `DATAOS-3833`, `DATAOS-4346`, or any `DATAOS-####`.

The Jira tickets point **at** Notion, not the other way around. Closest Jira→Notion sentences:

DATAOS-4175:

> **Authoritative gap doc (Notion):** https://app.notion.com/p/3bac5c1d487681bb934ee7f108e9dd18

DATAOS-4190:

> **Gap source (Notion):** https://app.notion.com/p/3bac5c1d487681bb934ee7f108e9dd18 — see _Page: Context Cards_ and _cross-cutting_ sections.

DATAOS-4346:

> **Authoritative gap matrix (Notion):** https://app.notion.com/p/3bac5c1d487681bb934ee7f108e9dd18

### b) Do the Notion pages mention argus / repo / typesense / file paths?

**`argus` / `Argus`:** yes, throughout Page A and Page B (service name, not the Bitbucket slug).

**`rubik_/argus`:** not found.

**`typesense` as a Bitbucket branch:** not found.

**`Typesense` as the search engine:** yes, Page B:

> Not in the `argus-raw-quries` SQL (Typesense search has `alignment`; this projection does not)

**File / query names (verbatim):**

- `` `argus-raw-quries` `` (typo; appears as section title and in Notes)
- `` `search_documents` `` — “Search SQL projection on `search_documents` (same fields in semantic, hybrid, lexical).”
- API paths such as `` `GET /api/v1/terms/{fqn}` ``, `` `GET /api/v1/lineage/assets?arn=…` ``, `` `/lineage/data-products?arn=` ``
- Index field names: `` `ai_instructions` ``, `` `ai_caveats` ``, `` `ai_synonyms` ``, `` `good_for_text` ``, `` `not_for_text` ``, `` `caveats_text` ``, `` `example_queries` ``, `` `example_descriptions` ``

**Repo paths like `src/argus/...`:** not found on either Notion page.

### c) Do DATAOS-3833 or descendants mention the repo / Bitbucket URL / branch / commit?

**DATAOS-3833 itself:** no Bitbucket URL, no `rubik_/argus`, no branch, no commit hash.

**No ticket in this tree contains `bitbucket.org` or `rubik_`.**

Repo-adjacent literals that *do* exist:

DATAOS-4175 / DATAOS-4346:

> Repo extract (same content): `release/2026-08/argus-glossary-context-cards-gaps.md`
>
> Broader matrix (Search/Lineage/Ask): `release/2026-08/ui-argus-requirements-gaps.md`

Those paths are **not present** on `typesense` (or `main`).

DATAOS-4606 (grandchild via 4191) — only ticket that names the branch and a commit (verbatim):

> `7ae4320` on `typesense` — _Add Prometheus metrics for MCP instruments_ (2026-09-02).
>
> * `src/argus/api/middleware/metrics.py`
> * `src/argus/app.py`

DATAOS-4326 names Python symbols, not the repo:

> `TermRepo.add_synonym` / `add_relationship` insert blindly, and `TermService` did not pre-check duplicates
>
> Pre-check in `TermService.add_synonym` / `add_relationship` and raise `ConflictError` before insert.

### d) Code/docs vs written-down knowledge

**Written in Jira/Notion, missing or stale in `typesense` code/docs**

- `release/2026-08/argus-glossary-context-cards-gaps.md` and `release/2026-08/ui-argus-requirements-gaps.md` — cited as “repo extract”, absent from `typesense`.
- Unified “get entity by ARN for drawer” API — still described as missing (P0). `typesense` has catalog + search + lineage + joins, not one drawer payload.
- Perspective as a catalog `kind` — Notion/Jira say not shipped; no perspective collection in Typesense docs.
- Quality score / policy counts / usage analytics — explicitly “not in Argus catalog”.
- Steward / owner id → name+email resolution — Partial, delegated to Platform identity.
- DATAOS-4191 “Burn the Data Product MCP into Argus” has an empty Jira description, but `src/argus/app.py` already mounts an in-process FastMCP app.
- Page B still reasons about Postgres `search_documents` SQL (`argus-raw-quries`). `typesense` README says that design was replaced:

> The previous design mirrored search into a Postgres `search_documents` table with pgvector; that meant maintaining ranking, faceting and fusion by hand.

**Implemented in `typesense` code, not written (or still marked Partial) in Notion/Jira**

- HEAD `be023b9` adds `ai_instructions`, `ai_caveats`, `ai_synonyms`, `example_queries` to asset search hits. Notion Page A still says those fields are “**not** in default search projection.” Jira 4346 still has the checkbox unchecked.
- Four Typesense collections (`argus_data_products` / `argus_assets` / `argus_members` / `argus_terms`), app-side 384-d embeddings, alias+`INDEX_VERSION` — documented in `docs/typesense.md` and `docs/search-api.md`, never mentioned on these two Notion pages except the one “Typesense search has `alignment`” note.
- Harvest model: `POST /argus/api/v1/tenants/{tenant}/events`, delete-and-reinsert, deterministic `stable_id(arn)` — in README; Jira talks about Harvest at product level, not this implementation.
- MCP mount + `/mcp/metrics` split (4606) — in code; Notion gap pages never mention MCP.
- Glossary Terms API surface in README (`GET/POST /argus/api/v1/terms`) matches 4205/3865, but the branch name `typesense` is only written on 4606, not on 4346 (the ticket the commits actually use).

**Aligned across all three**

- Operating model “decentralized generation, centralized convergence” (3833) matches README: local Vulcan authorship, Argus harvest + glossary + search.
- August v1 Glossary = browse + Enrich, not create/status/audit (Notion + 3865 + 4205).
- Context Cards consume search projections, not a dedicated drawer API (Notion note + 4190/4346 + `src/argus/search/query/plan.py` `include_fields`).
- Vulcan → Argus events `plan_updated` / `run_finished` (4186) match README harvest trigger `POST …/events`.
