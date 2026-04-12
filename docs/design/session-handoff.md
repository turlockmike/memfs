---
title: memfs — Complete Session Handoff Document
date: 2026-04-12
description: Everything needed to continue building memfs and the para-bench after session reset
---

# memfs Session Handoff — 2026-04-12

## What We Built Today

### memfs — Unix-Native Memory Filesystem for LLM Agents

**Repo:** https://github.com/turlockmike/memfs
**Local clone:** /tmp/memfs (may not survive reboot — re-clone from GitHub)
**127 tests passing, 10 commits, ~2500 lines of code + docs**

### The Core Idea

LLMs don't need human-like layered memory. They need efficient search over a file hierarchy with connection awareness. The filesystem IS the graph. The agent reads and writes files normally with any tool. The only agent-facing command is `memfs grep <query>`. Everything else is invisible infrastructure.

**Agent system prompt (3 sentences):**
> Your memory lives in `$MEM_HOME`. Read and write files normally with any tool. Use `memfs grep <query>` to search — connections between files strengthen when you search for them and weaken over time.

### Architecture

```
Any LLM Agent / Human / IDE
        |  normal file I/O
        v
   Filesystem (MEM_HOME)
   projects/  people/  concepts/
        |  watchdog FSEvents
        v
   memfs daemon (background)
   - watches file changes → updates .mem/memory.db
   - parses [[wikilinks]] into graph edges
   - runs power-law decay on schedule
```

- Files are source of truth — `.mem/memory.db` is a derived SQLite index, rebuildable via `memfs reindex`
- Harness-agnostic — works with Claude Code, GPT, Gemini, any agent that can read/write files
- `.mem/` follows `.git/` convention — hidden, derived metadata

### What's In The Repo

```
turlockmike/memfs/
├── memfs/
│   ├── __init__.py
│   ├── cli.py          # CLI: init, grep, ls, status, watch, skills, reindex, _decay
│   ├── db.py           # SQLite schema, WAL mode, CRUD
│   ├── parser.py       # YAML frontmatter, [[link]] extraction, JSONL parsing, content hash
│   ├── paths.py        # Path normalization (relative to MEM_HOME, no .. allowed)
│   ├── indexer.py       # Directory scanning, .memignore, link edges, broken link detection
│   ├── search.py       # FTS5 + vector RRF hybrid, temporal boost, query nodes, search edges, neighborhood context
│   ├── decay.py        # Power-law decay, spacing-effect increments
│   ├── embeddings.py   # all-MiniLM-L6-v2, cosine search, batch embed
│   ├── watcher.py      # watchdog daemon, lifecycle (--daemon/--stop/--status)
│   ├── eval.py         # LongMemEval: ingest, Recall@k, QA generation, LLM-as-judge
│   ├── eval_cli.py     # Eval CLI: recall, qa, score subcommands
│   └── skills/
│       ├── dream.md    # Memory consolidation skill
│       └── recall.md   # Context retrieval skill
├── tests/              # 127 tests across 9 files
├── docs/
│   ├── design/
│   │   └── mem-cli-design.md    # Full architecture plan
│   └── research/
│       ├── mastra-memory-system.md
│       ├── llm-memory-architectures.md
│       ├── filesystem-memory-search.md
│       ├── memory-eval-benchmarks.md
│       └── neuroscience-memory-principles.md
├── pyproject.toml
├── README.md
├── LICENSE (MIT)
└── .gitignore
```

### Concepts Not Yet Implemented (From Tiago Review)

**Directory-level weighting:** Directories can have a `weight:` field in their `index.md` frontmatter. All files in that directory get a retrieval boost multiplied by that weight. E.g., `weight: 2.0` in `projects/index.md` means project files are twice as likely to surface in grep. This is NOT hardcoded PARA — each agent defines its own hierarchy semantics. An agent doing code review might weight `recent-prs/` higher. An agent doing research might weight `sources/` higher. The hierarchy is meaningful to retrieval without the system prescribing what it should be. Implementation: in `_get_neighborhood()` or in `grep()`'s scoring, read `index.md` frontmatter for the result's directory and apply the multiplier.

