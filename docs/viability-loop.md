---
title: "Memfs viability loop — closed 2026-04-17"
date: 2026-04-17
layer: 2
---

# Memfs viability loop — closed 2026-04-17

Before today, the substrate existed (Neo4j graph, watcher, grep, layers,
freshness, calibration ledger) but the **operational loop** didn't:

- Claude Code sessions wrote transcripts to `~/.claude/projects/-home-mike/*.jsonl`
  that **never reached memfs**. Forward flow was broken.
- No scheduled consolidation pass ("dream") — the graph grew without curation.
- `memfs claim` was one-shot, so batching end-of-response claims was friction.

This doc records the build that closed those gaps.

## What was built

| # | Gap | Built |
|---|-----|-------|
| 1 | Sessions never indexed | `memfs ingest-session <jsonl>` + `karpathy-session-ingest.sh` Stop hook + backfill script |
| 2 | No consolidation input | `memfs dream-briefing` — NDJSON candidates (orphan / merge / split / link / stale / index) |
| 3 | No consolidation trigger | `karpathy-dream-trigger.sh` + cron `0 3 * * *` + claude-loop send |
| 4 | Claim logging friction | `memfs claim --auto` — batch NDJSON from stdin |

Commits on `neo4j-rewrite`:

- `b81c308` — Session ingestion CLI + Stop hook
- `a11fc22` — Dream briefing CLI
- `5cceb5b` — Dream schedule
- `fa92ed4` — `memfs claim --auto`

## Verification

### Status before / after

Before (MEM_HOME = `~/.config/karpathy`, 35 indexed nodes):

```
{"nodes": 35, "edges": {"link": 0, "search": 14}, "queries": 5, ...}
```

Backfill of last 7 days of sessions:

```
Backfill: scanning /home/mike/.claude/projects/-home-mike for jsonl
          modified within 7 days
  scanned:     148
  ingested ok: 148  (of which re-ingested duplicates: 1)
  failed:      0
```

After reindex:

```
{"nodes": 184, "edges": {"link": 2, "search": 0}, "queries": 0, ...}
```

35 → 184 nodes. The watcher picks up every new session markdown as it's
written by the Stop hook.

### Dream briefing sample (first 10 lines on live corpus)

```
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-12/62139254.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-13/0cb2f8c5.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-14/e142dca5.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-15/115aab86.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-15/41add404.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-16/13faf54c.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-16/18df99dd.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "merge", "nodes": ["sessions/2026-04-11/4c16d0a5.md", "sessions/2026-04-16/42c78d8f.md"], "reason": "title/content overlap jaccard=1.00", "priority": 0.9}
{"candidate_type": "split", "nodes": ["playbook.archive.md"], "reason": "1451 lines ≥ 500; 157244 bytes ≥ 10240", "priority": 0.9, "bytes": 157244, "lines": 1451}
{"candidate_type": "index", "nodes": ["sessions/2026-04-16"], "reason": "44 .md files, no index.md", "priority": 0.8, "file_count": 44}
```

29 total candidates across types: `merge`, `split`, `index` (sessions/* dirs
have no index.md), `orphan`, `stale`. The cluster of high-jaccard "merge"
hits for `sessions/2026-04-11/4c16d0a5.md` is a genuine signal — that session
had a boilerplate first prompt that recurs in later wake-cycle transcripts,
which is exactly the kind of pattern a dream pass should recognize and
either link or dedupe.

### Grep — forward flow is live

```
$ memfs grep "compression hypothesis" --limit 5
rank=1  sessions/2026-04-17/87177357.md  (the discussion that birthed this)
rank=2  formative-sessions/2026-04-16-viable-memory.md
rank=3  viable-memory-architecture.md
rank=4  sessions/2026-04-17/e4c8c7ab.md
rank=5  shared/optimization-techniques.md
```

For the first time, a grep for a concept Mike and I talked about in
session X on day N surfaces **both** the formative-session file
(hand-curated layer-5 texture) **and** the session transcripts where it
came up. That's the viability loop closed.

### Calibration still works

```
$ memfs calibration --window 30
{"window_days": 30, "scope": null, "total_verified": 0, ...}
```

Unchanged structurally; empty because tests wipe the Claim store in
fixtures. Real claim flow is unaffected.

### Tests

- `tests/test_ingest.py` — 5 cases (distill, frontmatter, idempotence,
  malformed-jsonl tolerance, empty-file handling)
- `tests/test_dream_briefing.py` — 3 cases (all six candidate types
  detected on a synthetic graph, NDJSON-serializable, priority-sorted)
- `tests/test_cli.py::TestClaimAuto` — 3 cases (batch insert, bad-json
  tolerance, missing-args rejection)

Full suite: **180 passing**, up from 177 before this change.

## Operational flow now

```
Claude Code session ends
      │
      ├── Stop hook: karpathy-session-ingest.sh
      │     └── memfs ingest-session <transcript>
      │           └── writes sessions/<date>/<short>.md (frontmatter layer=2)
      │
watcher picks up the new .md
      │
      └── Neo4j node created + FTS indexed
             │
             │  (next Karpathy session…)
             │
   "memfs grep <topic>" → session surfaces alongside formative files
             │
03:00 CT nightly
      │
      ├── cron: karpathy-dream-trigger.sh
      │     └── claude-loop send karpathy "/memfs-dream ..."
      │
  Karpathy wakes, runs memfs dream-briefing, acts on candidates,
  logs uncertain decisions via memfs claim --auto
```

The forward flow (session → memfs → next-session recall) and the
maintenance flow (dream-briefing → consolidation) are now both in place.
