# Ingestion, explained simply

A plain-English guide to what we changed in ingestion, why it matters, and a
real example of each. Written so you can explain it to someone else without
opening the code.

---

## First, what "ingestion" actually means here

We pull data from four places — Jira tickets, Bitbucket code, GitHub, and
Notion pages. Ingestion is everything that happens between "we fetched a
ticket" and "the graph can answer a question about it."

Three jobs happen in that gap:

1. **Chop** the text into pieces small enough to work with.
2. **Understand** each piece — pull out the people, systems, decisions, and
   the relationships between them.
3. **Store** all of that so it can be searched and explained later.

Step 2 is where we used to spend money — an AI model reads each piece and
writes down what it found. Every change below is about doing that step less
often, more honestly, or not at all.

---

## 1. Don't pay twice for text that didn't change

### The simple idea

Imagine you have a 300-page book and you hire someone to summarise every page.
You pay them ₹10 per page. Next month you fix one typo on page 47.

**The old way:** you hand them the whole book again and pay ₹3,000 again.

**The new way:** you compare every page against last month's, find that only
page 47 changed, and pay ₹10.

That's it. That's the whole change.

### How it works

Every piece of text gets a fingerprint calculated from the text itself. Same
text, same fingerprint — always. So on a re-sync we just compare two lists of
fingerprints:

- Fingerprint already there → **keep it.** Don't touch it. It keeps its
  existing AI-extracted facts.
- Fingerprint is new → **process this one.** Only this one.
- Fingerprint has vanished → the text was deleted.

### Real result

| | Old | New |
|---|---|---|
| Re-sync a page nobody edited | 2 AI calls | **0** |
| Re-sync after editing one paragraph | 2 AI calls | **1** |

### The technical bit

**The fingerprint is the chunk's primary key.** When text is chopped into
pieces, each piece's id is derived from its own bytes:

```python
digest     = sha256(piece.encode("utf-8")).hexdigest()
occurrence = how_many_identical_pieces_seen_before_in_this_record
chunk_id   = uuid5(NAMESPACE_URL, f"{record_key}:{digest}:{occurrence}")
```

The `occurrence` counter matters: a document can legitimately contain the same
sentence twice, and without it both copies would collapse into one id.

Because the id *is* the content, "did this text change?" is a **set
comparison**, not a diff algorithm:

```python
kept       = existing & incoming     # leave completely alone
added      = incoming - existing     # insert as 'pending'
superseded = existing - incoming     # mark superseded, do not delete
```

A chunk in `kept` that already has `status = 'done'` **stays done**. It is
never re-queued, so the AI never sees it again. That single line is the entire
cost saving.

One subtlety we had to handle: a paragraph inserted above a chunk shifts its
*position* without changing its *text*. So `chunk_index` is updated for kept
chunks, but it never takes part in identity — position is ordering, content is
identity.

The call returns a `ChunkDiff(kept, added, superseded, reused_done)`, where
`reused_done` is literally "the number of AI calls this re-ingestion did not
have to make" — which is how we measure the saving instead of guessing at it.

### Why it's a bigger deal than it sounds

This saving happens **every single re-sync**, not once. If you sync daily, you
save it 365 times a year. A one-time fix saves you once; this compounds.

### The lucky part

This was much cheaper to build than we expected, because the fingerprints
already existed in the system — they were being calculated and then thrown
away. We just started comparing them instead.

---

## 2. A renamed file is not a new file

### The simple idea

You rename `report.docx` to `report-final.docx`. Nothing inside changed.

**The old way:** the system saw a file disappear and a different file appear.
So it deleted everything it knew about the old one and paid to re-read the new
one from scratch.

**The new way:** it notices the *contents* are identical, recognises this as a
rename, and just re-points the existing knowledge at the new name. Zero cost.

### The technical bit

The problem is that the chunk id is namespaced by `record_key`, and
`record_key` embeds the path:

```
bitbucket:{connection}:source_file:{repo}:{path}
```

So renaming a file changes `record_key`, which changes **every** chunk id in
that file, even though not one byte of text changed. The diff in section 1 then
sees "all new" and re-extracts everything.