**Three-layer summaries:** `.summary.md` files should contain: exec summary (Layer 4, ~50 tokens), key passages (Layer 2-3, ~150 tokens), source backlink (Layer 1, full content). Recall reads top-down and stops at the resolution that answers the question.

**Synthesis nodes:** Dream should create new files (type: synthesis) that capture patterns across multiple source files. These are the highest-value output of consolidation — emergent understanding that doesn't exist in any single source.

### Key Design Decisions (with rationale)

**Two edge types only:**
- `link` — from `[[wikilinks]]` in file content. LLM's deliberate judgment. Power-law decay with floor of 0.5.
- `search` — from `memfs grep` query node → top-3 results. Organic, decays fully. Rank-weighted (1.0, 0.66, 0.33).

**No result↔result edges.** Per Anderson's fan effect (1974): nodes with too many associations become harder to retrieve. Edges connect query→result only. Files that are repeatedly relevant to similar queries develop strong incoming search edges from overlapping query nodes — the associative path goes THROUGH shared query nodes.

**Power-law decay, not exponential.** Per Ebbinghaus/Murre replication data: single exponential is the one model the forgetting data consistently rejects. Formula: `strength * (1 + 0.1 * days)^-0.5`. Half-life ~30 days.

**Spacing-effect increments.** Accessing things together rapidly gives diminishing returns. Gap between accesses matters: `0.05 * (1 + log(1 + days_gap)) * schema_multiplier`. Same-directory files get 1.5x bonus.

**Search results return neighborhood context:** directory, siblings (with title + description), index.md, links_to, linked_from. The agent sees the graph structure around each result, not just the file.

**Bundled skills:** `memfs skills setup` installs dream + recall skills into the agent framework. `memfs skills dream` outputs the skill markdown. Auto-detects Claude Code.

### SQLite Schema

```sql
CREATE TABLE nodes (
    path TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,           -- from frontmatter, max 200 chars
    created_at TEXT NOT NULL,
    modified_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedded_at TEXT,
    last_searched TEXT,          -- updated when file appears in grep results
    search_count INTEGER DEFAULT 0,
    date_hint TEXT               -- from frontmatter date: field
);

CREATE TABLE edges (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('link', 'search')),
    strength REAL NOT NULL DEFAULT 1.0,
    last_activated TEXT,
    access_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, target, type)
);

CREATE TABLE queries (
    id TEXT PRIMARY KEY,         -- SHA-256 of normalized query
    query_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used TEXT NOT NULL,
    use_count INTEGER DEFAULT 1
);

CREATE VIRTUAL TABLE fts USING fts5(path, title, content, tokenize='porter unicode61');

CREATE TABLE embeddings (path TEXT PRIMARY KEY, vector BLOB NOT NULL, model TEXT NOT NULL, created_at TEXT NOT NULL);
```

### Benchmark Results

LongMemEval oracle (500 questions, 940 sessions):

| Metric | FTS5 only | FTS5 + Vectors (RRF) |
|--------|-----------|---------------------|
| Recall@5 | 11.8% | **66.6%** |
| MRR | 10.4% | **49.7%** |

Per task type with vectors:
- single-session-assistant: 89.3%
- knowledge-update: 80.8%
- multi-session: 72.2%
- temporal-reasoning: 61.7%
- single-session-user: 48.6%
- single-session-preference: 26.7%

### Dependencies

**Required:** watchdog>=4.0, pyyaml>=6.0
**For vectors:** sentence-transformers<4.0 (pinned to avoid torchcodec conflict on macOS)
**For eval:** claude CLI or ollama at 192.168.4.30

---

## The Two Skills (The Unique Part)

### memfs-recall — Context Retrieval Skill

Not "search for files" — iterative exploration that builds a rich context window.

