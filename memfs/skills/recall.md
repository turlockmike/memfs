# memfs-recall — Context Retrieval Skill

A skill that retrieves the most relevant context from memory for a given task. Not just "search for files" — it's an iterative exploration that builds a rich context window from the memory graph.

## When to Run
- Before starting any task that needs background context
- When the agent needs to answer a question from memory
- At session start to load baseline context ("wake-up")

## Inputs
- A task description or question (what context is needed?)
- Optional: specific directory to search within
- Optional: max token budget for context

## Process

### 1. Initial Search
```bash
memfs grep "<task description or key terms>"
```

This returns results with full neighborhood: siblings, index, links_to, linked_from.

### 2. Evaluate Results

For each top result, decide:
- **Read it?** Does the title/snippet suggest it's relevant?
- **Follow links?** Do any `links_to` or `linked_from` look promising?
- **Explore siblings?** Does the directory index suggest related content?

### 3. Expand Context (Iterative)

Based on the neighborhood of the first results:

**Follow outgoing links:**
If a result links to `[[people/ken.md]]` and the task involves Ken, read that file.

**Check siblings:**
If the result is in `learning/` and the directory has an `srs-methods.md` sibling that looks relevant, read it.

**Follow backlinks:**
If something important links TO the result, that linking file might provide broader context.

**Search within a directory:**
```bash
memfs grep "<refined query>" | jq 'select(.directory == "learning")'
```

### 4. Build Context Window (Summary-First)

For each file to include, check if a `.summary.md` exists or if the file has `type: summary` in frontmatter. **Always prefer the summary.** Only read the full source if the summary doesn't contain enough detail for the task.

```
[Context from memory]

## learning/kanji.md (Kanji Study) [summary]
Key Decisions: Lessons grouped by radical, SRS intervals 1d/3d/7d/14d/30d, 5 kanji per lesson
Connections: [[srs-methods]], [[projects/satori]]
(full source: sessions/2026-04-12-kanji-curriculum.md)

## learning/srs-methods.md (SRS Methods)
<file content — short file, no summary needed>

## projects/satori.md (Satori App) [summary]
Kanji curriculum app, Next.js + IndexedDB, pre-computed curriculum
(full source: projects/satori-full.md)
```

This keeps the context window compact. A 5-file context built from summaries might be ~1000 tokens. The same 5 files at full resolution might be ~8000 tokens. The agent gets the same information density at 1/8 the cost.

**When to read the full source:** If the task requires specific details not in the summary (exact code, precise numbers, full conversation transcript), follow the `source:` link and read the original.

### 5. Wake-Up Mode (Session Start)

When run without a specific task, load general context:

```bash
# Top files by search frequency (most useful over time)
sqlite3 $MEM_HOME/.mem/memory.db \
  "SELECT path, title, description, search_count FROM nodes WHERE search_count > 0 ORDER BY search_count DESC LIMIT 10"

# Recently modified (what was I working on?)
sqlite3 $MEM_HOME/.mem/memory.db \
  "SELECT path, title, description, modified_at FROM nodes ORDER BY modified_at DESC LIMIT 10"

# Directory index files (high-level map)
sqlite3 $MEM_HOME/.mem/memory.db \
  "SELECT path, title, description FROM nodes WHERE path LIKE '%/index.md' ORDER BY path"
```

Format as a compact summary (~800 tokens):
```
[Memory Overview]
Top areas: learning/ (kanji, SRS, vocabulary), projects/ (satori), people/ (ken, john)
Recent work: kanji.md (today), satori.md (yesterday)
Active connections: kanji↔satori (strong), ken↔meetings (moderate)
```

## Output
A structured context block ready to prepend to the agent's prompt. The agent uses this context to inform its work — it doesn't need to search again for things it already loaded.

## Key Principle
Recall is a search → explore → assemble pipeline. The initial `memfs grep` is just the starting point. The neighborhood context (siblings, links, index) tells the agent WHERE to look next. The agent navigates the graph like browsing a wiki — each file reveals more connections to follow.

## Stopping Condition
- Token budget reached (default: 4000 tokens)
- No more promising links to follow
- 3 iterations of expansion without finding new relevant content
