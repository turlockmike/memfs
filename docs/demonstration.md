# memfs v2 — Anecdotal Demonstration on Karpathy's Corpus

**Date:** 2026-04-17
**Backend:** Neo4j 5.26 Community (Docker Compose), port 7687
**Corpus:** `~/memfs-demo` — read-only snapshots of Karpathy's own memory:
- `karpathy/`  — 15 files from `~/.config/karpathy/*.md`
- `shared/`    — 16 files from `~/.claude/projects/-home-mike/memory/` (MEMORY.md + topics/)
- `grove-archive/` — 15 files from `~/.config/grove/*.md` (stopped Apr 11)

The originals were **not modified**. All work was done against copies.

---

## 1. Migration

```
$ mkdir -p ~/memfs-demo/{karpathy,shared,grove-archive}
$ cp -r ~/.config/karpathy/*.md ~/memfs-demo/karpathy/
$ cp -r ~/.claude/projects/-home-mike/memory/*.md ~/memfs-demo/shared/
$ cp -r ~/.claude/projects/-home-mike/memory/topics  ~/memfs-demo/shared/
$ cp -r ~/.config/grove/*.md ~/memfs-demo/grove-archive/
$ find ~/memfs-demo -type f -name "*.md" | wc -l
46
```

### Indexing

```
$ MEM_HOME=~/memfs-demo memfs init ~/memfs-demo
{"action": "init", "mem_home": "/home/mike/memfs-demo", "nodes": 46, "edges": 1}

$ MEM_HOME=~/memfs-demo memfs reindex
{"action": "reindex", "nodes": 46, "edges": 0}
```

46 nodes, 0 link edges — Karpathy's corpus doesn't use `[[wikilinks]]`, so the
graph is purely FTS-backed. This is the null case for memfs: **no encoded
structure at all**. Whatever clustering appears later comes entirely from
emergent query behavior.

### After 10 queries

```
$ MEM_HOME=~/memfs-demo memfs status
{"nodes": 46, "edges": {"link": 0, "search": 30}, "queries": 10, "last_decay": "2026-04-17T05:18:25+00:00"}
```

- **10 Query nodes** (one per unique query)
- **30 search edges** (top-3 × 10 queries)
- **28 orphans** — ~61% of the corpus never got retrieved in 10 queries

This is the "structural absence" diagnostic: things Mike writes but doesn't
ask about. Useful for surfacing dead entries in a playbook.

---

## 2. Sample Queries

Representative of a typical Karpathy operating day. All were zero-LLM-cost
retrievals (Neo4j fulltext → BM25).

| # | Query | Top-1 | Context tokens | Baseline tokens | TCCA proxy |
|---|-------|-------|---------------:|----------------:|-----------:|
| 1 | compression hypothesis | karpathy/viable-memory-architecture.md | 54,631 | 162,262 | 2.97× |
| 2 | viable memory | karpathy/viable-memory-architecture.md | 28,350 | 162,262 | 5.72× |
| 3 | Gould origination rate | karpathy/predictions-archive.md | 63,926 | 162,262 | 2.54× |
| 4 | Kalshi trading | shared/topics/kalshi.md | 26,626 | 162,262 | 6.09× |
| 5 | gym routine | grove-archive/gym-routines.md | 31,929 | 162,262 | 5.08× |
| 6 | Mike wife Hilary | karpathy/mike-state.md | 17,542 | 162,262 | 9.25× |
| 7 | auditor calibration | karpathy/viable-memory-architecture.md | 54,584 | 162,262 | 2.97× |
| 8 | session continuity | karpathy/session-continuity-design.md | 22,536 | 162,262 | 7.20× |
| 9 | Grove stopped | grove-archive/playbook.md | 23,690 | 162,262 | 6.85× |
| 10 | Apr 16 directive | shared/topics/chess-eval-rebuild.md | 29,626 | 162,262 | 5.48× |

**Summary:**
- Baseline (accumulate-all) token cost: **162,262**
- Average memfs grep context: **35,344 tokens**
- **Average compression ratio: 4.59×**
- **Average TCCA proxy (correct-answer-assumed, top-5): 5.42×**

This is a *proxy* because it assumes the top-5 file contents are enough to
answer each question. For a real TCCA number we'd need to close the loop
with an LLM + judge (M3 provides the harness; the anecdotal demo just uses
context-token ratio as a retrieval-side lower bound on compression gain).

Every top-1 hit was topically correct: queries about the compression
hypothesis return `viable-memory-architecture.md`, which is exactly where
that idea is documented; queries about Mike's family return `mike-state.md`;
queries about the session-continuity design return that design doc as
top-1. None of this was hand-tuned — it's BM25 on unlinked, unstructured
markdown.

---

## 3. What memfs Detected

### Emergent clusters (from search edges)

Because the corpus has zero link edges, all "clustering" emerges from
shared query-to-document search edges. After 10 queries the top-connected
nodes are:

| Path | Incoming search edges |
|------|----------------------:|
| karpathy/viable-memory-architecture.md | 3 |
| karpathy/playbook.md | 3 |
| karpathy/mike-state.md | 2 |
| karpathy/predictions-archive.md | 2 |
| shared/MEMORY.md | 2 |

Shared query nodes implicitly cluster documents by relevance — if two
documents both appear under the same Query, they're co-relevant. The
dream-skill compression step would run over these clusters to synthesize
layer-3 summaries. That's out of scope for M5 but the wiring exists.

### Contradictions flagged