The fix is to look up the predecessor by content, before diffing:

```sql
SELECT * FROM source_records
WHERE content_hash = ?          -- identical contents
  AND record_key  != ?          -- but a different key
  AND record_key LIKE ?         -- same provider + connection only
LIMIT 1
```

That `LIKE` prefix is a guard, not an optimisation — without it, an identical
file in a *different* repository would be mistaken for a rename.

When a predecessor is found, its key is passed in as `adopt_from`, and any
incoming chunk whose **text** matches an already-extracted chunk there is
inserted with `status = 'done'` instead of `'pending'`. New ids, adopted
results, zero AI calls.

### Where this came from

The system we studied classifies every re-ingested record into **four**
outcomes:

| Outcome | Meaning |
|---|---|
| Created | genuinely new |
| Updated | contents changed |
| Unchanged | nothing to do |
| **Moved** | same contents, new name |

We only had the first three. The fourth is the one that saves a rename from
becoming a full re-processing job.

---

## 3. Never delete the evidence

### The simple idea

Suppose the graph says: *"Darshan owns the CDC memory fix."*

You ask: "How do we know that?"

The system should be able to point at the exact sentence in the exact ticket
where it learned that. **That sentence is the proof.**

Now a page gets edited and that sentence is removed.

**The old way:** delete the sentence from storage. The fact "Darshan owns the
CDC memory fix" is still sitting in the graph — but now nothing can explain
*why*. The proof is gone.

**The new way:** mark the sentence as *superseded* — no longer current, but
still on record. The fact stays explainable.

### The technical bit

The change is one column and one rule:

```sql
ALTER TABLE source_chunks ADD COLUMN superseded_at TEXT;

CREATE INDEX idx_source_chunks_live
    ON source_chunks(record_key) WHERE superseded_at IS NULL;
```

Every read filters on `superseded_at IS NULL`, so superseded rows are invisible
to normal operation but still reachable when something needs to explain itself.
The partial index means keeping history costs nothing at query time — live
lookups never scan the dead rows.

Nothing in the ingestion path issues `DELETE` against `source_chunks` any more.

### The one line worth memorising

> **"Deleting the evidence doesn't make the fact stale — it makes it
> unexplainable."**

That's the difference between "this information is a bit old" and "we have no
idea where this came from." The first is fine. The second destroys trust in the
whole system.

---

## 4. Remember what changed, not just that something changed

### The simple idea

**The old way:** the system stored a counter — *"this ticket has been updated
5 times."*

That tells you almost nothing. Updated how? From what to what?

**The new way:** every version is kept, in order, with its fingerprint and
timestamp. You can see what the content actually was at each point.

Think of it as the difference between a file that says "edited 5 times" and a
proper version history where you can open version 3.

### Real number

**1,058 record versions** currently stored. Before this change: zero.

### The technical bit

```sql
CREATE TABLE record_versions (
    record_key   TEXT    NOT NULL,
    version      INTEGER NOT NULL,
    content_hash TEXT    NOT NULL,
    ingested_at  TEXT    NOT NULL,
    PRIMARY KEY(record_key, version)
);
CREATE INDEX idx_record_versions_hash ON record_versions(content_hash);
```

Append-only: the insert is `INSERT OR IGNORE`, so re-running an ingestion can
never rewrite history or duplicate a version.

The index on `content_hash` is what makes the rename lookup in section 2 cheap
— it is the same hash, so the two features share one index.

---

## 5. Nothing gets thrown away silently

### The simple idea

The AI reads a ticket and proposes facts. Not all of them are usable — some
are malformed, some break our rules, some can't be verified.

**The old way:** five different places in the code quietly threw such facts
away. **Two of them didn't even count what they discarded.** So if you noticed
the graph was missing something obvious, there was no way to find out why. It
just wasn't there.

**The new way:** every single discard is written down — the full statement,
the reason it was refused, and the evidence it came from.

### Real result: 1,040 discards on record

