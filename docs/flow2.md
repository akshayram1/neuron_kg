# Ek record ka safar — andar jaana aur wapas nikalna

Do flow, ek hi asli Notion page par: **"All things Nilus!"**

**Part A** — wo page ingest hokar node kaise banta hai.
**Part B** — ek question poochhne par wahi page wapas kaise nikalta hai.

Har number live data se hai, banaya hua nahi.

---

# Part A — Ingestion

## Teen store, teen kaam

| Store | Kis sawaal ka jawab deta hai | Kya rakhta hai |
|---|---|---|
| **SQLite** | *"Hum yahan tak kaise pahunche?"* | hashes, versions, chunk text, drops, rulebook |
| **FalkorDB** | *"Kya sach hai?"* | nodes, edges, provenance |
| **Qdrant** | *"Isse milta-julta kya hai?"* | do vectors per node |

Ek line mein: **SQLite hisaab-kitaab hai, FalkorDB sach hai, Qdrant similarity hai.**

---

## Flow

```
Notion API
    │
    ├─ 1. bookkeeping row      ──────────────► SQLite  source_records
    ├─ 2. version history      ──────────────► SQLite  record_versions
    ├─ 3. text chop            ──────────────► SQLite  source_chunks
    │
    ├─ 4. node                 ──────────────► FalkorDB  (:Document)
    ├─ 5. edges                ──────────────► FalkorDB + SQLite record_edges
    ├─ 6. embeddings           ──────────────► Qdrant  (2 vectors)
    │
    └─ 7. jo refuse hua        ──────────────► SQLite  extraction_drops
```

---

## Step 1 · Fetch → SQLite mein bookkeeping row

```
source_records
  record_key       = notion:215a8e78…:page:198c5c1d…
  content_hash     = 311775e6ba0b7330881a7f96ddc5037a…
  primary_node_uid = d837df9a-29c7-5365-be20-f2401bca786d   ← ye key yaad rakho
  semantic_status  = done
  update_count     = 0
```

`content_hash` poore page ka fingerprint hai — isi se pata chalta hai page badla
ya nahi.

---

## Step 2 · History likho

```
record_versions
  version      = 1
  content_hash = 311775e6…
  ingested_at  = 2026-09-15T07:06:26
```

Append-only. Purani version kabhi overwrite nahi hoti.

---

## Step 3 · Text chop karo → SQLite mein chunk

```
source_chunks
  chunk_id      = 78d13c7e-035a-5222-8ff0-aa7d23a088de   ← andar sha256 baked hai
  chunk_index   = 0
  text          = "[SOURCE] Kind: Document Name: All things Nilus!
                   Nilus is 'GA'. Lenovo and Gensler are prod-live!…"
                                                          (1,508 chars)
  status        = done
  superseded_at = NULL                                    ← live hai
```

**Asli text yahin rehta hai. Yahi evidence hai.**

---

## Step 4 · Node banao → FalkorDB

```cypher
(:Document {
   uid              : "d837df9a-29c7-5365-be20-f2401bca786d",   // wahi uid
   name             : "All things Nilus!",
   url              : "https://app.notion.com/p/All-things-Nilus-198c5c1d…",
   search_text      : "[SOURCE] Kind: Document…",                // 1,508 chars, copy
   first_seen_at    : "…",
   last_edited_time : "…"
})
```

---

## Step 5 · Edges banao → FalkorDB *aur* SQLite dono

**FalkorDB:**

```cypher
(Workspace "DataOS (Internal)") ─CONTAINS─────► (this Document)

(this Document) ─MENTIONED_IN─► (SourceRecord "All things Nilus!")

(this Document) ─PARENT_OF────► (Document "Documentation Maintenance Guide")
                                 provenance: notion:…:page:357c5c1d…
```

**SQLite `record_edges`:**

```
record_key = notion:…:page:198c5c1d…     rel_type = CONTAINS
from_uid   = 5a475f6f… (workspace)       to_uid   = d837df9a… (this doc)
```

### Do cheezein gaur karne layak

