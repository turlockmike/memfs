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

### 3. Record What Changed

Write a brief consolidation log:
```markdown
---
title: Dream Cycle 2026-04-12
date: 2026-04-12
---
## Memory Consolidation

- Added 3 links between learning/ files
- Split projects/big-project.md into architecture.md and implementation.md
- Archived 2 orphan files (old meeting scratch notes)
- Created index.md for tools/ directory
- Moved kanji-reference.md from root to learning/
```

## Output
The skill modifies memory files directly (normal file I/O — the watcher handles indexing). It produces a consolidation log as its output.

## Key Principle
The agent IS the judge. It reads the files, decides what connections make sense, and makes the changes. memfs provides the data (orphans, search patterns, edge strengths); the agent provides the judgment.