| Reason | Count | What it means in plain words |
|---|---|---|
| `relation_not_allowed` | 490 | Our rulebook has no entry for this kind of relationship |
| `entity_no_connecting_fact` | 458 | We found a thing, but nothing useful to say about it |
| `evidence_not_in_chunk` | 71 | The AI's quote doesn't appear in the source text — usually it paraphrased instead of quoting exactly, sometimes it made the quote up |
| `endpoint_unresolved` | 11 | One side of the relationship couldn't be matched to a real thing |
| `direction_corrected` | 10 | The relationship was backwards — we fixed it (see next section) |

### The technical bit

```sql
CREATE TABLE extraction_drops (
    record_key   TEXT NOT NULL,
    chunk_id     TEXT NOT NULL,
    reason       TEXT NOT NULL,   -- one of the five codes above
    subject_kind TEXT, subject_name TEXT,
    relation     TEXT,
    object_kind  TEXT, object_name  TEXT,
    detail       TEXT,            -- e.g. the quote that wasn't found
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_extraction_drops_reason ON extraction_drops(reason, created_at);
```

The whole refused statement is stored, not a counter — so a drop can be read
back, understood, and acted on.

**One design decision worth defending.** Rows are rewritten per
`(record_key, chunk_id)`, not appended. If they were appended, a chunk
re-extracted after we changed the rulebook would leave behind old counts
describing rules that no longer exist — and the table would slowly fill with
complaints about problems already fixed. Rewriting keeps it a picture of *now*.

The `evidence_not_in_chunk` check itself is deliberately forgiving about
formatting and strict about content:

```python
needle   = " ".join(evidence.casefold().split())    # normalise whitespace/case
haystack = " ".join(chunk_text.casefold().split())
return bool(needle) and needle in haystack
```

Empty evidence counts as missing, not as verbatim — otherwise a model that
returned nothing would pass the check.

### Why this matters to a CTO

That `evidence_not_in_chunk` row is the important one.

We ask the AI to quote the **exact sentence** it learned each fact from. Then
we check that the sentence is really there. **71 times it wasn't** — the AI had
reworded it, or produced a quote that simply wasn't in the text. Each of those
facts was refused rather than stored.

If we'd kept them, the graph would look fine and every one of those 71 facts
would trace back to a quote nobody ever wrote. That number was previously
invisible. Now it's a quality metric you can watch over time.

---

## 6. When the AI gets it backwards, fix it — don't throw it away

### The simple idea

Our rulebook says a decision applies to a system:

```
Decision  ──APPLIES_TO──>  System
```

But the AI keeps writing it the other way around, because that's how people
talk. English sentences like *"Nilus uses the flatten strategy"* naturally put
the system first.

**The old way:** the rule check fails → throw the fact away. Gone.

**The new way:** before giving up, try it the other way round. If *that*
direction is valid, **swap it and save it** — and record that we did.

### Real example from our data

```
The AI wrote:   System "Nilus"  ──APPLIES_TO──>  Decision "use flatten
                                                  strategy for MySQL CDC"

We saved it as: Decision "use flatten strategy for MySQL CDC"
                          ──APPLIES_TO──>  System "Nilus"

And recorded:   direction_corrected
```

That fact used to be lost completely. Now it's in the graph, correctly
oriented, and flagged so anyone can audit the correction.

### The technical bit

The rulebook already knows which kinds of thing each relationship connects, so
the check is just run twice:

```python
def resolve_direction(subject_kind, relation, object_kind):
    if is_relation_allowed(subject_kind, relation, object_kind):
        return AS_IS
    if is_relation_allowed(object_kind, relation, subject_kind):   # try reverse
        return SWAPPED
    return None                                     # neither — keep for review
```

Three outcomes, never two. `SWAPPED` writes the fact with its endpoints
exchanged, sets `direction_corrected = True` on the edge, and writes a trace
row. The correction is never silent — you can always ask the graph which of its
facts were flipped and why.

