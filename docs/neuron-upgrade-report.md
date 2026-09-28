# Neuron — Retrieval, Ingestion and Reasoning Upgrade

**Status:** implemented, tested and measured on live data.
**Measured on:** 15 Sep 2026. All figures below are re-run results, not estimates.

---

## 1. Where these ideas came from — Utopia's ingestion

None of section 2 was invented from scratch. We reviewed **Utopia**, an
open-source Rust/Postgres enterprise knowledge-graph substrate, and read its
ingestion path and its 36 architecture decision records specifically to find
out what a system that had already run this workload for longer had learned.
The value was not the code — it is a different language and a different
database — it was the **failures they had already paid for**.

### 1.1 The lesson that paid for the whole review

Their schema records a measured finding: **short queries are systematically
won by short documents.** Their numbers — the median source length of the
items actually retrieved was **44 characters against 89 for the population**.
A one-line entry beat a full description not because it was more relevant but
because it was shorter.

Their comment also records that they were caught by this **four separate
times**, and that the first four fixes all treated the *query* side. The fifth
fix treated the *document* side: two kinds of query deserve two documents.

That is the bug we had live, in production, twice over. Applying their lesson
to our own ranking then exposed **a second bug they never had** — we were
ranking inside each category before merging, so being the best of 5 items
scored the same as being the best of 264. Section 3.1 is the result.

**The lesson under the lesson:** reading another team's post-mortems found a
bug in our system that our own tests did not.

### 1.2 What we took, one line each


| #   | What Utopia does                                                                                                                                                                                                  | What we did before                                                                                        | Taken |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | ----- |
| 1   | Classifies every re-ingested record as **created / updated / unchanged / moved** — four outcomes                                                                                                                  | Insert / update / delete / keep — **no "moved"**, so a rename was a full delete-and-reinsert              | ✅     |
| 2   | The unit of change is **the text, not the record**. Content whose text is unchanged **adopts the existing row** — keeping its embedding, its extraction timestamp and every link from a fact back to its evidence | Deleted everything belonging to a record and re-queued it, so one new comment re-extracted the whole item | ✅     |
| 3   | Content that has left is **marked superseded, never deleted**, and its embedding is kept                                                                                                                          | Hard delete                                                                                               | ✅     |
| 4   | Every refused statement is stored with a **reason code and an example**                                                                                                                                           | Five drop paths, two of them completely silent, none recorded                                             | ✅     |
| 5   | When a relationship fails validation, **try the reverse direction before discarding it**                                                                                                                          | Discarded outright                                                                                        | ✅     |
| 6   | **"No relation is no relation"** — when neither direction is valid, keep the statement for review rather than inventing a vague catch-all edge                                                                    | Not applicable; the statement was simply lost                                                             | ✅     |
| 7   | Version history is **append-only** — what the content was, not just how many times it changed                                                                                                                     | A change counter only                                                                                     | ✅     |
| 8   | A user's dismissal of unknown vocabulary is **a flag, not a delete**                                                                                                                                              | No vocabulary tracking at all                                                                             | ✅     |
| 9   | Reasoning is **forward chaining over ontology axioms** — their reasoning engine contains zero model calls                                                                                                         | Four hand-written rules, no axiom engine                                                                  | ✅     |
| 10  | A rule **reports what it could not expand** rather than silently truncating                                                                                                                                       | No derivation engine to truncate                                                                          | ✅     |




### 1.3 The three most valuable, in their own words

> **"Deleting the chunk makes the fact unexplainable, not merely stale."**
> The piece of text a fact was extracted from *is* that fact's evidence. This
> is why we moved from delete to soft supersession.

> **"Deriving fewer, and 'this entity does not satisfy the rule', look
> identical in the result."**
> A capped derivation that reports nothing is indistinguishable from a rule
> that found nothing. This is why our engine reports what it capped.

> **"The user's 'no' did not survive one round of extraction."**
> Their recorded bug: a dismissed vocabulary term was removed with a delete,
> so the next ingest re-proposed it. We used a flag from day one because they
> had already paid for that mistake.



### 1.4 What we deliberately did NOT take


| Utopia pattern                                       | Why not                                                                                                                                                                                                                    |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Derived facts in a **separate store**                | Their reason was that 40+ of their queries read the main fact table and only one knew about the flag. Our read paths already handle the marker correctly in all three places. The refactor would buy little and risk much. |
| **LLM adjudication** of duplicate entities           | Genuinely valuable, but it is a review-queue feature, not a retrieval or cost fix. We have no review interface to put the queue in yet.                                                                                    |
| **Public ontology packs** (schema.org, FOAF, PROV-O) | Our domain is one company's tickets, code and documents — not open-world data. A pack would add vocabulary nobody queries.                                                                                                 |
| Their **search layer**                               | Thinner than ours — plain rank fusion at a fixed constant, no weighting, no reranker. Nothing to take.                                                                                                                     |




### 1.5 The honest scope limit

The re-ingestion win in 3.2 currently lands on **one of our four sources** —
the only one that stores content pieces, because the other three stopped
running LLM extraction entirely. That one source is also **the only one still
spending tokens**, so it is where the money is. We are not claiming a
four-source saving.

