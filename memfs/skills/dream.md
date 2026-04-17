# memfs-dream — Memory Consolidation Skill

A reflective skill that reviews how memory was used in a session and improves the memory graph. Like sleep consolidation for the brain — replay, reorganize, strengthen what matters, prune what doesn't.

## When to Run
- End of a work session (as a hook or manual invocation)
- Periodically (daily, weekly)
- When `memfs status` shows high orphan count or stale index

## Inputs
- The session's activity: what files were read, written, searched for
- `memfs status` output (node/edge counts, orphans, last decay)
- `memfs ls --orphans` output
- Recent queries from the queries table

## Process

### 1. Gather Session Activity

On the Neo4j backend, the whole briefing is one command:

```bash
memfs dream-briefing              # NDJSON: orphan | merge | split | link | index | stale
memfs status                      # counts, last decay
memfs ls --orphans                # just orphans, if you want them isolated
memfs link-suggest                # NDJSON link candidates from content similarity
                                  #   (use when SEARCH traffic is too low for
                                  #    dream's co-search-based link candidates)
```

`dream-briefing` already calls `find_content_similar_unlinked` internally, so
the `"candidate_type":"link"` lines it emits include both co-search and
content-similarity sources. `link-suggest` is just that slice, standalone.

### 2. Evaluate and Improve

For each category, the agent makes judgment calls:

**Missing connections:**
- The dream briefing emits `"candidate_type":"link"` entries. Two sources:
  - `"source":"cosearch"` — pairs co-occurring in top-3 SEARCH results across N+ queries
  - `"source":"content_similarity"` — pairs with overlapping rare tokens in title+description+content-head
- For **high-confidence** link candidates (priority ≥ 0.5), pipe straight through:
  ```bash
  memfs dream-briefing | jq -c 'select(.candidate_type=="link" and .priority>=0.5)' \
    | memfs link-apply --from-stdin
  ```
  Edges land with `source=<candidate source>` so they persist through file re-indexing.
- For **lower-confidence** candidates, read the pair, judge, and either
  - add an authored `[[link]]` in the source file (survives as `source=authored`), or
  - apply via `memfs link-apply --from-stdin` (survives as graph-derived), or
  - drop it.
- `authored` edges are cleared on any file re-index; graph-derived edges are durable.

**Files that should be split:**
- Any file over ~500 lines or covering 2+ distinct topics?
- Split into focused files with `[[links]]` between them.

**Files that should be merged:**
- Are there two files with >80% overlapping content?
- Merge the smaller into the larger, add any unique content, delete the smaller.

**Orphans to triage:**
- For each orphan: is it useful? If yes, add it to a relevant directory and link it.
- If not useful, archive or delete it.

**Directory organization:**
- Any directory with >20 files and no subdirectories? Consider splitting.
- Any file clearly in the wrong directory? Move it.

**Index files:**
- Does every directory with >3 files have an `index.md`?
- If not, create one with a description of what the directory contains.

**Synthesize search clusters:**
- Look at recent queries. Are there 3+ queries that touched overlapping sets of files?
- If yes, that's a theme the agent is working on. Create a synthesis node:
  ```markdown
  ---
  title: Authentication Patterns
  type: synthesis
  sources: ["sessions/auth-audit.md", "projects/api-redesign.md", "resources/oauth-spec.md"]
  ---
  # Authentication Patterns
  Across this week's work, three patterns emerged...
  - [[sessions/auth-audit]] — found inconsistent token handling
  - [[projects/api-redesign]] — new API needs OAuth2 PKCE
  - [[resources/oauth-spec]] — reference spec for PKCE flow
  ```
- Synthesis nodes are the highest-value output of dream. They capture emergent understanding that doesn't exist in any single source file.

**Review decaying connections:**
- In Neo4j:
  ```cypher
  MATCH ()-[r:LINK]->() WHERE r.strength < 0.3 AND r.strength > 0.05
  RETURN startNode(r).path, endNode(r).path, r.strength, r.source
  ORDER BY r.strength ASC
  ```
- For each fading connection: is it still relevant? If yes, re-strengthen by
  adding an explicit `[[link]]` in the source file (authored) or re-applying
  via `memfs link-apply`. If no, let it decay naturally.

### 3. Summarize Sessions (Progressive Summarization)

For each long file created or heavily modified during the session, generate a summary file:

```markdown
---
title: "Session: Kanji Curriculum Design"
date: 2026-04-12
description: Designed the first 50 lessons of the kanji curriculum with SRS intervals
source: sessions/2026-04-12-kanji-curriculum.md
type: summary
---

## Summary
Designed 50 lessons grouped by radical (not JLPT). SRS intervals: 1/3/7/14/30 days. 5 kanji per lesson max. Open question: whether vocab cards include kanji breakdowns.

## Key Passages
> "After testing both orderings, radical-based grouping had 40% better retention in the first week because students could see the shared components."

> "The 5-kanji limit came from cognitive load research — Cowan's 4±1 chunks. We round up to 5 because each kanji shares a radical with at least one other in the lesson."

## Connections
- [[learning/srs-methods]] — interval schedule based on this
- [[projects/satori]] — implementing this curriculum

## Open Questions
- Should vocabulary cards include kanji breakdowns?
```

Three layers in one file:
1. **Summary** (~50 tokens) — exec synthesis, read this first
2. **Key Passages** (~150 tokens) — the 15% of the source that earned highlighting
3. **source: link** — follow to full content (~2000+ tokens) only when needed

The recall skill reads top-down: summary answers most questions. Key passages provide evidence. Full source is the last resort.

**When to summarize:**
- Any file over ~500 tokens that was created or heavily modified this session
- Meeting notes, long research captures, verbose logs, session transcripts
- NOT short files, config files, or files that are already concise

**Where summaries live:**
```
sessions/
├── 2026-04-12-kanji-curriculum.md          # full source (truth)
├── 2026-04-12-kanji-curriculum.summary.md  # dream-generated summary
```

This is Tiago Forte's progressive summarization: raw → summarized. The full source stays untouched. The summary is a new file with a backlink. The memory graph now has two resolutions — summaries for fast scanning, full sources for deep dives.

### 4. Record What Changed

Write a brief consolidation log:
```markdown
---
title: Dream Cycle 2026-04-12
date: 2026-04-12
---
## Memory Consolidation

- Summarized 3 session files (saved ~4000 tokens on future recall)
- Added 3 links between learning/ files
- Split projects/big-project.md into architecture.md and implementation.md
- Archived 2 orphan files (old meeting scratch notes)
- Created index.md for tools/ directory
```

## Output
The skill modifies memory files directly (normal file I/O — the watcher handles indexing). It produces:
1. **Summary files** for long sessions (with `source:` backlink to full content)
2. New `[[links]]` between related files
3. Reorganized files (moved, split, merged)
4. A consolidation log

## Key Principle
The agent IS the judge. It reads the files, decides what connections make sense, and makes the changes. memfs provides the data (orphans, search patterns, edge strengths); the agent provides the judgment.

The recall skill automatically prefers summaries over full sources — reading the graph at the resolution the task requires. Only when the summary isn't enough does it follow the `source:` link to the full content.