**A bug this produced, worth knowing about.** There is a step before writing
that works out which entities are actually referenced by a fact, so unreferenced
ones can be dropped. It originally checked the direction *as stated*. So for a
swappable fact, both endpoints looked unreferenced, the entities were dropped
first, and the fact then died later at "endpoint unresolved" — the swap never
got a chance to run. That pre-pass had to learn about `resolve_direction` too.

### The best part of this story

The team we learned this from wrote in their notes that they tried **three
rounds of prompt tuning** to stop the AI doing this. It didn't work — the
English pattern is just too strong. So they stopped trying to fix the AI and
fixed the *write path* instead.

That's a genuinely useful lesson: **some AI behaviour isn't a prompt problem.
Accept it and handle it downstream.**

---

## 7. "No relation is no relation"

### The simple idea

Sometimes the AI finds two things that are clearly related, but the
relationship doesn't fit any category we have.

There are three possible responses:

| Response | Verdict |
|---|---|
| Throw it away | We lose real information |
| Invent a vague `related_to` link | **Worse** — we've now put a claim in the graph that nobody actually made |
| Keep the statement for review | Correct |

We do the third. The full statement and its evidence are kept, unfiled, where
a human can look at it.

### The technical bit

When `resolve_direction` returns `None`, the full triple plus the sentence it
came from goes to `extraction_drops` under `relation_not_allowed`, and the
shape is counted in `ontology_misses`. Nothing is written to the graph.

The temptation is to add a generic `RELATES_TO` edge so the connection isn't
lost. We don't, and the reason is that the graph cannot distinguish a real
relationship from a placeholder once both are edges. A missing edge is a gap
someone can find. A fake edge is a claim that will be read, cited, and
repeated.

There is no catch-all relation anywhere in the vocabulary — that is checked by
a test, so nobody can add one later "just so the connection isn't lost".

**We got this partly wrong the first time.** The write path stored the triple
but not the evidence, so 490 live rows read `Term GA -APPLIES_TO-> System
Nilus` with no sentence attached. Kinds and names alone cannot answer the one
question the row exists for — *is our ontology too narrow, or was the model
wrong?* — because that judgement needs the quote. Fixed, and pinned with a
test. Rows written before the fix still have no evidence; they will fill in as
those chunks are re-extracted.

### The principle

> **An unnamed relationship is honest. An invented one is a lie the graph
> will repeat forever.**

---

## 8. The rulebook is data now, not code

### The simple idea

The rules about which relationships are valid used to be a hardcoded list
inside the program. Two problems:

1. **Changing the rules meant changing code and redeploying.**
2. The list could only say *"this is allowed"* — nothing more.

Now the rules live in a table. And they can say far more than "allowed":

| Property | What it means | Example |
|---|---|---|
| **Functional** | Only one at a time | A ticket has one assignee, not five |
| **Transitive** | Chains automatically | A contains B, B contains C → A contains C |
| **Symmetric** | Works both ways | If A is the same person as B, B is the same person as A |
| **Temporal: event** | Happened at a moment | A commit was authored on 18 August |
| **Temporal: state** | True over a period | Someone was assigned from January until now |

That last distinction — event vs state — sounds academic but it's load-bearing.
It's how the system knows that *"what was worked on in August"* should include
an assignment that started in January and is still open, but should **not**
include a commit made in January.

### Real numbers

**37 rules**, covering **17 relationship types**. Before: a flat list with 6.

### The technical bit

```sql
CREATE TABLE relation_axioms (
    relation        TEXT NOT NULL,
    subject_kind    TEXT NOT NULL,
    object_kind     TEXT NOT NULL,
    extractable     INTEGER NOT NULL DEFAULT 0,   -- may the AI assert it?
    functional      INTEGER NOT NULL DEFAULT 0,   -- at most one at a time
    is_transitive   INTEGER NOT NULL DEFAULT 0,
    is_symmetric    INTEGER NOT NULL DEFAULT 0,
    is_asymmetric   INTEGER NOT NULL DEFAULT 0,
    inverse_of      TEXT,
    sub_property_of TEXT,
    temporal        TEXT NOT NULL DEFAULT 'state', -- state | event | eternal
    PRIMARY KEY(relation, subject_kind, object_kind)
);
```

