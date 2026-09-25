# Neuron ingestion flow — simple version

```mermaid
flowchart TD
    S[Jira + Bitbucket + Notion] --> D[Deterministic Pass 1]
    D --> P[(PostgreSQL + pgvector)]

    P --> R{Chunk resolved?}
    R -->|Yes| DF[Write deterministic facts — no LLM]
    R -->|No or partial| V[Retrieve related nodes and old evidence]
    V --> L[Semantic extraction — LLM call]

    L --> O[Structured chunk decision]
    O --> N["Graph branch<br/>Attach existing node<br/>Create new node<br/>Update or supersede fact"]
    O --> I["Finding branch<br/>Contradiction<br/>Architecture change<br/>Impact proposal"]

    DF --> G[(PostgreSQL fact ledger + FalkorDB)]
    N --> G
    G --> DI[Deterministic impact check]
    DI --> I

    I --> FL[Create, update, resolve or stale Finding]
    FL --> W{Reusable lesson?}
    W -->|Yes| WL[Wisdom aggregation — separate LLM call]
    WL --> H[Human review]
    H --> K[Store Wisdom with Finding and source lineage]

    G --> E[Ingestion complete]
    FL --> E
    K --> E

    classDef llm fill:#4b2e66,stroke:#b57cff,color:#fff;
    classDef finding fill:#4a171b,stroke:#b3262d,color:#fff;
    classDef wisdom fill:#33205c,stroke:#8b5cf6,color:#fff;

    class L,WL llm;
    class I,FL finding;
    class W,H,K wisdom;
```

- The same structured chunk decision feeds the graph-update and Finding branches in parallel.
- Deterministic facts also run an impact check and can create or update a Finding without another LLM call.
- Wisdom runs after Findings as a separate aggregation call; it never derives organisational wisdom directly from a raw chunk.