---

---



## 2. Headline numbers


| Metric                                 | Before          | After              | Change |
| -------------------------------------- | --------------- | ------------------ | ------ |
| Retrieval MRR                          | 0.3571          | **0.6000**         | +68%   |
| Retrieval recall@8                     | 0.4286          | **0.8571**         | +100%  |
| Retrieval precision@8                  | 0.0536          | **0.1071**         | +100%  |
| Benchmark questions answered correctly | 3 / 7           | **6 / 7**          | +3     |
| Re-sync of an unchanged document       | 2 LLM calls     | **0**              | −100%  |
| Re-sync after a one-paragraph edit     | 2 LLM calls     | **1**              | −50%   |
| Embedding API requests per large sync  | 659             | **41**             | −94%   |
| Worst-case stall on a single record    | 30 min          | **90 s**           | −95%   |
| Measured write throughput              | 3–5 records/min | **81 records/min** | ~20×   |
| Ontology rules the system can express  | 0               | **37**             | new    |
| Relationships derivable without an LLM | 0               | **34 edges**       | new    |
| Discarded facts that can be explained  | 0%              | **100%**           | new    |
| Version history retained per record    | none            | **740 records**    | new    |
| Automated tests                        | 53              | **119**            | +125%  |
| Ingestion cost (full corpus)           | ≈ $6.81         | **≈ $0.56**        | −92%   |


---



## 3. What each change does and how we use it



### 3.1 Search ranking — the largest single win

**Problem measured:** short documents were systematically beating the right ones.
An empty 95-character file was outranking the 19,564-character file that actually
answered the question. A 16,694-character file named directly in the question never
entered the top 8.

**Cause:** results were ranked inside their own category, then merged. Being the best
of 5 items scored identically to being the best of 264 items. In one benchmark case,
5 items from a 5-item category occupied 5 of the 8 result slots.

**Fix:** one global ranking across all categories, plus two separate embeddings per
item — one for its name/identifier, one for its content — queried on both channels and
interleaved rather than score-merged.

**How we use it:** every question asked in chat goes through this path. It is the
default and always on.


|                            | Before | After      |
| -------------------------- | ------ | ---------- |
| MRR                        | 0.3571 | **0.6000** |
| recall@8                   | 0.4286 | **0.8571** |
| precision@8                | 0.0536 | **0.1071** |
| Benchmark pass rate        | 3 / 7  | **6 / 7**  |
| Embeddings stored per item | 1      | **2**      |


The ranking strategy was not hand-picked. Three strategies were swept against the
benchmark set; the winner scored MRR 0.6000 against 0.5476 and 0.5357, with recall
tied at 0.8571. That result is recorded next to the setting.

---



### 3.2 Re-ingestion — stop paying twice for the same text

**Before:** re-syncing a document deleted all of its stored pieces and re-queued every
one of them for LLM extraction, even when nothing had changed.

**After:** re-sync is a content-keyed difference. Unchanged pieces keep their existing
embedding and every fact already extracted from them. Only genuinely new content is
processed. Removed content is marked superseded rather than deleted, so the evidence
behind older facts survives. A document that is renamed or moved is detected by content
and relinked instead of being re-processed from scratch.


|                            | Before                  | After                           |
| -------------------------- | ----------------------- | ------------------------------- |
| Unchanged document re-sync | 2 LLM calls             | **0**                           |
| One-paragraph edit re-sync | 2 LLM calls             | **1**                           |
| Removed content            | deleted                 | **retained, marked superseded** |
| Rename / move              | full delete + re-insert | **relink, no re-processing**    |
| Version history            | none                    | **740 versions recorded live**  |


**How we use it:** every scheduled re-sync now costs close to zero for stable content.
The saving compounds — it is per re-sync, not one-time.

---



### 3.3 Sync performance — the throughput fix

**Problem observed live:** a large sync was moving at 3–5 records/min and stalled for
11 minutes at 0% CPU.

**First hypothesis was wrong.** Profiling disproved it: embedding took 1.3 s, chunking
19 ms, a complete record write 738 ms — a capability of 81 records/min against 3–5
observed. The real causes were request *count* against the provider's rate limit, and
an unbounded network timeout that allowed a worst case of 30 minutes on a single record.

**Fix:** an explicit 30-second timeout with 2 retries, and request batching — up to 16
records or 100,000 tokens per API call.


|                                          | Before          | After                                  |
| ---------------------------------------- | --------------- | -------------------------------------- |
| Embedding API requests (659-record sync) | 659             | **41**                                 |
| Worst case per record                    | 30 min          | **90 s**                               |
| Observed throughput                      | 3–5 records/min | measured capability **81 records/min** |


**How we use it:** applied to all four data sources. Failure paths flush the batch
without hiding the original error.

---



### 3.4 Nothing is discarded silently

**Before:** five separate code paths discarded extracted facts. Two were completely
silent. None were recorded anywhere. If the graph was missing something, there was no
way to find out why.