The primary key is the **triple**, not the relation, because one relationship
can legitimately connect several different pairs of kinds — a repository
contains source files *and* commits, and each pair is its own row with its own
axioms.

**The design decision that made this safe** is the `extractable` column. It
separates two questions that used to be the same question:

| Question | Answered by |
|---|---|
| May the AI assert this relationship? | `extractable = 1` — only 22 rows |
| What are this relationship's properties? | the axiom columns — **all 37 rows** |

That split is what let us add the 15 structural relationships — the ones set by
system fields rather than by the AI, like ticket assignment and file authorship
— so that deduction and consistency checks can reason about them, **without**
giving the AI permission to invent them.

The rules are loaded once and cached, and the function every caller uses kept
its exact old signature — so both call sites in the extraction path were
untouched by the migration.

### The safety check — say this one out loud

We had to prove that moving the rulebook out of code didn't accidentally change
what the AI is allowed to claim.

So we didn't sample. We tested **every possible combination**: 12 kinds of
thing × 17 relationships × 12 kinds of thing = **2,448 combinations**, old
rulebook versus new.

**Zero differences.**

The new rulebook describes more, but permits exactly the same things.

---

## 9. The system now tells us what our rulebook is missing

**This is the most impressive thing to show, and it's the easiest to
understand.**

### The simple idea

Every time the AI proposes a relationship shape we don't have a rule for, we
don't just refuse it — we **count it, with an example.**

After a while, that count becomes a ranked list of exactly what our rulebook is
missing, ordered by how much it's costing us.

### The real list

| Missing shape | Real example | Times it came up |
|---|---|---|
| `System → APPLIES_TO → Term` | PostgreSQL → WAL_LEVEL | **91** |
| `Document → DEFINES → System` | Snowflake doc → Snowflake | **84** |
| `Term → APPLIES_TO → System` | GA → Nilus | **72** |
| `Document → APPLIES_TO → System` | CDC Cost Estimation → Nilus CDC | **61** |
| `Term → CAVEAT_OF → System` | CVE-2025-54121 → Starlette | **46** |

### The headline

- **25** relationship shapes are missing from our rulebook
- **490** facts are currently being dropped because of them
- Adding just the **top 5** would recover **354** of them

### The technical bit

```sql
CREATE TABLE ontology_misses (
    kind          TEXT NOT NULL,     -- what sort of gap (e.g. relation_type)
    key           TEXT NOT NULL,     -- 'System -APPLIES_TO-> Term'
    example       TEXT,              -- 'PostgreSQL -> WAL_LEVEL'
    count         INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    dismissed_at  TEXT,              -- a flag, never a DELETE
    PRIMARY KEY(kind, key)
);
```

The primary key is `(kind, key)`, so a repeat sighting is an **upsert that
increments `count` and moves `last_seen_at`** — one row per distinct gap, no
matter how many times it occurs. That is what turns noise into a ranked list.

`dismissed_at` being a nullable timestamp rather than a deleted row is the
whole point: a dismissed gap is still in the table, so the next sync recognises
it and leaves it dismissed.

### Why this lands with a CTO

Before, these facts vanished without trace. Now the system writes its own
**ranked product backlog** — and each item comes with a cost attached. You're
not guessing what to build next; the data is telling you, with a number.

### One more detail worth mentioning

When a human dismisses one of these suggestions, we mark it dismissed — we
don't delete the row.

The team we learned from shipped the delete version first, and discovered the
very next sync just proposed the same term again. In their words:

> **"The user's 'no' did not survive one round of extraction."**

We built it as a flag from day one, because they'd already paid for that
mistake.

---

## 10. New facts without calling the AI at all

### The simple idea

If the rulebook says `PARENT_OF` is **transitive**, then the system can work
out new facts by itself — no AI, no cost.

```
Known:    DATAOS-3867  is under  DATAOS-3840
Known:    DATAOS-3840  is under  DATAOS-3833
Worked out: DATAOS-3867 is under DATAOS-3833
```

