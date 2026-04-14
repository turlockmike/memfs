# Research Index

Research docs in this directory split into two categories: **memory-system research** (the domain memfs itself addresses) and **LLM eval / autoresearch methodology** (generalized learnings from running actual optimization loops on projects like the chess explanation engine and the PoE2 expert).

## Memory-system research (informs memfs design)

| File | Topic | Lines |
|---|---|---|
| `filesystem-memory-search.md` | FTS5, filesystem-as-index, search-as-retrieval | 428 |
| `llm-memory-architectures.md` | Survey of memory architectures across frameworks | 390 |
| `mastra-memory-system.md` | Detailed study of Mastra's memory system | 443 |
| `memory-eval-benchmarks.md` | How to evaluate memory systems empirically | 592 |
| `neuroscience-memory-principles.md` | Power-law decay, fan effect, spacing effect | 313 |

## LLM eval & autoresearch methodology (generalizes from chess + PoE2 runs)

| File | Topic | Lines |
|---|---|---|
| `autoresearch-loop-skill.md` | The Karpathy-style research loop — fixed evaluator, one mutable file, one metric, commit after every keep. The operational protocol. | 339 |
| `llm-eval-methodology.md` | Principles learned the hard way — the load-bearing insight ("check the input context before changing the vocabulary"), contamination detection, judge calibration, ratchet discipline, anti-patterns. Read this before starting any LLM optimization work. | 143 |
| `optimization-techniques.md` | Named techniques T1–T10 for making Claude Code sessions and skills more effective. T1 Plumbing Extraction has the highest ROI; compression is last, not first. | 246 |
| `chess-autoresearch-case-study.md` | The chess explanation engine project — v4 puzzle suite (45 hand-written puzzles, Steps Method), tactical_analyzer upstream-bug finding, honest 77.3% baseline, why the pre-Apr-11 scores were inflated. The canonical worked example for the methodology. | 399 |
| `poe2-eval-case-study.md` | PoE2 crafting expert system — eval framework for domain expertise (85-question quiz, 93% projected score), CLI toolkit, encyclopedia. Second worked example. | 59 |
| `systems-thinking-bench-notes.md` | Benchmark measuring whether LLMs can question the frame of a problem, not just reason within it. Scoring, prompt tiers, isolation flags. | 76 |

## Reading order

If you're new to this body of work:
1. `llm-eval-methodology.md` — what the work is and why
2. `autoresearch-loop-skill.md` — the operational protocol
3. `chess-autoresearch-case-study.md` — a worked example
4. `optimization-techniques.md` — tactical patterns
5. Then the memory-system research docs as needed

## Source-of-truth pointers

Some of these files are copies from other locations:

| File here | Live canonical source |
|---|---|
| `autoresearch-loop-skill.md` | `~/.claude/skills/autoresearch/SKILL.md` |
| `llm-eval-methodology.md` | `~/.claude/projects/-home-mike/memory/topics/llm-eval-methodology.md` |
| `optimization-techniques.md` | `~/.claude/projects/-home-mike/memory/topics/optimization-techniques.md` |
| `chess-autoresearch-case-study.md` | `~/.claude/projects/-home-mike/memory/topics/chess-eval-rebuild.md` |
| `poe2-eval-case-study.md` | `~/.claude/projects/-home-mike/memory/topics/poe2.md` |
| `systems-thinking-bench-notes.md` | `~/.claude/projects/-home-mike/memory/topics/systems-thinking-bench.md` |

If you're editing these and expect the edits to survive future session rotations, edit the canonical source; the memfs copies will be refreshed from those. If you're editing in memfs to publish (e.g. for readers outside Mike's machine), then the canonical-source copies are downstream and will need to be re-synced. Don't let the two drift silently — pick a direction each time.