1. `memfs grep <query>` → get results with neighborhood
2. Evaluate: follow links? Check siblings? Read the directory index?
3. Expand context iteratively — follow outgoing links, check siblings, follow backlinks
4. Assemble context window ordered by relevance
5. Stop when token budget reached or no more promising leads

**Wake-up mode** (session start): top files by search_count + recently modified + directory indexes → compact ~800 token summary.

### memfs-dream — Memory Consolidation Skill

Reflective skill that improves the memory graph. Like sleep consolidation.

1. Gather session activity: recent queries, recently modified files, search hits, orphans
2. Evaluate and improve:
   - Missing connections → add `[[links]]`
   - Files that should split → split + link
   - Files that should merge → merge
   - Orphans → link, move, or archive
   - Missing index.md files → create them
   - Wrong directory → move
3. Write a consolidation log

**Key insight:** The agent IS the judge. memfs provides data (orphans, search patterns, edge strengths). The agent provides judgment.

**Progressive Summarization (added late in session):** Dream generates `.summary.md` files for long sessions/docs with a `source:` frontmatter field pointing back to the full content. Recall reads summaries first (~200 tokens) and only follows the source link when detail is needed (~2000+ tokens). This is Tiago Forte's progressive summarization applied to agent memory — two resolution levels in the same graph. A 5-file context from summaries is ~1000 tokens vs ~8000 at full resolution. Same information density, 1/8 the cost.

### Tiago Forte's Review (End of Session)

Dispatched the tiago-forte agent to review the full architecture. Key feedback:

**1. Progressive summarization needs three layers, not two.**
Currently: raw source (L1) → exec summary (L4). Missing: bolded/highlighted key passages (L2-3). Fix: `.summary.md` should contain exec summary at top PLUS the ~15% of source content that earned highlighting. Three resolutions: exec summary → key passages → full source.

**2. Dream should synthesize, not just defragment.**
Moving files and fixing links is maintenance. Real consolidation creates NEW synthesis nodes. If the agent ran 12 searches about auth patterns this week, dream should notice that search cluster and create `auth-patterns.md` that captures the emergent pattern with wikilinks back to sources.

**3. Directory hierarchy is agent-dependent, not PARA-hardcoded.**
Tiago suggested organizing by actionability (projects/areas/resources/archive). Captain's correction: the folder structure depends on the agent's domain and purpose. The important architectural insight is that **directories themselves can have weightings** — not just individual nodes.

**4. Directory-level weighting (new concept — not yet implemented).**
A `.memweight` or `weight:` field in `index.md` frontmatter could set a retrieval boost for all files in that directory. E.g., `weight: 2.0` in `projects/index.md` means all files under `projects/` get a 2x boost in grep results. This lets each agent define its own hierarchy semantics without hardcoding any structure. The hierarchy becomes meaningful to retrieval without the system dictating what it should be.

**5. The killer metric for para-bench is the longitudinal delta.**
Does the gap between recall+dream and raw grep GROW as the session progresses? If dream consolidation works, travelers 7-9 should benefit MORE than travelers 2-3. That compounding curve should be the primary metric.

**6. Benchmark naming may need revision.**
para-bench implies testing PARA specifically, but we're testing whether skills (recall + dream) improve task performance. Either rename to reflect what we're testing, or add a condition that tests hierarchy organization effects. Deferred to benchmark design phase.

---

## What's Next: The "para-bench" Benchmark

### The Problem

Every existing memory benchmark tests RETRIEVAL — "can you find the right file?" Nobody tests whether memory makes agents BETTER AT WORK over time. MemoryArena (Feb 2026) found that memory systems don't consistently beat full-context stuffing. The retrieval isn't the bottleneck — it's what the agent DOES with what it retrieves.

### The Benchmark Design

**Name candidates:** para-bench, memprove, compound, longwork
**License:** Should be open (CC-BY 4.0 for data, MIT for code)

A multi-session work simulation. Not 500 independent questions — a sequence of realistic work sessions where later sessions depend on earlier ones.

