# Utopia → Neuron

What we took from [Utopia](https://github.com/deeplethe/utopia) (DeepLethe's
open-source enterprise knowledge-graph substrate, Rust + Postgres), what it
fixed in Neuron, and what we deliberately left behind.

Every number below is measured on the `less_token` graph (400 source records:
264 Bitbucket files, 100 commits, 29 Jira work items, 5 Notion pages), not
estimated. Where a claim comes from Utopia, the file or ADR is cited.

---

## Why we looked at it

Neuron had two retrieval failures we had reproduced by hand but not
explained:

1. "which file handles the Vulcan client" returned an **empty**
   `src/argus/clients/vulcan/__init__.py` (95 bytes) instead of the real
   `src/argus/clients/vulcan/client.py` (19,564 bytes).
2. "what does `src/argus/schemas/lineage.py` do" never retrieved that file at
   all, and answered "the evidence does not include the file's contents".

Utopia had hit the same bias **four times** and written down the fix
(`migrations/0003_graph.sql:3-13`):

> Short queries were taken over by tautological classes (`Park\nA park.` wins
> on *length*, not semantics: the median source length of retrieved classes
> was **44 vs 89** for the population). "The short side systematically has
> smaller distances" has bitten this repo four times.

That made the review worth doing properly.

---

## Results

| | Before | After |
|---|---|---|
| Retrieval MRR | 0.3571 | **0.6000** |
| Retrieval recall@8 | 0.4286 | **0.8571** |
| Retrieval precision@8 | 0.0536 | **0.1071** |
| Golden cases passing | 3 / 7 | **6 / 7** |
| Re-sync of an unchanged page | 2 LLM calls | **0** |
| Re-sync after a 1-paragraph edit | 2 LLM calls | **1** |
| Discarded facts explainable | no | **yes** (5 reasons, queryable) |
| Ontology axioms expressible | 0 | **37** (transitive / symmetric / functional / temporal) |
| Facts derivable without an LLM | 0 rules | **34 edges** from 4 compiled rules |
| Tests | 53 | **119** |

Code: 14 files changed, +957 / −133, plus 8 new files.

---

## 1. Retrieval — two documents per node, one global ranking

**Utopia's lesson** (`migrations/0003_graph.sql:3-13`,
`crates/utopia-server/src/type_resolution.rs:203-218`): *"two kinds of query
deserve two documents"*, and **interleave channels, never merge them by
distance** — a short query against short text systematically scores higher,
so a score merge lets one channel monopolise every top slot.

**What Neuron had.** One embedding per node, over `name + full content`, and
`hybrid_search` looping per label.

**What we changed.**

- Qdrant points now carry two named vectors: `name` (the node's path/title
  alone) and `content` (the full `search_text`). Both are queried and the
  results **interleaved**, never score-merged.
- The vector leg became a **single global query** instead of one per label.
- Point payloads store `embedded_text` + `embedded_model` rather than an
  `embedded_at` timestamp — *a timestamp answers "was it embedded", not "is
  this embedding still of this text"*.

**A bug this exposed, which Utopia did not have.** Neuron computed RRF ranks
*per label*, so being the best of 5 `Document` nodes scored identically to
being the best of 264 `SourceFile` nodes — both `1/(10+0+1)`. Measured
consequence: for one question, **all 5 Documents in the entire graph**
occupied 5 of the top 8 slots. Cosine similarity *is* comparable across
labels, so the per-label split was pure loss.

**Measured, not assumed.** The fulltext fusion strategy was swept
(`scripts/sweep_retrieval_params.py`, now `--graph`-aware) across
k × vector_weight × per_method_limit × strategy. `global_score` won at the
production pool size (MRR 0.6000 vs 0.5476 / 0.5357, recall tied at 0.8571).

> Worth recording: that ranking **flipped**. Against the old single-vector
> leg, `global_score` was the *worst* option on recall (0.5714 vs 0.7143).
> The legs are not independent — re-sweep whenever either one changes,
> never carry the constant across.

---

## 2. Re-ingestion — reuse what did not change

**Utopia's mechanism** (`utopia-store/src/documents.rs::replace_chunks`): a
text-keyed claim/diff. A new chunk whose text matches a live one **adopts
that row**, keeping its embedding, its `extracted_at` and every
`fact_evidence` foreign key. Only genuinely new text is inserted; vanished
rows are soft-superseded, embeddings deliberately retained so as-of retrieval
still works.

**What Neuron had.** `DELETE FROM source_chunks WHERE record_key = ?`,
then re-insert everything as `pending`. One new comment on a ticket
re-extracted the entire record.

**What we changed.** `save_chunks` is now a diff. This turned out to be far
cheaper than expected, because `chunk_id` was *already* content-addressed —
`uuid5(record_key : sha256(text) : occurrence)` — so "did this text change"
is a set comparison, not a diff algorithm.