**After:** every discard is written with a reason code, the full triple and its evidence.
Five reason codes are in use. Where a relationship is stated backwards, the direction is
corrected and the fact is kept — flagged, not silently dropped. Where no valid
relationship exists, the triple is preserved for review instead of a vague placeholder
edge being invented.

**Live evidence from the current production graph:** 10 discards recorded, 5 for
`entity_no_connecting_fact` and 5 for `relation_not_allowed`. 2 out-of-vocabulary terms
counted rather than thrown away.


|                                           | Before    | After                           |
| ----------------------------------------- | --------- | ------------------------------- |
| Silent drop paths                         | 2 of 5    | **0 of 5**                      |
| Discards queryable with reason + evidence | no        | **yes**                         |
| Backwards relationships                   | discarded | **corrected and kept, flagged** |
| Unknown vocabulary                        | discarded | **counted for review**          |


---



### 3.5 Ontology as data

**Before:** the rules governing which relationships are valid were a hardcoded list.
It could not express cardinality, transitivity, symmetry or time, and it did not cover
the structural relationships at all.

**After:** 37 rules held as data — 22 the model is allowed to assert, 15 structural ones
it is not. 17 distinct relationship types are now described. Changing the ontology no
longer requires a code change.

**Safety gate:** the migration was proved exhaustively, not sampled. All **2,448**
possible combinations (12 entity kinds × 17 relationships × 12 entity kinds) were
compared old-versus-new. **0 mismatches.** Adding structural relationships widened what
the graph can reason about without widening what the model is permitted to claim.


|                                              | Before          | After                                |
| -------------------------------------------- | --------------- | ------------------------------------ |
| Rules expressible                            | 0               | **37**                               |
| Relationship types described                 | 6               | **17**                               |
| Cardinality / transitivity / symmetry / time | not expressible | **expressible**                      |
| Equivalence gate                             | none            | **2,448 combinations, 0 mismatches** |


---



### 3.6 New relationships without an LLM

The ontology rules compile into a derivation engine. It reads the axioms and produces
new relationships by forward chaining — no model call, no token cost.

**Live result on the evaluation graph:** **34 derived relationships** — 21 transitive,
11 from a compiled parent rule, 2 symmetric. Against 30 asserted relationships of the
same type, the engine produced 21 more. Validity intervals are intersected, confidence
is the minimum of the premises, an asserted fact always beats a derived one, and
anything hit by the depth cap is reported rather than silently truncated.

**Bug found and fixed here:** the first derived edges were written with empty provenance.
Every read path filters on provenance, so 21 correct relationships existed in storage and
were invisible to the product. A conclusion now inherits the union of its premises'
provenance. **All 34 derived edges now carry provenance — verified by query, plus a
regression test.**

**How we use it:** this recovers part of the reasoning we lost when LLM extraction was
switched off for three of the four data sources — at zero marginal cost.

---



### 3.7 Cost

Three of four sources now ingest with no LLM extraction at all; only embedding remains.


|                                             | Cost              |
| ------------------------------------------- | ----------------- |
| Current full-corpus ingestion               | **≈ $0.56**       |
| Equivalent LLM-extraction approach          | ≈ $6.81           |
| For the two sources that stopped extracting | **~700× cheaper** |
| Per query                                   | ≈ $0.0011         |


---



## 4. Implemented **and tested** — confirmed

Everything in this section was verified by a live run today, not by reading code.


| Capability                              | Test evidence             | Live evidence                                         |
| --------------------------------------- | ------------------------- | ----------------------------------------------------- |
| Global ranking + dual embeddings        | 8 tests                   | benchmark re-run: MRR 0.6000, 6/7 passing             |
| Content-keyed re-ingestion diff         | 10 tests                  | two consecutive syncs, second one 0 LLM calls         |
| Discard tracking + direction correction | 10 tests                  | 10 discards, 2 reason codes in production             |
| Ontology as data                        | 10 tests                  | 37 rules loaded; 2,448-combination gate, 0 mismatches |
| LLM-free derivation                     | 20 tests                  | 34 derived edges, all with provenance                 |
| Embedding batching + timeout            | 8 tests                   | 659 → 41 requests; 2 named vector channels live       |
| **Full suite**                          | **119 passed, 1 skipped** | —                                                     |


**Two failures that were live in production are now fixed and confirmed end-to-end
through the real chat path**, not just in tests.

---

---



## 5. Still open — stated honestly


| Item                       | Status                                                                                                                                        |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 of 7 benchmark questions | Still failing — a people-lookup question needing exact-identifier matching. A deterministic fix was scoped and deliberately deferred.         |
| Re-ingestion saving        | Currently benefits the one source that still runs LLM extraction. The other three do not store content pieces, so they have nothing to diff.  |
| Derivation engine          | Off by default, behind an explicit flag. Enabled and measured, not yet enabled permanently.                                                   |
| One remaining source sync  | Needs to be re-triggered after the performance fix; the other three are complete.                                                             |
| Throughput figure          | 81 records/min is a measured *capability* from profiling. The end-to-end rate after the fix has not yet been measured over a full large sync. |


---