**Phase structure:**
```
Phase 1: SEED — Agent receives information
Phase 2: TASK — Agent does work that benefits from Phase 1
Phase 3: DREAM — Agent consolidates memory
Phase 4: TASK — Harder work needing accumulated context
Phase 5: CURVEBALL — Information changes
Phase 6: TASK — Work requiring updated understanding
```

**Each TASK evaluation has:**
- A prompt (realistic work task)
- Prior context the agent SHOULD know from earlier phases
- A weighted rubric (not binary yes/no)
- Maximum score

**Three experimental conditions:**
1. No memory — agent starts fresh each session
2. memfs grep only — raw search, no skills
3. memfs + recall + dream — full framework

**Delta between conditions 2 and 3 = value of the skills.**

### Adapting MemoryArena — The Travel Planner Benchmark

**Dataset:** HuggingFace `ZexueHe/memoryarena`, CC-BY 4.0
**Domain:** Group Travel Planner (270 tasks, cumulative constraints, self-contained, no external APIs)
**No evaluation harness code exists** — their "Code" link is broken. We build the harness from scratch.

**Their key finding to challenge:** Memory systems don't beat full-context. Our hypothesis: skills that ASSEMBLE context intelligently (not just retrieve) will beat both raw retrieval AND full-context.

#### Why Travel Planner Is Perfect

It's the strongest scenario for testing memory skills because:
- **Cumulative state:** Each new traveler joining references prior travelers' decisions. Session 7 may need to recall what session 3 decided.
- **Constraint propagation:** "Stay at a hotel 2 tiers above Person 3's choice" — the agent MUST recall a specific prior decision, not just a fact.
- **Natural connections:** Travelers share flights, restaurants, attractions — the graph of connections between session files is rich and meaningful.
- **Deterministic scoring:** Answers are structured itineraries (flights, hotels, restaurants). Exact match against ground truth — no LLM judge needed.
- **Self-contained:** Uses TravelPlanner's static database. No external APIs, no web access, fully reproducible.

#### Implementation Plan

**Step 1: Download + Ingest**
```bash
# Download from HuggingFace
pip install datasets
python3 -c "from datasets import load_dataset; ds = load_dataset('ZexueHe/memoryarena', 'group_travel_planner'); ds['test'].to_json('travel_planner.jsonl')"
```

