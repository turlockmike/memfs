# memfs v2 — Neo4j Architecture

**Status:** design, April 16, 2026
**Author:** Karpathy (at Mike's direction)
**Origin:** Apr 16 conversation with Mike on viable memory; Rick's Neo4j recommendation; direct request to build memfs right from day 1

## Why Neo4j from Day 1

memfs is a graph system. Storing a graph in SQLite means translating graph operations into SQL every time. The dream skill's most valuable operations — shared-query-node clustering, multi-hop expansion, path finding, synthesis node detection — are inherently graph queries. Cypher expresses them in one line; SQL expresses them in recursive CTE hell.

memfs is also greenfield: zero users, zero deployed memories, zero downstream code. Migration cost is strictly monotonic — never cheaper than now. Choosing SQLite today because it's convenient is programmed technical debt.

The architectural correctness argument: viable memory requires the right substrate from the start. "Start with the convenient thing and migrate later" is exactly the pattern that produces substrate-forced rewrites. Build it right once.

## Core Commitments (unchanged from v1)

1. **Files are source of truth.** Neo4j is a derived cache, rebuildable from files via `memfs reindex`. Blow it away with no data loss.
2. **One verb for the agent.** `memfs grep` stays the single agent-facing search command. Implementation swaps; interface doesn't.
3. **Harness-agnostic.** Agent uses normal filesystem tools for read/write. memfs is indexing, not storage.
4. **Neuroscience-informed dynamics.** Power-law decay, fan-effect-limited edges, spacing-effect increments — all preserved.

## Architecture

```
Agent (any harness)
  ├─ Read/Write/Edit/Glob/Grep      →  Filesystem (source of truth)
  │                                     └─ ~/memory/
  │                                         ├─ projects/
  │                                         ├─ people/
  │                                         └─ concepts/
  │
  └─ memfs CLI                      →
       ├─ memfs grep <query>
       ├─ memfs graph <op>
       ├─ memfs claim / verify      (calibration ledger)
       ├─ memfs dream               (compression skill)
       └─ memfs eval                (TCCA + Recall@k)
              │
              ▼
       Neo4j (derived cache, port 7687)
       ├─ (:Node {path, title, layer, source, ...})
       ├─ (:Query {id, text, ...})
       ├─ (:Claim {id, text, confidence, ...})
       ├─ -[:LINK {strength, ...}]->
       ├─ -[:SEARCH {strength, rank, ...}]->
       ├─ -[:CLAIMS_ABOUT {...}]->      (provenance)
       ├─ -[:CONTRADICTS {...}]->       (flagged on ingest)
       └─ full-text index on Node.content

       Filesystem watcher → Neo4j syncer
       ├─ watchdog FSEvents/inotify
       ├─ debounce + batch
       └─ transactional write to Neo4j

       docker-compose.yml → Neo4j 5.26 Community + APOC
```

## Graph Schema

### Node Labels

```cypher
(:Node {
    path: string (PK),              // relative to MEM_HOME
    title: string,
    description: string,
    layer: integer,                 // 1=web-ref, 2=KB, 3=summary, 4=opinion, 5=identity
    source: string,                 // provenance; required for layer >= 3
    content_hash: string,
    date_hint: date,                // parsed from frontmatter
    created_at: datetime,
    modified_at: datetime,
    last_searched: datetime,
    search_count: integer,
    freshness_verified_at: datetime, // when last confirmed matching source
    freshness_source_url: string     // optional; what to re-verify against
})

(:Query {
    id: string (PK, SHA-256 of normalized query),
    text: string,
    created_at: datetime,
    last_used: datetime,
    use_count: integer
})

(:Claim {
    id: string (PK, UUID),
    text: string,
    confidence: float (0-1),
    scope: string,                  // domain tag (trading, ops, factual, architectural)
    claimed_at: datetime,
    claimed_to: string,             // who the claim was made to (telegram, mike-direct, log)
    verified_at: datetime,
    outcome: string                 // correct | wrong | partial | unverified
})
```

### Relationships

```cypher
(source:Node)-[:LINK {strength, created_at, last_activated, access_count}]->(target:Node)
    // Created from [[wikilinks]] in file content. Floor 0.5 on decay.

(source:Query)-[:SEARCH {strength, rank, created_at, last_activated, access_count}]->(target:Node)
    // Created when Query appears in memfs grep top-3. Full decay, prunable.

(node:Node)-[:CLAIMS_ABOUT {created_at}]->(claim:Claim)
    // Provenance: this node asserts this claim. Many-to-many.

(claim_a:Claim)-[:CONTRADICTS {detected_at, adjudicated: bool}]->(claim_b:Claim)
    // Flagged on ingest. Not auto-resolved.

(node:Node)-[:DERIVED_FROM {extraction_type, created_at}]->(source_node:Node)
    // Mandatory at layer >= 3. Summaries/opinions/identity point to their basis.
```

### Full-Text Index

```cypher
CREATE FULLTEXT INDEX node_content IF NOT EXISTS
FOR (n:Node) ON EACH [n.title, n.description, n.content]
OPTIONS {
  indexConfig: {
    `fulltext.analyzer`: 'english',   // Porter-like stemming
    `fulltext.eventually_consistent`: false
  }
}
```

Queries use `CALL db.index.fulltext.queryNodes('node_content', $query)` with BM25-style ranking.

## Layer Typing

Every node has a `layer` integer from 1 to 5:

| Layer | Name | Who writes | Provenance required |
|-------|------|------------|---------------------|
| 1 | Web reference | External (mirror from web) | No — URL is inherent |
| 2 | Knowledge base | Raw capture (sessions, logs, transcripts) | No |
| 3 | Summary | Dream skill or agent | **Yes** — `source:` pointer |
| 4 | Opinion | Agent (interpretation) | **Yes** — pointer chain to source |
| 5 | Identity | Rare, explicit | **Yes** — rationale required |

Indexer validates on ingest. Layer ≥ 3 without `source:` is rejected (non-fatal — logged, file held in quarantine until fixed).

Search can filter by layer: `memfs grep --layer 3 "kanji curriculum"` returns only summaries.

## Provenance Chain

Every layer-3+ node's frontmatter must include:

```yaml
---
layer: 3
source: sessions/2026-04-16-kanji-discussion.md    # relative to MEM_HOME
source_lines: 120-180                                # optional, specific range
extraction_type: summary                             # summary | gist | opinion | synthesis
---
```

Indexer creates `(:Node)-[:DERIVED_FROM]->(source:Node)`. Audit can walk the chain to verify a claim's origin.

## Calibration Ledger

First-class memfs verb, not a skill:

```bash
memfs claim --text "Native CronCreate cannot persist across sessions" \
            --confidence 0.9 \
            --scope architectural \
            --to mike-direct
# emits claim_id

# Later, when verified:
memfs verify <claim_id> --outcome correct
# or
memfs verify <claim_id> --outcome wrong --note "durable:true flag is silently ignored"
```

Storage: append-only `.mem/calibration.jsonl` AND `(:Claim)` node in Neo4j. Writes allowed only via `memfs claim` and `memfs verify`. Other operations are read-only against the ledger.

Reporting: `memfs calibration --window 30d` outputs calibration curve by scope.

## Contradiction Detection

On ingestion of a layer-3+ node (summary, opinion, identity), the indexer:

1. Extracts assertions (claims) from the new content
2. For each claim: Cypher query for existing claims in overlapping scope
3. If a semantically contradicting claim exists, create `[:CONTRADICTS]` relationship
4. Emit `{"event": "conflict", "new": <new_id>, "existing": <existing_id>}` on watcher stderr
5. Do NOT auto-resolve. Dream or Mike adjudicates.

Implementation: semantic overlap via fulltext + Cypher patterns; exactness via embedding sim in later phase. Start with simple heuristics, evolve via eval.

## Freshness Stamps

External-world facts (layer 1, or layer 2 that reference external state) get freshness metadata:

```yaml
---
layer: 2
freshness_verified_at: 2026-04-16T23:45:00Z
freshness_source_url: https://ollama.ai/changelog
freshness_stale_after_days: 30
---
```

`memfs grep` returns results with a `freshness` field:
- `"freshness": "fresh"` — verified within stale_after_days window
- `"freshness": "stale"` — past the window
- `"freshness": "never_verified"` — no verification record

Agent can filter: `memfs grep --fresh-only`. Scheduled `memfs freshness-scan` verb (later milestone) auto-refreshes stale facts against their source URLs.

## TCCA Integration

`mem-eval.py` extends to log:

```json
{
  "question_id": "longmemeval_42",
  "step": 6,
  "retrieval_tokens": 1250,
  "context_tokens": 3400,
  "generation_tokens": 85,
  "total_tokens": 4735,
  "baseline_tokens": 48000,
  "answer_correct": true,
  "tcca": 10.14,
  "layer_distribution": {"2": 0.2, "3": 0.7, "4": 0.1},
  "question_type": "multi-session"
}
```

Reports TCCA by question type AND by layer distribution. Segmented κ (slope over steps).

## Milestones (5 total)

| # | Scope | Target |
|---|-------|--------|
| M1 | Neo4j backing store; all existing features on graph DB; docker compose; passing tests | Apr 17 EOD |
| M2 | Layer typing + mandatory provenance + frontmatter validation | Apr 19 |
| M3 | TCCA instrumentation in eval; reference adapters (accumulate, BM25, vector-RAG) | Apr 21 |
| M4 | Calibration ledger + contradiction events on ingest | Apr 23 |
| M5 | Freshness + anecdotal demo on Karpathy's own corpus + writeup | Apr 26 |

## Non-Goals (explicit)

- **Distributed deployment.** Local Docker Compose only. Production Neo4j clustering is not a memfs concern.
- **Authorization / multi-tenant.** Single-user system. No RBAC.
- **Vector search in M1.** Add as optional adapter later; FTS via Neo4j's Lucene handles baseline.
- **UI.** CLI + NDJSON. No web dashboard in v2.
- **Production hardening of Neo4j.** Community edition + default config is sufficient for the scale.

## Migration from v1 SQLite (for anyone running it)

Not a concern — Mike confirmed no users. The SQLite version is replaced entirely on the `neo4j-rewrite` branch. `master` remains as historical reference.

## Anecdotal Demonstration (M5)

Migrate Karpathy's corpus into memfs v2:
- `~/.config/karpathy/` → `/mike/memory/karpathy/` (identity layer + opinions)
- `~/.claude/projects/-home-mike/memory/` → `/mike/memory/shared/` (mixed layers)
- `~/.config/grove/` → `/mike/memory/grove-archive/` (historical KB)

Run `memfs init` + index. Run `memfs dream` against the corpus. Report:
- Graph statistics (nodes, edges, cluster count)
- Emergent synthesis nodes
- Detected contradictions
- Compression ratio (token cost of loaded neighborhood vs. accumulate-all for typical queries)

This becomes `docs/demonstration.md`. When Mike's eval harness is ready, memfs v2 runs against it and the TCCA numbers become the reference point for the Compression Hypothesis paper.