Verified against the real ledger:

| | Old | New |
|---|---|---|
| Unchanged page re-synced | delete 2, queue 2 | `kept=2, added=0` → **0 LLM calls** |
| One paragraph edited | queue 2 | `added=1` → **1 LLM call** |

Also taken:

- **Soft supersession** (`superseded_at`) rather than DELETE: the chunk a
  fact was extracted from is that fact's evidence, and deleting it makes the
  fact *unexplainable* rather than merely stale.
- **`record_versions`** — append-only `(version, content_hash, ingested_at)`.
  The ledger previously kept only `update_count`: it could say a record had
  changed five times but not what any earlier version was.
- **Rename detection** (Utopia's fourth ingest action, `Moved`). Neuron's
  `chunk_id` is namespaced by `record_key`, so renaming a file changed every
  chunk id even with byte-identical text — a full re-extraction for a rename
  alone. `find_moved_from` identifies the predecessor and the new chunks
  adopt its completed extractions.

**Honest scope**: this win currently lands on **Notion only**, the sole
caller of `save_chunks`. That is also the only provider still spending LLM
tokens, so it is where the money is.

---

## 3. Extraction drops — a count without a reason answers nothing

**Utopia's table** (`migrations/0003_graph.sql:416-430`): `extraction_drops`,
eleven reason codes, countable in the UI. Their note: the extractor had seven
`continue` statements that dropped facts invisibly.

**What Neuron had.** Five discard paths in `_write_extraction`: three bumped
a counter, **two were entirely silent**, and none survived the log line they
were written to.

**What we changed.** An `extraction_drops` table with five reason codes
(`relation_not_allowed`, `evidence_not_in_chunk`, `endpoint_unresolved`,
`entity_no_connecting_fact`, `direction_corrected`), rewritten per
`(record_key, chunk_id)` rather than appended — so a chunk re-extracted after
an ontology change does not leave behind counts describing rules that no
longer exist.

### Direction correction — salvage instead of reject

**Utopia's rule** (ADR 0012): the ontology declares
`employee (organization → person)`; the model still writes
`Musk employee Microsoft`, because English "X is an employee of Y" is too
strong a pattern. *Three rounds of prompt tuning could not suppress it.* So
the write path corrects it — swaps the endpoints, records
`direction_corrected`, never silently.

Neuron used to **discard** such a fact outright. Now `resolve_direction()`
returns `as_is` / `swapped` / `None`, and a reversed-but-valid fact is
written with its endpoints swapped, `direction_corrected=True` on the edge
and a trace row in the drops table.

Verified through the real write path with three deliberately broken facts:

```
written: 2 entities, 1 facts, 2 rejected
extraction_drops: {direction_corrected: 1, relation_not_allowed: 1,
                   evidence_not_in_chunk: 1, entity_no_connecting_fact: 1}

[direction_corrected] (System) 'Redis' -APPLIES_TO-> (Decision) 'switch to Redis'
     detail: written as (Decision) -APPLIES_TO-> (System)
```

That one fact was previously lost entirely.

A subtlety worth writing down: the `referenced` precomputation had to use
`resolve_direction` too. Otherwise a swappable fact's endpoints counted as
unreferenced, its entities were dropped first, and the fact died later at
"endpoint unresolved" — the swap would never have got a chance.

**Also taken — ADR 0010, "no relation is no relation".** When neither
direction is valid, the full triple and its evidence are kept in the drops
table. No vague `related_to` edge is invented: *an unnamed relation is
honest, an invented one is an assertion nobody made.*

---

## 4. The ontology as data

**Utopia's design** (`migrations/0003_graph.sql:49-86`): `relation_types`
rows carry the axioms as columns — `functional`, `is_transitive`,
`is_symmetric`, `is_asymmetric`, `inverse_of`, `sub_property_of`, and a
three-way `temporal` (`state` / `event` / `eternal`). Domain and range are
join tables, because OWL allows a relation to take several domains.

**What Neuron had.** `RELATION_TYPE_MAP`, a Python dict. It could say which
triples were allowed and nothing else — no transitivity, no cardinality, no
growth without a redeploy.

**What we changed.** A `relation_axioms` table (per graph, since the ledger
already resolves per graph name), seeded from code on first use.

**The design decision that made this safe** — two concerns kept apart:

- `extractable` — may the LLM assert this? Only the 22 semantic rows.
- the axioms — describe **every** relation, structural ones included,
  because that is what derivation and consistency checks read.

So the 15 structural relations (`ASSIGNED_TO`, `PARENT_OF`, `MODIFIES`,
`SAME_AS`, …) are now in the store for their axioms **without** widening what
the model may claim.

**Gate: 2,448 (subject, relation, object) triples checked exhaustively
against the old dict — 0 mismatches.** Extraction behaviour is unchanged.

Axioms the old dict could not express:

```
transitive : CONTAINS, PARENT_OF, SAME_AS, SUPERSEDES
symmetric  : SAME_AS
functional : ASSIGNED_TO, AUTHORED_BY, BELONGS_TO, REPORTED_BY
temporal   : MODIFIES / IMPLEMENTS / AUTHORED_BY = "event"
```

**`ontology_misses`** — a relation the ontology lacks is now **counted**
(with an example) instead of discarded. `dismissed_at` is a flag, never a
DELETE: Utopia shipped the delete first and found the next extraction simply
re-inserted the term, so *"the user's no did not survive one round of
extraction"* (`migrations/0004_ontology.sql:15-18`).

---

## 5. Derivation without an LLM

`crates/utopia-reason` contains **zero** LLM references — new relationships
come from forward chaining over ontology axioms. This matters for Neuron
specifically: LLM extraction is switched off for Jira/GitHub/Bitbucket, so
the "why" those sources used to contribute had no replacement.

`graph/inference.py` compiles rules from the §4 axioms — declare `PARENT_OF`
transitive once and the closure follows, with no new code.

**Real result:**

```
PARENT_OF:  30 asserted  ->  21 derived   (Jira epic chains)
CONTAINS:  369 asserted  ->   0 derived   (Repository→SourceFile has no chain)
asserted PARENT_OF: 30, never overwritten
```

e.g. `DATAOS-3867 → DATAOS-3840 → DATAOS-3833` derives
`DATAOS-3867 → DATAOS-3833`, so "which epic is this under" is now answerable
across a whole chain — with no LLM call.

Constraints taken verbatim in spirit from `utopia-reason/src/derive.rs`:

- **Asserted always wins** — an already-asserted triple is never derived, so
  "who said this" has one answer.
- **Validity is the intersection of the premises**; an empty intersection
  derives *nothing* rather than an undated edge.
- **Confidence is the minimum** of the premises — a chain is only as
  trustworthy as its weakest link.
- **Depth and per-relation caps, and what got capped is reported.** Utopia
  measured `part_of` going 185 → 828 without converging, with a depth
  histogram oscillating from level 5 — the shape of a cycle. Silent
  truncation makes "derived fewer" and "nothing satisfies the rule" look
  identical.
- **Off by default**, and idempotent (second run: 51 → 51 edges).

### The bug this phase produced, and how it was caught

The first implementation wrote derived edges with `source_record_keys: []`.
Every read path does `UNWIND coalesce(r.source_record_keys, [])`, and an
empty list yields **no rows** — so 21 edges existed in the database and were
invisible to the entity panel, to chat evidence and to the graph view.
Present in storage, absent from the product.

Fixed by making a conclusion's provenance the union of its premises'
provenance, and pinned with a regression test, because this is exactly the
kind of thing that rots silently.

---

## What we deliberately did NOT take

| Utopia pattern | Why not |
|---|---|
| **Derived facts in a separate table** | Their argument was that 40+ of their queries read `facts` and only one knew about the flag. Neuron's read paths already handle the marker correctly (`entity.py` gives derived its own bucket, `chat.py` renders `[inferred]`, `graph_view.py` passes it through). The refactor would buy little and risk much. |
| **LLM adjudication of duplicate entities** | Real value, but it is a review-queue feature, not a retrieval or cost fix. Neuron has no review UI to put the queue in yet. |
| **Ontology packs (schema.org, FOAF, PROV-O)** | Neuron's domain is one company's Jira/Git/Notion, not open-world RDF. A pack would add vocabulary nobody queries. |
| **Their search layer** | Thinner than Neuron's: plain RRF at k=60, no weighting, no reranker. Nothing to take. |

---

## Still open

- **"what does Animesh do"** is the one golden case still failing. The string
  is a username buried inside four large test fixtures — neither the name
  channel nor the content channel surfaces it, because whole-file embeddings
  dilute a small literal. This is a grep-shaped question, not a
  similarity-shaped one; the fix is a deterministic literal lookup in
  `graph/structured_query.py`, not more tuning.
- **Conflict detection** (`axiom_violations`, `ontology_defects`) is not
  built. The axioms it needs now exist, which was the blocker.
- **`PARENT_OF` direction** reads backwards in the graph: Jira's
  `issue.parent_id` produces `child -PARENT_OF-> parent`. Transitivity is
  unaffected (it is consistent), but the label is misleading and a direct
  "who is X's parent" question could be answered the wrong way round.

## How to reproduce the numbers

```bash
uv run pytest tests/ -q
uv run python -m scripts.evaluate_retrieval eval/less_token_golden.jsonl --graph less_token --k 8
uv run python -m scripts.sweep_retrieval_params eval/less_token_golden.jsonl --graph less_token
uv run python -m scripts.rebuild_vectors --recreate --graph less_token   # after any vector-shape change
```