Nobody wrote that third line anywhere. The system deduced it from the first two
and one rule.

Now "which epic is this ticket under?" can be answered across a whole chain.

### The safety rules

Deduction can easily go wrong, so it's fenced in:

- **Something actually written down always beats something deduced.** If a real
  source says otherwise, the real source wins.
- **Dates are the overlap of both inputs.** If A was true Jan–Mar and B was true
  Feb–Apr, the conclusion is only true Feb–Mar.
- **Confidence is the weakest link.** A chain is only as good as its shakiest
  step.
- **If it stops early, it says so.** It never quietly deduces less and lets you
  think there was nothing to find.

### The technical bit

Rules are compiled from the axiom columns — declare `PARENT_OF` transitive once
and the closure follows with no new code.

The closure itself is a breadth-first walk over edge chains:

```
frontier = every pair of edges where first.to == second.from   (paths of length 2)
loop while frontier is not empty and depth <= MAX_DEPTH:
    for each chain (first ... last):
        if first.from == last.to:  skip          # a cycle, not a fact
        if (first.from, last.to) already asserted or derived:  skip
        emit conclusion, extend the chain by one, push to next frontier
    depth += 1
```

Three guards inside that loop are what keep it honest:

**Cycles are skipped, not derived.** A loop in the data would otherwise produce
`A → A`, which is true of nothing.

**Dates are intersected:**

```python
valid_at   = max(all premise start dates)
invalid_at = min(all premise end dates)
if valid_at and invalid_at and valid_at >= invalid_at:
    return None                       # empty window -> derive nothing at all
```

That last line matters more than it looks. The alternative — writing the edge
with no dates — would assert the conclusion holds *always*, which is exactly
what the premises deny. Deriving nothing is the only honest option.

**Confidence is `min()` of the premises.** A chain is only as trustworthy as its
weakest link, so confidence can never go up as a chain gets longer.

And when `MAX_DEPTH` or the per-relation cap is hit, the function returns
`capped=True` alongside its results, so the caller can say so out loud. Silent
truncation would make "we derived fewer" and "nothing satisfies this rule" look
identical in the output.

### The bug this produced, and how we caught it

The first version wrote derived edges with an empty provenance list. Every read
path in the product does:

```cypher
UNWIND coalesce(r.source_record_keys, []) AS source_key
```

An empty list unwinds to **zero rows**. So 21 perfectly correct relationships
existed in the database and were invisible everywhere — the entity panel, the
chat evidence, the graph view. Present in storage, absent from the product.

The fix: a conclusion's provenance is the **union of its premises' provenance**.
Pinned with a regression test, because this is exactly the kind of thing that
rots silently.

### Why this matters commercially

We switched off AI extraction for three of our four sources to save money. That
saved a lot — but it also cost us the "why." This deduction engine wins some of
that reasoning back **at zero marginal cost.**

### Honest status

Measured and working: **34 deduced relationships** on our evaluation graph. But
it's still behind a switch and not permanently on.

---

## 11. Sending 16 things in one envelope instead of 16 envelopes

### The simple idea

To make text searchable, every piece is converted to numbers by an external
service. We were making **one API call per record**.

659 records = 659 calls. The service rate-limits us, so we spent most of the
time waiting in a queue rather than doing work.

**The fix:** put up to 16 records in each call.

**659 calls → 41 calls.**

### The second bug, which was worse

There was no time limit on these calls. If the service hung, we'd wait — and
the built-in retry meant a single record could block things for **30 minutes.**

We saw this live: a sync frozen for 11 minutes with the CPU completely idle.
Not slow. **Waiting.**

We set an explicit 30-second limit with 2 retries. Worst case per record went
from 30 minutes to **90 seconds.**

### The technical bit

A batch object is held in a `ContextVar`, opened before the first write of a
sync and closed on **every** exit path — success, failure, and cancellation.
Records accumulate until either limit trips:

```python
BATCH_RECORDS      = 16
BATCH_TOKEN_BUDGET = 100_000

def add(self, uid, label, content, name):
    self.rows.append((uid, label, content, name))
    self._tokens += len(content) // 4        # cheap stand-in, see below
    if len(self.rows) >= self.max_records or self._tokens >= self.token_budget:
        self.flush()
```