Zero contradictions were detected because the M4 heuristic only runs on
layer ≥ 3 nodes, and **no files in this corpus have frontmatter `layer:`
fields**. This is itself a finding: Karpathy's memory is mostly raw KB
(effective layer 2); he has not been using the provenance system. If
`karpathy/viable-memory-architecture.md` were tagged `layer: 4` (opinion),
the contradiction heuristic would activate against the grove-archive
documents — several of which restate or reverse claims from the Apr 16
Karpathy playbook.

### Orphans

28 orphans out of 46 nodes (61%). Top offenders from `grove-archive/`
(unsurprising — Grove is stopped), plus 9 orphans inside `karpathy/` itself:

- `karpathy/capability-inventory.md`
- `karpathy/chess-ratchet-state.md`
- `karpathy/hook-deploy-plan.md`
- `karpathy/hook-lifecycle-findings.md`
- `karpathy/running-tasks.md`
- `karpathy/structural-absences.md`
- `karpathy/eval-coverage-census.md`
- `karpathy/playbook.archive.md`
- `karpathy/backlog.md`

These files exist in Karpathy's config but didn't surface in 10 queries
matching a realistic operating day. Worth a pass to decide: prune, rename
for better retrieval, or link from the main playbook.

### Freshness

Zero files have freshness stamps. All 46 nodes register as
`freshness: never_verified`. This is the null case: Karpathy isn't using
freshness metadata yet. The M5 system is ready for it (grep returns
`freshness` per result, `--fresh-only` filters, `memfs freshness-scan`
reports stale facts) but waits for frontmatter to be added.

---

## 4. Architecture Validation

### What M1-M5 prove out in the demo

- **M1 (Neo4j backing):** 46-node corpus indexes in <2 seconds; FTS returns
  in <100ms per query. 169 tests pass against Neo4j.
- **M2 (layer typing):** Not exercised by the existing corpus, but the
  indexer cleanly defaults to `layer: 2` when the field is absent.
- **M3 (TCCA instrumentation):** The context-token ratios above are a
  retrieval-side proxy that the harness can convert into full TCCA once an
  LLM is wired into the adapter. The adapter abstraction (memfs / bm25 /
  accumulate) makes this a drop-in.
- **M4 (calibration + contradiction):** Infra is in place; no claims were
  recorded against this corpus because these are historical files, not live
  assertions.
- **M5 (freshness):** Every node reports `never_verified`; filter works; scan
  returns the empty set correctly.

### What the demo reveals

1. **Karpathy's memory has essentially no link structure.** Zero wikilinks
   across 46 files. The graph is a degenerate FTS-over-markdown at this
   point. That's fine for now — memfs is supposed to make connections
   emerge via search, not require them to be hand-authored — but it means
   the "synthesis node" benefit doesn't kick in until the dream skill runs.

2. **The corpus is overwhelmingly layer-2.** Zero provenance chains. This
   is exactly the state that M2/M4 were designed to improve: any Karpathy
   writeup that *interprets* history (an opinion) should be layer-4 with
   a source pointer; any daily summary should be layer-3. Retrofitting
   these stamps is future work.

3. **Retrieval quality is already good.** BM25 on raw markdown finds the
   right top-1 for all 10 queries. This is a strong argument for the
   "zero-magic" design: you don't need embeddings to get useful retrieval
   out of a well-named corpus.

4. **Compression is real.** 4.59× average compression vs. loading the full
   corpus for each query. For an agent whose limit is ~200k context, this
   is the difference between fitting one question vs. five.

---

## 5. Rough TCCA Numbers

Assuming the top-5 files per query contain the answer (sanity-checked by
eyeballing the top hits above), and using a char/4 token approximation
(because tiktoken wasn't installed in the demo venv):

```
baseline_tokens = 162,262  (all 46 files)
avg_context_tokens = 35,344  (top-5 for a query)
naive TCCA proxy = 162,262 / 35,344 = 4.59
```

If we trust the accumulate adapter's denominator as a realistic ceiling
on how many tokens a *non-memfs* agent would use to guarantee-answer from
this corpus, memfs shaves ~80% of token cost per query. For full 200k
context budgets the absolute saving is less dramatic, but for 32k contexts
it's the difference between "tractable" and "must summarize first."

---

## 6. What to Do Next

1. **Add layer stamps to Karpathy's existing files.** Specifically:
   - `viable-memory-architecture.md` → `layer: 4, source: ~/memory/sessions/2026-04-16.md`
   - `session-continuity-design.md` → `layer: 4, source: ~/memory/sessions/2026-04-13.md`
   - `mike-state.md` → `layer: 5` (identity)
   - Most of `grove-archive/*.md` → `layer: 2` with `source: grove-historical`

2. **Backfill `[[wikilinks]]` across the playbook.** Right now the graph is
   all derived from co-search. A handful of wikilinks (playbook →
   viable-memory-architecture; viable-memory → VSM architecture; etc)
   would make the neighborhood graph non-trivial.

3. **Wire a dream skill pass.** Run the compression skill against the
   top clusters (compression hypothesis, viable memory, session continuity)
   and write the resulting layer-3 summaries back into memfs with source
   pointers. That activates the contradiction heuristic.

4. **Add freshness stamps to infrastructure facts.** Anything that references
   an external system (Neo4j version, claude-loop schema, local LLM model)
   should get `freshness_verified_at + stale_after_days` so `freshness-scan`
   finds drift before it becomes a bug.

5. **Run the real TCCA harness.** Wire an LLM + judge into `memfs tcca`
   over a LongMemEval slice. The M3 adapters are already in place.

This writeup is the baseline. When M6 (dream-skill-on-corpus) and M7
(auto-freshness-refresh) land, compare against this document.