Each task has a `base_person` (Jennifer's existing 3-day itinerary) and 5-9 sequential `questions` (new travelers joining). The `answers` are structured itineraries.

**Step 2: Convert to memfs Workflow**

For each task:
1. Write `base_person` itinerary as a markdown file → `sessions/base-jennifer.md`
2. For each subtask (new traveler joining):
   a. Write the traveler's request as a new file → `sessions/traveler-eric.md`
   b. **Recall skill:** Search memory for relevant prior context (base itinerary + prior travelers' constraints)
   c. **Agent generates itinerary** using retrieved context
   d. Write the agent's answer as a file → `sessions/traveler-eric-itinerary.md`
   e. Score against ground truth

After all subtasks in a task:
3. **Dream skill:** Consolidate — add links between related traveler files, create index.md summarizing the group trip

**Step 3: Three Experimental Conditions**

| Condition | Memory | Recall Skill | Dream Skill | Context Strategy |
|-----------|--------|-------------|-------------|-----------------|
| **A: No memory** | None | No | No | Only current subtask prompt |
| **B: Full context** | None | No | No | ALL prior subtasks + answers stuffed into prompt |
| **C: Raw grep** | memfs | No | No | `memfs grep` top-5, dump into prompt |
| **D: Recall skill** | memfs | Yes | No | Iterative exploration, neighborhood-aware context assembly |
| **E: Recall + Dream** | memfs | Yes | Yes | Same as D, but dream runs between tasks within a group |

Conditions A and B are the baselines from the MemoryArena paper. C is naive retrieval. D tests whether the recall skill assembles better context. E tests whether dream consolidation between travelers helps later travelers.

**Step 4: Metrics**

- **Success Rate (SR):** Fraction of tasks where ALL subtasks are correct. Binary per task.
- **Progress Score (PS):** Fraction of subtasks correct. Continuous per task.
- **Soft Progress Score:** Partial credit for constraint satisfaction (e.g., right hotel tier but wrong specific hotel).
- **Context Efficiency:** Tokens of context used per correct subtask. Lower is better — the recall skill should use LESS context than full-context stuffing while achieving equal or better accuracy.
- **Dream Delta:** For condition E, compare PS of travelers 5-9 (after dream cycles) vs. travelers 5-9 in condition D (no dream). Does consolidation help the later, harder subtasks?

**Step 5: The Key Comparison**

MemoryArena's headline result: external memory systems (MemGPT, Mem0, etc.) DON'T consistently beat full context (condition B).

Our hypothesis:
- Condition D (recall skill) > Condition C (raw grep) — intelligent context assembly beats naive top-k
- Condition D (recall skill) ≥ Condition B (full context) — with less tokens
- Condition E (recall + dream) > Condition D — consolidation helps on later subtasks

If D beats B with fewer tokens, that's the proof: **skills that navigate the memory graph produce better context than stuffing everything in.**

#### Concrete Harness Architecture

```python
# para-bench/harness.py (pseudocode)

class MemoryCondition:
    """Interface for each experimental condition."""
    def setup(self, task): ...
    def get_context(self, subtask_prompt, prior_subtasks): ...
    def record_answer(self, subtask_idx, answer): ...
    def between_subtasks(self): ...  # dream hook

class NoMemory(MemoryCondition): ...
class FullContext(MemoryCondition): ...
class RawGrep(MemoryCondition): ...
class RecallSkill(MemoryCondition): ...
class RecallAndDream(MemoryCondition): ...

def run_task(task, condition, agent_llm):
    condition.setup(task)  # write base itinerary to memory
    results = []
    for i, (question, expected) in enumerate(zip(task.questions, task.answers)):
        context = condition.get_context(question, results)
        prompt = build_prompt(question, context)
        answer = agent_llm(prompt)
        score = evaluate(answer, expected)
        results.append({"subtask": i, "answer": answer, "score": score})
        condition.record_answer(i, answer)
        condition.between_subtasks()  # dream if applicable
    return results

def run_benchmark(tasks, conditions, agent_llm):
    for condition in conditions:
        for task in tasks:
            results = run_task(task, condition, agent_llm)
            # compute SR, PS, context efficiency
```

#### Timeline

1. **Day 1:** Create repo, download data, build harness skeleton, implement NoMemory and FullContext conditions
2. **Day 2:** Implement RawGrep and RecallSkill conditions, run on 10 tasks
3. **Day 3:** Implement RecallAndDream, run full 270 tasks across all conditions
4. **Day 4:** Analyze results, write up findings

### MemoryArena Data Format (Travel Planner)

```json
{
  "id": 1,
  "base_person": {
    "name": "Jennifer",
    "original_query": "...",
    "daily_plans": [{"day": 1, "flights": [...], "restaurants": [...], "attractions": [...]}]
  },
  "questions": ["I am Eric, joining Jennifer...", "I am Sarah, joining..."],
  "answers": [{"traveler": "Eric", "daily_plans": [...]}, ...]
}
```

270 tasks, 5-9 travelers per task chained in sequence. Each new traveler's itinerary must satisfy their preferences AND be compatible with prior travelers' bookings.

### Research Docs Available

All saved in the workspace and in the memfs repo:

- `doc/research/mastra-memory-system.md` — OM architecture, 94.87% claim analysis, benchmark methodology gaps
- `doc/research/llm-memory-architectures.md` — MemGPT, GraphRAG, Mem0, Zep, Cognee, LangGraph comparison
- `doc/research/filesystem-memory-search.md` — FTS5, embeddings, graph structure, maintenance strategies
- `doc/research/memory-eval-benchmarks.md` — 15 runnable benchmarks across 4 categories
- `doc/research/neuroscience-memory-principles.md` — Power-law decay, spacing effect, consolidation, engrams, fan effect
- `doc/research/multi-session-agent-benchmarks.md` — MemoryArena, TIGM, Evo-Memory, SWE-Bench-CL, LifeBench
- `doc/research/benchmark-scenarios.md` — Concrete scenarios from all existing benchmarks

### Mastra's Benchmark Methodology (What NOT to Do)

- They only used LongMemEval (one benchmark, one metric)
- Their 94.87% is on a dataset they modified to fix questions they deemed broken
- Consolidation (observer + reflector) was pre-computed offline, not tested live
- No ablation between observer and reflector published (they have the data internally)
- gpt-4o score is actually 84.23% — the 94.87% headline is gpt-5-mini
- No longitudinal testing of whether consolidation improves over time

---

## Captain's Design Philosophy (Capture for Next Session)

These came up throughout the conversation and should guide future work:

1. **"LLMs don't need human-like layered memory"** — one layer is enough. The bottleneck is the attention window, not the memory architecture. Compression (like vector search) helps explore efficiently.

2. **"The filesystem IS the graph"** — no need for a separate graph DB. Files are nodes, directories are clusters, `[[links]]` are edges, SQLite is the derived index.

3. **"The agent should manage its own memory"** — the LLM is the best judge of where to store information. The tool provides data, the agent provides judgment.

4. **"Memory is a means, not an end"** — the purpose isn't to have memory, it's to do useful tasks quickly and accurately. Benchmark accordingly.

5. **"What's unique isn't the search — it's the skills"** — vector grep and filesystem hierarchy aren't novel. The recall skill (iterative context assembly) and dream skill (reflective consolidation) are the differentiators.

6. **"It has to work with any harness"** — no Claude Code hooks, no framework-specific integration. Pure filesystem + one CLI command.

7. **"Use Unix primitives"** — commands should mirror mv, ln, cat, grep, ls. The filesystem already solved distributed state.

8. **"Prove it works on real work, not retrieval benchmarks"** — the benchmark should measure task completion quality with vs. without memory, over multiple sessions.

---

## Immediate Next Steps

### 1. Build the para-bench repo
- Create `turlockmike/para-bench` (or whatever name is chosen)
- Download MemoryArena travel planner data from HuggingFace
- Build evaluation harness that feeds subtasks sequentially
- Implement three conditions: no memory, raw grep, recall + dream
- Run first comparison, get a number

### 2. Harden memfs for real use
- Publish to PyPI (`pip install memfs`)
- Add `memfs init --daemon` one-shot setup
- Write GitHub Actions CI (tests on push)
- Consider: should the watcher auto-embed new files if sentence-transformers is installed?

### 3. Test on real workspace
- Run `memfs init ~/workspace/doc` and `memfs watch --daemon`
- Use `memfs grep` in actual work for a week
- Run `memfs skills dream` at end of each day
- Track: does retrieval improve? Does the agent find things faster?

### 4. Skills refinement
- The dream and recall skills are currently markdown descriptions. They need to be tested with a real agent doing real work.
- The recall skill's stopping condition (token budget, iteration limit) needs tuning.
- The dream skill needs a concrete consolidation log format.

---

## Memory / Context for Next Session

**Project memory file:** `~/.claude/projects/-Users-michaeldarmousseh-workspace/memory/project_mem-cli.md`
**Plan file:** `~/.claude/plans/splendid-crafting-penguin.md`
**Dashboard copy:** `~/workspace/doc/synthesis/mem-cli-design.md`
**This handoff:** `~/workspace/doc/synthesis/memfs-session-handoff.md`

The repo at https://github.com/turlockmike/memfs has everything. Clone it, run `python3 -m pytest tests/` to verify (127 should pass), and pick up from "build the para-bench."