**`MENTIONED_IN` ka provenance khaali hai** — kyunki wo *khud* provenance link
hai. Node se uske source record tak jaata hai.

**`PARENT_OF` ka provenance doosre record ko point karta hai** (`page:357c5c1d`)
— child page ke record se. Edge us record se likha gaya jisne relationship
batayi, is page se nahi.

---

## Step 6 · Embed karo → Qdrant

```
point id = d837df9a-29c7-5365-be20-f2401bca786d    ← wahi uid, teesri baar

vectors:
  name    : 1536 floats   ← sirf "All things Nilus!" embed hua
  content : 1536 floats   ← poora 1,508-char text embed hua

payload:
  label          : "Document"
  uid            : "d837df9a…"
  embedded_text  : "[SOURCE] Kind: Document…"     ← kya embed hua, verbatim
  embedded_model : "text-embedding-3-small"
```

`embedded_text` store karne ki wajah: ek timestamp batata hai *"embed hua tha
kya"*, par ye batata hai *"embedding abhi bhi isi text ki hai kya"*.

---

## Step 7 · Jo refuse hua wo bhi likho → SQLite

Is **ek** chunk se 10 rows:

| reason | subject | relation | object |
|---|---|---|---|
| `relation_not_allowed` | Term "GA" | APPLIES_TO | System "Nilus" |
| `relation_not_allowed` | Term "prod-live" | APPLIES_TO | Project "Lenovo" |
| `relation_not_allowed` | Term "prod-live" | APPLIES_TO | Project "Gensler" |
| `relation_not_allowed` | Term "Latest Public Release" | APPLIES_TO | System "DataOS" |
| `relation_not_allowed` | Term "Latest Public Release" | APPLIES_TO | System "DataOS" |
| `entity_no_connecting_fact` | Term "GA" | — | — |
| `entity_no_connecting_fact` | Term "prod-live" | — | — |
| `entity_no_connecting_fact` | Term "Latest Public Release" | — | — |
| `entity_no_connecting_fact` | System "Nilus" | — | — |
| `entity_no_connecting_fact` | System "DataOS" | — | — |

### Ye cascade demo karne layak hai

Upar ke 5 neeche ke 5 ki **wajah** hain:

```
AI ne kaha:   Term "GA" ─APPLIES_TO─► System "Nilus"
                    │
                    ▼
rulebook mein Term→System ka APPLIES_TO nahi hai
                    │
                    ▼
              relation_not_allowed
                    │
                    ▼
to GA aur Nilus ke paas koi bacha hua fact nahi
                    │
                    ▼
              entity_no_connecting_fact

        5 relations gire  →  5 entities gire
```

Aur wahi `Term -APPLIES_TO-> System` shape **72 baar** aaya hai — isiliye wo
`ontology_misses` mein hai. **Ek SQL row add karne se ye 72 facts wapas aa
jate.**

---

## Do join keys — poora system inhi par khada hai

### `uid` — entity ki pehchaan, teeno stores mein same

```
SQLite     source_records.primary_node_uid  = d837df9a…
FalkorDB   node.uid                         = d837df9a…
Qdrant     point.id                         = d837df9a…
```

### `record_key` — provenance, do stores mein

```
SQLite     source_records / source_chunks / record_edges / extraction_drops
FalkorDB   SourceRecord.record_key,  edge.source_record_keys[]
```

Isi wajah se chat kisi bhi fact par *"ye kahan se aaya"* bata sakta hai — edge
se `record_key` uthao, SQLite se chunk text nikalo.

---

## Aur yahi wo drift risk hai

`uid` **teen jagah** likha jata hai, par koi transaction teeno ko nahi
baandhta.

FalkorDB mein node ban jaye aur Qdrant upsert fail ho jaye — node fulltext
mein milega, vector leg mein nahi. Utopia mein node aur uska vector **ek hi
Postgres row** hain, ek transaction mein.

Ye wahi fark hai jiski wajah se Postgres-only port par baat hui thi.

---
---

