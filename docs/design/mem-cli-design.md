# Plan: `mem` — Unix-Native Memory Filesystem CLI

## Context

A memory system for LLM agents that uses **plain files as source of truth** and **SQLite as a derived index**. The filesystem IS the graph, Unix commands are the verbs. The CLI wraps standard Unix operations with index maintenance and neuroscience-informed connection dynamics.

Reviewed by: Rob Pike (namespace/composition), Andrej Karpathy (complexity/tokens), computational neuroscience research, and compared against MemPalace (96.6% LongMemEval via verbatim + ChromaDB).

**Key differentiators vs. MemPalace:** rebuildable index (not opaque ChromaDB), hybrid search baked into production path (not benchmark-only), unified graph (not two disconnected systems), principled maintenance via decay (not verbatim-forever).

## File Structure

```
~/workspace/bin/mem.py              # Single-file CLI
~/workspace/bin/mem-eval.py         # Separate benchmark harness (not in mem.py)

<memory-root>/                      # Files live at root (no nodes/ subdir)
├── .mem/                           # Hidden — derived index + config
│   └── memory.db                   # SQLite: FTS5, edges, access logs
├── projects/
├── people/
└── concepts/
```

- `.mem/` follows `.git/` convention — derived metadata hidden, human content at root
- `MEM_HOME` env var points to default memory root (Sprint 1, not deferred)
- `memory.db` is always rebuildable from files via `mem reindex`

## Path Resolution & Link Semantics

All paths stored in `edges`, `nodes`, and `fts` are **relative to MEM_HOME**. No `..` allowed in stored paths. Resolution rules:

- CLI accepts absolute paths or relative-to-cwd; internally normalized to relative-to-MEM_HOME before storage
- `[[links]]` in files are resolved relative to the file's directory, then normalized to relative-to-MEM_HOME
- `[[Target|Alias]]` supported — `Target` is the path, `Alias` is display-only (not stored)
- Same filename in different dirs is fine — `people/ken.md` and `concepts/ken.md` are distinct paths
- On directory rename, daemon issues `UPDATE edges SET source = replace(source, old_prefix, new_prefix)` / same for `target` — atomic, O(affected rows) with existing indexes
- On `mem init` / `mem reindex`, unresolvable links (target file doesn't exist) are stored as edges with `strength = 0` and excluded from search boosting. On file create event, daemon runs `UPDATE edges SET strength = 1.0 WHERE target = ? AND strength = 0` — O(1) with `idx_edges_target`

## Atomicity & Write Safety

All file mutations happen via normal Unix tools (the agent's native I/O). The `watchdog` handler processes each filesystem event inside a single SQLite transaction. `PRAGMA journal_mode=WAL;` on every connection open. `mem reindex` is the recovery path if the DB ever gets out of sync.

## Ignore Rules

`.memignore` file at MEM_HOME root, `.gitignore` syntax. Default ignores (hardcoded):

```
.mem/
.git/
node_modules/
*.log
*.tmp
```

Checked on `mem init`, `mem reindex`, and by the `watchdog` handler on every filesystem event. PID and log files (`.mem/watch.pid`, `.mem/watch.log`) are hardcoded ignores.

## Error Schema

```jsonc
// Recoverable error: NDJSON to stderr, exit 1
{"error": "file_not_found", "path": "missing.md", "hint": "check path or run mem reindex"}

// Fatal error: NDJSON to stderr, exit 2
{"error": "db_corrupt", "path": ".mem/memory.db", "hint": "run mem reindex to rebuild"}
```

## Architecture: Daemon + One Search Command

The agent uses normal file I/O (any tool, any harness). A background daemon keeps the index current. The agent only calls `mem` for search.

### Agent-facing (one command)

| Command | What it does |
|---------|--------------|
| `mem grep <query>` | Hybrid FTS5 search (+ vectors in v2). Returns ranked NDJSON. Creates search edges to top-3 results. |

**Agent system prompt (3 sentences):**
> Your memory lives in `$MEM_HOME`. Read and write files normally with any tool. Use `mem grep <query>` to search — connections between files strengthen when you search for them and weaken over time.

### Operator-facing (setup + maintenance)

| Command | What it does |
|---------|--------------|
| `mem init [dir]` | Create `.mem/memory.db`, initial full index scan |
| `mem watch` | Start filesystem watcher daemon (foreground, or `--daemon` for background) |
| `mem reindex` | Nuke `.mem/memory.db`, rescan all files from scratch |
| `mem status` | Node/edge counts, last index update, last decay, index freshness |
| `mem ls [dir]` | List files with edge counts. `--backlinks <path>` for incoming edges. |

**Background (invisible):**
- `mem watch` daemon: uses `watchdog` library (cross-platform: FSEvents/inotify/ReadDirectoryChangesW). On file create/modify/delete/rename → update nodes, FTS5, parse `[[links]]` → edges, extract `date:` → `date_hint`. Respects `.memignore` via `watchdog`'s `ignore_patterns`.
- `mem _decay`: hidden subcommand, idempotent. Run via launchd on macOS (`com.captains.mem-decay`), cron/systemd timer on Linux.

**Daemon lifecycle:**
- `mem watch` — foreground (for debugging)
- `mem watch --daemon` — background (writes PID to `.mem/watch.pid`)
- `mem watch --stop` — kills daemon by PID
- `mem watch --status` — running/stopped + PID + last event timestamp
- `mem init --daemon` — create DB + start watcher + install launchd plist (one-shot setup)
- launchd plist: `KeepAlive: true`, `StandardErrorPath: .mem/watch.log`

**Separate script:** `mem-eval.py` — benchmark harness for LongMemEval.

**Deferred to v2:** `consolidate`, `wake-up` (4-layer context loading), `dedup`.

## NDJSON Output Schemas (defined before coding)

```jsonc
// mem grep — the agent's only command
{"path": "projects/satori.md", "title": "Satori", "rank": 1, "score": 0.82, "edge_strength": 1.2, "snippet": "...kanji curriculum..."}

// mem ls (default)
{"path": "projects/satori.md"}

// mem ls --verbose  
{"path": "projects/satori.md", "title": "Satori", "links_out": 3, "links_in": 5, "search_hits": 12}

// mem status
{"nodes": 142, "edges": {"link": 87, "search": 310}, "queries": 45, "last_index": "2026-04-12T...", "last_decay": "2026-04-12T..."}

// mem watch (daemon event stream to stderr)
{"event": "modified", "path": "projects/satori.md", "indexed": true, "links_found": 2}

// Errors (stderr)
{"error": "file_not_found", "path": "missing.md", "hint": "check path or run mem reindex"}
```

## SQLite Schema

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE nodes (
    path          TEXT PRIMARY KEY,
    title         TEXT,
    created_at    TEXT NOT NULL,
    modified_at   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    embedded_at   TEXT,
    -- Search-derived access tracking (updated when file appears in grep results)
    last_searched TEXT,              -- ISO8601, last time this appeared in top-k grep results
    search_count  INTEGER DEFAULT 0, -- number of times appeared in grep results
    date_hint     TEXT               -- ISO8601, extracted from frontmatter `date:` field on index
);

-- 2 edge types: 'link' ([[target]] in file content), 'search' (query → top-3 result)
-- Per neuroscience (fan effect, Anderson 1974): edges connect query→result only,
-- never result↔result. Top 3 results per search, weighted by rank.
CREATE TABLE edges (
    source         TEXT NOT NULL,
    target         TEXT NOT NULL,
    type           TEXT NOT NULL CHECK(type IN ('link', 'search')),
    strength       REAL NOT NULL DEFAULT 1.0,
    last_activated TEXT,
    access_count   INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source, target, type)
);
CREATE INDEX idx_edges_target ON edges(target);
CREATE INDEX idx_edges_strength ON edges(strength);

-- Search queries (lightweight — used as edge sources, decay and prune)
CREATE TABLE queries (
    id          TEXT PRIMARY KEY,           -- hash of normalized query text
    query_text  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used   TEXT NOT NULL,
    use_count   INTEGER DEFAULT 1
);

-- FTS5 full-text search (title weighted 5x in BM25)
CREATE VIRTUAL TABLE fts USING fts5(
    path, title, content,
    tokenize='porter unicode61'
);

-- Embeddings (v2 — table exists but empty until --embed)
CREATE TABLE embeddings (
    path       TEXT PRIMARY KEY,
    vector     BLOB NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

**Cut from v1 schema (per Pike + Karpathy):** `expires_at`, `co_access_log`, `frontmatter` column, `frontmatter_text` FTS column.

## Decay Model (Neuroscience-Informed)

### Power-Law Decay (not exponential)

Per neuroscience review: single exponential is the one model the forgetting data consistently rejects. Power law decays fast early, levels off — connections that survive 90 days have proven durability.

```python
def decayed_strength(strength, days_since_last_co_access):
    return strength * (1 + 0.1 * days_since_last_co_access) ** -0.5
```

| Time since last co-access | Retained strength (from 1.0) |
|---------------------------|------------------------------|
| 1 day | 0.95 |
| 7 days | 0.77 |
| 30 days | 0.50 |
| 90 days | 0.32 |
| 365 days | 0.16 |

### Spacing-Effect Increment (not flat)

Per neuroscience: accessing two files together 10 times in one hour is NOT the same signal as once a month for 10 months. Time-weighted increment with schema bonus:

```python
days_gap = max(0, (now - last_co_accessed).days)
same_dir = os.path.dirname(source) == os.path.dirname(target)
schema_multiplier = 1.5 if same_dir else 1.0
increment = 0.05 * (1 + math.log(1 + days_gap)) * schema_multiplier
```

| Gap since last co-access | Increment |
|--------------------------|-----------|
| Same session (0 days) | 0.05 (× 1.5 if same dir) |
| 7 days | 0.10 |
| 30 days | 0.13 |
| 365 days | 0.17 |

Max strength cap: 5.0. Link edge floor: 0.5. Search edges decay fully (no floor).

### Connection Model (Neuroscience-Informed)

Per Anderson's fan effect (1974) and sparse coding research: nodes with too many associations become harder to retrieve. Degradation is measurable by fan 3. The brain strengthens connections between the retrieval cue and a sparse set of results — NOT between results and each other.

**Two edge types only:**

| Type | Source | Target | Created by | Decay |
|------|--------|--------|------------|-------|
| `link` | file A | file B | `[[B]]` written in A's content | Power-law, floor 0.5 (LLM's deliberate judgment persists) |
| `search` | query node | file | `mem grep` top-3 results | Power-law, full decay (organic, prunable) |

**Query normalization:** `lowercase → strip punctuation → Porter stem → sort tokens → SHA-256 hash`. This means "Satori kanji" and "kanji satori" and "KANJI SATORI!" all resolve to the same query node, preventing fragmentation.

**Search edge creation:** Each `mem grep` call:
1. Normalizes + hashes the query text → query node in `queries` table
2. Creates/strengthens edges from query → rank 1, 2, 3 results only
3. Rank-weighted increments: rank 1 = full increment, rank 2 = 0.66×, rank 3 = 0.33×
4. If the same query is repeated, the query node's `use_count` increments and existing edges strengthen with spacing effect
5. Updates `nodes.last_searched` and `nodes.search_count` for each top-k result

**No result↔result edges.** Files that are repeatedly relevant to similar queries develop strong incoming search edges from overlapping query nodes. Graph traversal through shared query nodes provides the associative path between related files — without polluting the graph with O(n²) spurious edges.

### Decay is invisible to the agent

Two triggers, both invisible to the agent:

1. **Lazy:** Every `mem grep` call applies decay to results before ranking. Cheap — only decays edges touching the result set, not the entire graph.
2. **Scheduled:** `launchd` plist at `~/Library/LaunchAgents/com.captains.mem-decay.plist` runs `mem.py _decay` (hidden subcommand, not in `--help`) daily at 3am. Full graph sweep.

No `mem decay` user-facing command in v1. The agent accesses memories and creates connections; the system handles retirement.

## Stale Index Prevention

Two mechanisms, both automatic:

1. **Daemon (primary):** `mem watch` detects file modifications via `watchdog` and immediately reindexes — updates `content_hash`, FTS5 entry, re-parses `[[links]]`, extracts `date:` from frontmatter into `date_hint`. This is real-time; files are never stale while the daemon runs.

2. **Lazy (fallback):** On `mem grep`, before searching, check `nodes.modified_at` against actual file mtime for any result. If stale (daemon was down), reindex that file inline before returning results. Belt and suspenders.

## Access Tracking via Search

With no `mem cat`, the only read signal is search. `accessed_at` / `access_count` are replaced with `last_searched` / `search_count` — updated when a file appears in `mem grep` top-k results. This is actually a better signal: it means "the system found this relevant to a query" rather than "someone opened this file."

`mem grep` updates for each top-k result:
- `nodes.last_searched = now()`
- `nodes.search_count += 1`

## Search: `mem grep`

### v1: FTS5 only

Per Karpathy: measure before adding vectors. FTS5 with Porter stemming handles morphological variants. Run against LongMemEval first — if accuracy is >50%, the gap to 60% may not require vectors.

```python
SELECT path, rank FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 20
```

BM25 column weights: `{path: 1.0, title: 5.0, content: 1.0}`.

### v1 bonus: Temporal proximity scoring

Per MemPalace comparison: when query contains a date reference (regex: `YYYY-MM-DD`, `Mon DD`, `last Tuesday`), boost results whose `date_hint` (extracted from frontmatter `date:` field on index) or `modified_at` are close:

```python
doc_date = date_hint or modified_at  # prefer explicit date, fall back to mtime
temporal_boost = 1.0 / (1.0 + abs(query_date - doc_date).days)
score = fts_score * (1 + temporal_boost)
```

### v2: Edge expansion (pattern completion)

Per neuroscience (engram networks): after FTS5 produces initial top-N, one hop of graph traversal to find "silent engrams" — strongly connected files whose content doesn't match the query:

1. For each top-5 result, fetch edges with `strength > 0.5`
2. Score neighbors: `edge_strength * 0.3`
3. Merge, deduplicate, re-rank. Max 5 neighbors per result, 20 expanded total.

### v2: Vector search + RRF fusion

Add `all-MiniLM-L6-v2` embeddings. Reciprocal Rank Fusion: `score = 1/(60+rank_fts) + 1/(60+rank_vec)`. Only if FTS5-only benchmark shows meaningful gap.

## LongMemEval Integration (`mem-eval.py`)

Separate script at `~/workspace/bin/mem-eval.py`. Calls `mem` as subprocess.

### Ingestion

Each haystack session → one markdown file:
```markdown
---
session_id: answer_4be1b6b4_2
date: "2023/04/10 (Mon) 17:50"
---
**User:** I'm thinking of getting my car detailed...
**Assistant:** I'm happy to help...
```

Exchange-pair chunking (per MemPalace): user turn + AI response as one semantic unit within the file. Configurable via `--max-lines-per-chunk` (default 8, but LongMemEval sessions often have longer multi-turn context).

### Metrics (per MemPalace comparison)

**Fast retrieval metrics (no LLM, iterate quickly):**
- **Recall@k:** Did the correct session appear in top-k results?
- **MRR (Mean Reciprocal Rank):** Average of 1/rank for the first correct result. Punishes bad ranking that Recall alone hides.
- **Precision@k:** What fraction of top-k results are actually relevant?

**Slow QA metric (Claude judge, run once retrieval is solid):**
- **QA accuracy:** Full answer generation + LLM judge, per-task breakdown.

### Pipeline

```bash
# Fast retrieval metric
mem-eval.py recall longmemeval_oracle.json --root /tmp/eval/ --k 5

# Full QA metric  
mem-eval.py qa longmemeval_oracle.json --root /tmp/eval/ --output hyp.jsonl
mem-eval.py score hyp.jsonl longmemeval_oracle.json
```

## Implementation Sequence

### Milestone 1: Core + Daemon + Eval (~600 lines)

Build the minimum testable system. Eval in Sprint 1 — iterate on the right things from day one.

1. SQLite schema + WAL mode + connection management
2. File parsing: YAML frontmatter, `[[link]]` extraction, content_hash
3. `mem init` — scan dir, index all .md files, create link edges
4. `mem grep` — FTS5 search with BM25 column weights. Creates search edges to top-3 results (rank-weighted). Query node tracking.
5. `mem ls`, `mem status`, `mem reindex`
6. `mem watch` — cross-platform `watchdog` handler (`PatternMatchingEventHandler` with `.memignore` patterns). On file create/modify/delete/rename → update nodes, FTS5, parse `[[links]]` → edges, extract `date:` → `date_hint`. Daemon lifecycle: `--daemon`, `--stop`, `--status`.
7. `.memignore` support, `MEM_HOME` env var, argparse, NDJSON output, exit codes
8. `mem-eval.py` — ingest LongMemEval sessions, Recall@k + MRR + Precision@k scoring
9. **Validate:** Run Recall@k on `longmemeval_oracle.json`. Establish FTS5 baseline.

### Milestone 2: Decay + Search Dynamics (~200 lines)

10. Power-law decay engine (`mem _decay`, hidden subcommand for launchd)
11. Spacing-effect search edge increments (repeated queries strengthen existing edges with time-weight)
12. Search-edge rank weighting validation (confirm rank 1/2/3 increments are well-calibrated)
13. Lazy decay application before `mem grep` results ranking
14. Temporal proximity scoring in `mem grep`
15. launchd plist for daily decay (`com.captains.mem-decay`)
16. **Validate:** Re-run Recall@k. Compare with/without decay + temporal scoring.

### Milestone 3: QA Evaluation (~200 lines)

17. `mem-eval.py qa` — retrieve + prompt Claude → hypothesis JSONL
18. `mem-eval.py score` — Claude-as-judge (adapt evaluate_qa.py)
19. Per-task accuracy reporting (6 question types + abstention)
20. **Validate:** Full QA accuracy on `longmemeval_oracle.json`. Compare against published baselines.

### Milestone 4: Vectors (only if benchmark justifies) (~200 lines)

21. Venv bootstrap (`~/.mem/venv/`, sentence-transformers)
22. Embedding computation with `all-MiniLM-L6-v2`
23. BLOB storage in embeddings table
24. Brute-force cosine search + RRF fusion in `mem grep`
25. Edge expansion (pattern completion via query node traversal, 1-hop)
26. **Validate:** Recall@k and QA accuracy delta from adding vectors.

### Milestone 5: Advanced Features (~200 lines)

27. `mem wake-up` — 4-layer context loading (identity + top nodes by search-hit frequency)
28. `mem consolidate` — merge near-duplicates, detect orphans, prune dead query nodes
29. `mem dedup` — find near-duplicate nodes via FTS5 similarity

**Total:** ~1,400 lines across two files (mem.py + mem-eval.py).

## Dependencies

### Sprint 1 (required)

| Package | Purpose |
|---------|---------|
| `watchdog>=4.0` | Cross-platform filesystem watcher (FSEvents/inotify/ReadDirectoryChangesW). Uses `PatternMatchingEventHandler` with `ignore_patterns` from `.memignore` + hardcoded defaults. |
| `pyyaml` | Frontmatter parsing (already installed system-wide as 6.0.2) |
| stdlib: `sqlite3`, `json`, `os`, `hashlib`, `argparse`, `re`, `struct`, `datetime`, `tempfile` | Core functionality |

### Sprint 4 (only if benchmark justifies vectors)

| Package | Purpose |
|---------|---------|
| `sentence-transformers` | `all-MiniLM-L6-v2` embedding model |
| `numpy` | Vector math |
| `torch` (CPU) | Required by sentence-transformers |

## Key Files

| File | Role |
|------|------|
| `~/workspace/bin/mem.py` | **CREATE** — the CLI |
| `~/workspace/bin/mem-eval.py` | **CREATE** — benchmark harness |
| `~/workspace/bin/todo.py` | Reference: argparse, NDJSON, atomic writes |
| `~/workspace/bin/feed-sync.py` | Reference: subprocess Claude calls, single-file CLI |
| `~/workspace/tools/longmemeval/data/longmemeval_oracle.json` | Benchmark data |
| `~/workspace/tools/longmemeval/src/evaluation/evaluate_qa.py` | Reference for judge prompts |

## Verification

| Step | What | Target |
|------|------|--------|
| 1 | `mem init` on test dir with 2 linked .md files | 2 nodes, 1 link edge |
| 2 | `mem watch` + create new .md file with `[[link]]` | Daemon detects change, indexes file, creates link edge |
| 3 | `mem watch` + modify existing file | Daemon detects change, content_hash updated, FTS5 refreshed |
| 4 | `mem watch` + delete file | Daemon removes node + prunes edges |
| 5 | `mem grep "hello"` | FTS5 ranked results as NDJSON, search edges to top-3 |
| 6 | `mem grep "hello"` repeated after 7 days | Search edges strengthen with spacing-effect increment |
| 7 | `mem-eval.py recall longmemeval_oracle.json` | Recall@5 + MRR + Precision@5 baseline |
| 8 | `mem _decay` on aged edges | Power-law strength reduction verified |
| 9 | `mem-eval.py qa longmemeval_oracle.json` | QA accuracy per task type |
| 10 | Sprint 4 vectors (if needed) | Recall@5 delta measured |

## Research Docs (for reference)

- `doc/research/mastra-memory-system.md` — Observable Memory architecture, 94.87% LongMemEval
- `doc/research/llm-memory-architectures.md` — MemGPT, GraphRAG, Mem0, Zep, LangGraph, Cognee comparison
- `doc/research/filesystem-memory-search.md` — FTS5, embedding models, graph structure, maintenance
- `doc/research/memory-eval-benchmarks.md` — 15 runnable benchmarks across 4 categories
- `doc/research/neuroscience-memory-principles.md` — Power-law decay, spacing effect, consolidation, engrams
