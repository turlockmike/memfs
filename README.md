# memfs

Unix-native memory filesystem for LLM agents.

Files are truth. SQLite is the index. Search is the only verb.

## Architecture

```
Any LLM Agent / Human / IDE
        |  normal file I/O
        v
   Filesystem (MEM_HOME)
   projects/  people/  concepts/
        |  FSEvents / inotify
        v
   memfs daemon (background)
   - watches file changes
   - updates .mem/memory.db (FTS5 + edges)
   - parses [[links]] into graph edges
   - runs decay on schedule
```

- **Files are source of truth** — `.mem/memory.db` is a derived cache, rebuildable via `memfs reindex`
- **One agent command** — `memfs grep <query>` is all an agent needs
- **Harness-agnostic** — works with any LLM agent that can read/write files
- **Neuroscience-informed** — power-law decay (not exponential), fan-effect-limited edges, spacing-effect increments

## Quick Start

```bash
pip install memfs

# Initialize a memory directory
memfs init ~/memory

# Search (creates search edges to top-3 results)
export MEM_HOME=~/memory
memfs grep "kanji curriculum"

# Start the filesystem watcher (keeps index current as files change)
memfs watch --daemon

# Check index health
memfs status
```

## Agent System Prompt (3 sentences)

> Your memory lives in `$MEM_HOME`. Read and write files normally with any tool. Use `memfs grep <query>` to search — connections between files strengthen when you search for them and weaken over time.

## How It Works

### Two Edge Types

| Type | Source | Target | Created by | Decay |
|------|--------|--------|------------|-------|
| `link` | file A | file B | `[[B]]` in A's content | Power-law, floor 0.5 |
| `search` | query node | file | `memfs grep` top-3 results | Power-law, full decay |

Per [Anderson's fan effect](https://en.wikipedia.org/wiki/Fan_effect): edges connect **query to result only**, never result to result. This prevents graph noise from O(n^2) spurious associations.

### Search Edges

Each `memfs grep` call:
1. Normalizes the query (lowercase, strip punctuation, Porter stem, sort tokens, SHA-256)
2. Creates/strengthens edges from query node to rank 1, 2, 3 results only
3. Rank-weighted: rank 1 = full increment, rank 2 = 0.66x, rank 3 = 0.33x
4. Repeated queries strengthen existing edges with spacing-effect weighting

### Power-Law Decay

```
strength = initial_strength * (1 + 0.1 * days_since_last_activation) ^ -0.5
```

| Days inactive | Retained strength |
|---------------|-------------------|
| 1 | 95% |
| 7 | 77% |
| 30 | 50% |
| 90 | 32% |
| 365 | 16% |

### File Format

Plain markdown with optional YAML frontmatter:

```markdown
---
title: Kanji Learning
date: 2026-04-12
---
# Kanji Learning

Studying kanji with spaced repetition. See [[srs-methods]] and [[people/ken]].
```

`[[wikilinks]]` become graph edges. `[[Target|Alias]]` supported.

## Commands

### Agent-facing

| Command | What it does |
|---------|--------------|
| `memfs grep <query>` | FTS5 search, returns ranked NDJSON, creates search edges |

### Operator-facing

| Command | What it does |
|---------|--------------|
| `memfs init [dir]` | Create index, scan all .md files |
| `memfs watch [--daemon]` | Filesystem watcher (keeps index current) |
| `memfs reindex` | Rebuild index from scratch |
| `memfs status` | Node/edge counts, index health |
| `memfs ls [dir] [-v]` | List indexed files |

## Output Format

All output is NDJSON (one JSON object per line):

```jsonc
// memfs grep
{"path": "projects/satori.md", "title": "Satori", "rank": 1, "score": 0.82, "edge_strength": 1.2, "snippet": "...kanji curriculum..."}

// memfs ls
{"path": "projects/satori.md"}

// memfs status
{"nodes": 142, "edges": {"link": 87, "search": 310}, "queries": 45}
```

## Documentation

- [Design Document](docs/design/mem-cli-design.md) — full architecture, schema, decay model, implementation plan
- [Research: Memory Architectures](docs/research/llm-memory-architectures.md) — MemGPT, GraphRAG, Mem0, Zep, Cognee, LangGraph comparison
- [Research: Neuroscience Principles](docs/research/neuroscience-memory-principles.md) — power-law decay, spacing effect, engram networks, fan effect
- [Research: Filesystem Search](docs/research/filesystem-memory-search.md) — FTS5, embedding models, graph structure in filesystems
- [Research: Mastra Memory](docs/research/mastra-memory-system.md) — Observable Memory architecture analysis
- [Research: Eval Benchmarks](docs/research/memory-eval-benchmarks.md) — LongMemEval, LoCoMo, MemoryAgentBench, and 12 others

## License

MIT
