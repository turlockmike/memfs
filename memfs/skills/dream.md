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
```bash
# What was searched this session?
sqlite3 $MEM_HOME/.mem/memory.db "SELECT query_text, use_count FROM queries ORDER BY last_used DESC LIMIT 20"

# What files were found and used?
sqlite3 $MEM_HOME/.mem/memory.db "SELECT path, search_count, last_searched FROM nodes WHERE last_searched IS NOT NULL ORDER BY last_searched DESC LIMIT 20"

# What files were modified recently? (proxy for "worked on")
sqlite3 $MEM_HOME/.mem/memory.db "SELECT path, modified_at FROM nodes ORDER BY modified_at DESC LIMIT 20"

# Orphans
memfs ls --orphans
```

### 2. Evaluate and Improve

For each category, the agent makes judgment calls:

**Missing connections:**
- Read pairs of recently co-searched files. Do they reference related concepts?
- If yes, add a `[[link]]` from one to the other. The watcher will create the edge.

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

## Key Decisions
- Lessons grouped by radical, not JLPT level
- SRS intervals: 1d, 3d, 7d, 14d, 30d
- Each lesson introduces 5 kanji max

## Open Questions
- Should vocabulary cards include kanji breakdowns?

## Connections
- [[learning/srs-methods]] — interval schedule based on this
- [[projects/satori]] — implementing this curriculum
```

The `source:` frontmatter field points back to the full file. The recall skill reads the summary first (~200 tokens). If it needs detail, it follows the source link to the full content (~2000+ tokens).

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