`len(content) // 4` is a deliberately rough token estimate. It only decides
*when* to flush; running a real tokeniser on every record to make the guard
exact would cost more than the guard saves.

Each flush sends **both** vectors for every record in one array — all the
contents first, then all the names — so the response can be split back apart by
position:

```python
inputs = [content for ... in rows] + [name for ... in rows]
# response.data[i] is row i's content vector
# response.data[len(rows) + i] is row i's name vector
```

**One safety detail.** `close_batch()` runs inside failure handlers too. If the
embedding service is what broke the sync, a flush error raised from there would
replace the original exception and hide the real cause. So the close logs its
error and swallows it — the sync fails for the reason it actually failed.

### The part worth admitting out loud

**My first diagnosis was wrong.** I assumed the AI conversion was slow. I
profiled it to be sure, and the profile disproved me:

| Step | Actual time |
|---|---|
| Converting text to numbers | 1.3 s |
| Chopping text into pieces | 0.019 s |
| Writing a complete record | 0.738 s |

That adds up to a capability of **81 records/minute**. We were observing
**3–5**. So the work itself was never the problem — the *waiting* was.

Saying this to a CTO is a feature, not a weakness. It shows the number came
from measurement, not from a guess that happened to sound right.

---

# The numbers, and how we got each one

| | Before | After | How we did it | Detail |
|---|---|---|---|---|
| **Unchanged document re-sync** | 2 AI calls | **0** | Fingerprint every piece of text from its own content. On re-sync, compare fingerprints instead of deleting and redoing — unchanged text keeps the facts already extracted from it. | §1 |
| **AI calls per large sync** | 659 | **41** | Batch up to 16 records into a single request instead of one request per record. Fewer round trips, far less rate-limit queueing. | §11 |
| **Worst-case stall, one record** | 30 min | **90 s** | The network call had no time limit, and retries multiplied it. Set an explicit 30-second timeout with 2 retries. Found by profiling a live frozen sync — the CPU was idle, so it was waiting, not working. | §11 |
| **Discards explainable** | 0% | **100%** (1,040 rows, 5 reasons) | Five places in the code threw facts away, two of them silently. Every one now writes a row with the full statement, a reason code, and its evidence. | §5 |
| **Rulebook entries** | 0 | **37** | Moved the rules out of code and into a table, so they can carry properties — functional, transitive, symmetric, and whether something is a moment or a period. Proved safe by testing all 2,448 possible combinations against the old rules: zero differences. | §8 |
| **Version history** | none | **1,058 records** | Replaced a "times changed" counter with an append-only log of every version, each with its content fingerprint and timestamp. | §4 |
| **Full-corpus ingestion cost** | ≈ $6.81 | **≈ $0.56** | Three of the four sources stopped using AI extraction entirely and now use direct field mapping. Only the fourth still pays for AI. The rest is search-index cost, which is tiny. | §1, §11 |
| **Automated tests** | 53 | **144** | Every change above landed with tests next to it. Includes one that checks all 2,448 rulebook combinations, and one that proves the deduction engine never overrides a real recorded fact. | all |

---

# If you only remember four things

**1. We stopped paying twice for text that didn't change.**
Re-syncing an unedited page went from 2 AI calls to zero. That saving repeats
on every sync, forever.

**2. Nothing disappears silently any more.**
1,040 refused facts are on record with reasons. Including 71 times the AI cited
a sentence that didn't exist — caught and refused.

**3. The system tells us what to build next.**
25 missing rulebook entries, ranked by cost. The top 5 would recover 354 facts.
That's a backlog the data wrote by itself.

**4. We didn't invent any of this — we borrowed failures somebody else had
already paid for.**
We read another team's architecture decision records and post-mortems. Not one
line of their code — it's a different language and a different database. What
we took were their **mistakes**, already made and already documented.

And applying one of their lessons found a bug in our own system that our own
tests had missed.