# Part B — Retrieval

Ab ulta safar. Sawaal:

> **"Is Nilus GA, and which customers are live in production?"**

Wahi page jo Part A mein ingest hua tha, isi sawaal ka jawab dega.

---

## Flow

```
Question
    │
    ├─ 1. query embed         ──────────────► OpenAI      1536 floats
    │
    ├─ 2. FULLTEXT leg        ──────────────► FalkorDB    7 labels x 30
    ├─ 3. VECTOR leg          ──────────────► Qdrant      content + name
    ├─ 4. RRF fusion          ──────────────► (Python)    top 6
    │
    ├─ 5. evidence gather     ──────────────► FalkorDB    facts + provenance
    ├─ 6. LLM                 ──────────────► OpenAI      answer
    └─ 7. citations           ──────────────► FalkorDB    record_key -> source
```

Gaur karein: **SQLite is raste mein hai hi nahi.** Ledger ingestion ke liye hai,
padhne ke liye nahi.

---

## Step 1 · Question ko vector banao

```
"Is Nilus GA, and which customers are live in production?"
        │
        ▼  text-embedding-3-small
   1536 floats
```

Wahi model jo ingestion mein use hua tha — warna dono vectors ek hi space mein
nahi hote aur comparison bemaani ho jata.

---

## Step 2 · Fulltext leg → FalkorDB

FalkorDB ka fulltext index **per-label** hai, to har label alag se poochna
padta hai:

```
WorkItem      30 hits
Document      30 hits
Decision      30 hits
Term          30 hits
Commit        30 hits
PullRequest   30 hits
SourceFile    30 hits
              ─────────
              210 total
```

Phir saare 210 ek **global ranking** mein fuse hote hain (`global_score`):

```
rank 0: [Decision] create a new capture instance for incompatible…
rank 1: [Decision] use initial snapshots for the MySQL-to-Lakehouse…
rank 2: [Decision] run the local truth gate before production…
rank 3: [Term]     source inventory
…
rank 152: [Document] All things Nilus!        ← hamara page, bahut neeche
```

**Hamara page yahan 152ve number par hai.** Sawaal mein "GA" aur "production"
shabd hain, par page ka text unhe usi tarah nahi likhta — fulltext literal
matching hai.

Ye global fusion hi wo fix tha jiski baat hui thi. Pehle har label apne andar
rank hota tha, to 5 Documents mein sabse achha hona 264 SourceFiles mein sabse
achha hone ke barabar score paata tha.

---

## Step 3 · Vector leg → Qdrant

Do channel, dono global (label-wise nahi):

```
content channel : 210 results   ← poore text ke vector se
name channel    : 210 results   ← sirf naam ke vector se
```

Dono **interleave** hote hain — ek-ek karke, score merge nahi:

```
rank 0: [Document] Understanding Nilus Manager          cos=0.5581
rank 1: [Decision] tune Nilus worker configuration…     cos=0.5503
rank 2: [Decision] author Nilus CDC pipelines…          cos=0.5633
rank 3: [Decision] tune Nilus worker configuration…     cos=0.5497
rank 4: [Term]     Nilus Batch                          cos=0.5626
rank 5: [Document] All things Nilus!                    cos=0.5495   ← hamara page
```

**Yahan hamara page 5ve number par hai** — 152ve ki jagah. Semantic search ne
wo pakda jo literal matching se chhoot gaya tha.

Score merge kyun nahi karte: chhoti query aur chhote text ke beech distance
systematically kam aati hai, to ek channel saare top slots kha jata.

---

## Step 4 · RRF fusion — asli hisaab

Dono legs ke **rank** jodte hain, score nahi. Hamare page ke liye:

```
RRF_K = 10        VECTOR_LEG_WEIGHT = 3.0

fulltext  rank 152  →  1  / (10 + 152 + 1)  =  0.0061
vector    rank   5  →  3.0 / (10 +   5 + 1)  =  0.1875
                                               ───────
                                       total  =  0.1936
```

Do cheezein saaf dikhti hain:

**Vector leg ne is page ko bachaya.** Fulltext ka yogdan 0.0061 tha — kuch bhi
nahi. Agar sirf fulltext hota, ye page kabhi top 6 mein nahi aata.

**Isiliye vector leg ka weight 3.0 hai.** Wo hand-picked nahi hai — golden set
par sweep karke chuna gaya.

Top 6 ban gaye:

```
0.2776  [Document] Understanding Nilus Manager        fulltext+vector
0.2500  [Decision] tune Nilus worker configuration…   vector
0.2308  [Decision] author Nilus CDC pipelines…        vector
0.2143  [Decision] tune Nilus worker configuration…   vector
0.2000  [Term]     Nilus Batch                        vector
0.1936  [Document] All things Nilus!                  fulltext+vector   ← hamara page
```

Ye 389 candidates mein se chune gaye.

---

## Step 5 · Har hit ke facts uthao → FalkorDB

Har node ka neighbourhood, uski provenance ke saath:

```
[Document] Understanding Nilus Manager     facts=7  records=1  chars=4,704
[Decision] tune Nilus worker config (CDC)  facts=2  records=1  chars=  648
[Decision] author Nilus CDC pipelines      facts=3  records=1  chars=  855
[Decision] tune Nilus worker config (bal)  facts=2  records=1  chars=  785
[Term]     Nilus Batch                     facts=2  records=1  chars=  418
[Document] All things Nilus!               facts=2  records=2  chars=1,718
                                           ────────────────────────────────
                                 context:  blocks=6  facts=18  chars=9,138
```

Yahi wo jagah hai jahan Part A ka `search_text` wapas kaam aata hai — node par
jo text likha tha, wahi ab evidence banta hai.

---

## Step 6 · LLM ko bhejo

```
input  : 2,761 tokens        (6 blocks + system prompt)
output :    80 tokens
time   : 10.59 s
```

Prompt sirf ek kaam karta hai — **jo evidence di gayi hai usi se jawab do**,
aur bata do ki kaun sa block use kiya.

Jawab:

> ### General availability
>
> - Yes. Nilus is **GA**.
> - **Lenovo** and **Gensler** are live in production. The source states this
>   as of **September 15, 2026** *(All things Nilus!)*.

---

## Step 7 · Citations → FalkorDB

LLM ne **6 mein se sirf 1** block use kiya:

```
cited blocks : 1   →  "All things Nilus!"
citations    : 2   →  us block ke record_keys se resolve hue
```

Baaki 5 blocks bheje gaye the par jawab mein nahi aaye — isiliye cite nahi
hue. Ye jaan-boojh kar hai: sirf wo source dikhana jispar claim tika hai.

---

## Poora circle

```
Part A:  Notion page  ──►  chunk  ──►  node (uid d837df9a…)  ──►  2 vectors
                                              │
                                              ▼
Part B:  question  ──►  vector rank 5  ──►  RRF 0.1936  ──►  evidence
                                              │
                                              ▼
                                      "Nilus is GA. Lenovo and
                                       Gensler are live in production."
                                              │
                                              ▼
                                  citation: All things Nilus!
```

Jo text Part A mein `source_chunks` mein likha tha — *"Nilus is 'GA'. Lenovo
and Gensler are prod-live!"* — wahi Part B mein jawab banke wapas aaya, apne
source ke link ke saath.

**Yahi poora system hai.**

---

## Do cheezein jo is trace se saaf hui

**Fulltext akela kaafi nahi hai.** Hamara page fulltext mein 152ve number par
tha. Sirf vector leg ne usse top 6 mein pahunchaya. Aur ulta bhi sach hai —
vector akela exact identifier (file path, ticket key) miss karta hai. Dono
chahiye.

**SQLite read path mein hai hi nahi.** Ledger sirf ingestion ke liye hai.
Retrieval FalkorDB aur Qdrant se hota hai. Isiliye kal wali baat mein ledger ko
graph mein daalne ka koi fayda nahi tha — wo do alag raste hain.
