# mvm — architecture

A minimum viable memory system for LLM agents. The smallest design that closes the loop honestly: encode → retrieve → verify, with no sinks at any level of recursion.

## Thesis

A memory system is **viable** iff it can do three things in a closed loop:

1. **Encode** — take information from the world and store it
2. **Retrieve** — get it back on demand
3. **Verify** — prove the retrieval matches what was encoded

Below this floor, the system is either a write-only log (no retrieval check) or hopeful retrieval (no verification). The minimum is the closure of those three.

## Invariants

These hold across the entire system. Every component honors them.

1. **No sinks.** Every persistent output has at least one structural consumer. Logs that nothing reads are leaks (FEP entropy spills). Resolved by either dissolving (noise) or driving substrate change (signal).
2. **Main session is grader.** When a probe runs, the main session reads its rationale and decides. Probes are measurement instruments; the orchestrator is the integrator. One generative model performs both action and inference.
3. **Tests are immutable post-authoring.** Goodhart prevention is structural, not policy. If a test fails, fix the doc; never the test.
4. **Trust hierarchy: KB > web > weights.** Substrate is canonical by default. Override only on explicit signal (web has fresher source, KB silent, etc.).
5. **Append-only KB.** Status tags (`current` / `superseded` / `archived`) instead of deletion. Historical record always reconstructable.
6. **Three terminal states for any prediction error.** Dissolve (cross-check disconfirms), substrate update (ingest / supersede / annotate), or user escalation. No anomaly accumulates indefinitely.

## Component map

```
~/.claude/skills/                  ← agent-invoked protocols
├── mvm-ingest/SKILL.md            (~70 lines, write side)
├── mvm-recall/SKILL.md            (~50 lines, read side)
└── mvm-dream/SKILL.md             (~70 lines, offline integration + meta-audit)

~/mvm/bin/                         ← CLI primitives (PATH-resident via symlink)
├── mvm verify                        cold-clone subprocess (out-of-session fallback)
├── mvm index                          FTS5 + graph adjacency rebuild
├── mvm search                        tri-mode retrieval (text + graph + hierarchy)
└── mvm stats                          decoherence dashboard

~/mvm/state/                       ← runtime state
├── recall-log.jsonl                one entry per recall
├── dream-log.jsonl                 one entry per dream cycle
├── index.db                        SQLite + FTS5
└── graph.db                        adjacency table

~/mvm/knowledge/                   ← user-curated KB
├── <topic>.md                      canonical content + frontmatter
└── <topic>.tests.yaml              locked Q/A test cases
```

## Data flow

```
USER QUESTION
     │
     ▼
/mvm-recall ──► spawns 3 parallel haiku probes
     │              │
     │              ├─► naked clone        (weights only)
     │              ├─► KB clone           (mvm search + Read, no web)
     │              └─► web clone          (WebSearch + WebFetch, no KB)
     │              │
     │              ▼
     │         each returns: ANSWER + RATIONALE
     │              │
     ▼              ▼
main session reconciles via trust hierarchy
     │
     ├─► KB hit → return answer + citation
     ├─► KB silent + web hit → return + spawn /mvm-ingest (background)
     ├─► all silent → hard miss, refuse, log gap
     └─► three-way disagreement → surface all three, log contested
     │
     ▼
append entry to recall-log.jsonl
     │
     ▼  (later)
/mvm-dream reads recall-log + dream-log
     │
     ├─► Phase 0 — meta-audit (read dream-log)
     │     └─► chronic failures, stuck topics, noise patterns → escalate to user
     │
     ├─► Phase 1-5 — coverage / quality / staleness / contested / gaps
     │     └─► each anomaly cross-checked, then either dissolved or acted on
     │
     ▼
append entry to dream-log.jsonl  ← consumed by next dream's Phase 0
```

## The three theoretical frames (all describe the same loop)

**FEP (Friston).** /ingest is action (substrate update, world moves to match prediction). /recall is perception (belief update from substrate). /dream is the offline complexity-regularization step. Cold-clone score is direct measurement of prediction error. Hooks are precision-weighting (which prior to trust). Algedonic alarm fires when the Markov blanket has been compromised.

**RL (Sutton).** LLM is a frozen policy. Filesystem is the parameter store. Cold-clone scores are the reward signal. File writes are the gradient updates. /recall = forward inference. /ingest = environment interaction + write to replay buffer. /dream = experience replay + value consolidation. The KB is the externalized world model.

**Biology (Buzsáki).** Wake mode = recall + ingest (fast hippocampal indexing + slow neocortical integration). Sleep mode = dream (sharp-wave-ripple replay; consolidates new traces with old). The user-question stream is the perceptual input; the substrate is the consolidated memory; dream's meta-audit is the proprioceptive loop ("am I sleeping enough?").

The frames converge because the loop is the same: closed-loop self-evidencing systems share the same structural invariants regardless of substrate.

## Closure rule (no sinks)

Every prediction error reaches one of three terminal states within finite cycles:

| Terminal state | Cause | Consumer |
|---|---|---|
| **Dissolve** | Cross-check disconfirms — was noise | Recorded as transient in dream-log; future cycles reference for tuning |
| **Substrate update** | Cross-check confirms — was signal | Ingest / supersede / annotate the KB |
| **User escalation** | Recurs across cycles — system can't resolve | User is the final reality check; meta-audit surfaces it |

**The user is the meta-meta consumer.** The recursion terminates with them.

## Decoherence framework

Four distinct decoherence types, each with its own signal and fix:

| Type | Symptom | Detection | Fix |
|---|---|---|---|
| **Coverage** | KB doesn't cover what users ask | Web-fallback rate per topic (mvm stats) | Proactive `/mvm-ingest` on top fallback URLs |
| **Quality** | Canonical can't be retrieved from anymore | Cold-clone fail on existing tests | Cross-check; if confirmed, re-ingest from original `source:` |
| **Staleness** | KB has aging info that web has superseded | KB-clone vs web-clone disagreement with fresher-source markers | Cross-check; if confirmed, ingest fresh URL, mark old `superseded` |
| **Bloat** (v0.1) | Search precision degrades; near-duplicate canonicals proliferate | Mean search latency, candidate-set noise | Consolidation pass during /dream |

## Trust hierarchy and overrides

Default: **KB > web > weights**.

- KB grounded (rationale cites doc) → return KB answer
- KB silent + web grounded (rationale cites URL) → return web, spawn ingest
- KB and web silent + weights grounded → return naked, flag "from prior, not in KB", no ingest (general knowledge — KB shouldn't have it)
- All silent → hard miss, refuse, log gap

**Curated-domain override:** declared domains (e.g. PoE2 mechanics, finance, kalshi) always prefer KB even at lower confidence. The substrate is canonical for those topics by user contract.

**Superseding override:** if web has a clearly fresher source (date markers, "as of", "updated") and contradicts KB, web wins; old canonical is marked `superseded` and a fresh canonical is ingested.

## Cost profile

Per recall: 3 parallel haiku probes ≈ ~$0.001. ~300× cheaper than one opus call. Treat recall cost as effectively free.

Per ingestion: ~30s, 5-10 cold-clone calls (naked + injected for each test). ~$0.01.

Per dream cycle: depends on activity; bounded at 3 ingestions per cycle. Typical: ~2-5 minutes including meta-audit and cross-checks.

## v0.1+ deferred

- Real vector embeddings (Voyage AI) replacing FTS5 in mvm search
- Two test cohorts (source-derived + blind cohort)
- Free-recall grader mode (Tulving's recognition-vs-recall fix; requires three-stage verification)
- Recency penalty + decay timestamps in retrieval ranking
- Bloat-decoherence consolidation pass
- Pressure-gated automatic /mvm-dream scheduling
- Algedonic alarms (calibration inversion → freeze ingest)
- Reconstruction-mode verify (canonical → source claims, entailment-graded)
- Strict-isolation cold-clone via `--bare` mode (requires `ANTHROPIC_API_KEY`)
- Hooks for auto-fire of /mvm-recall on curated-domain UserPromptSubmit
- Per-canonical retrieval-count tracking (better priority signal than mtime)

## Decision log (what was tried and rejected)

| Considered | Rejected | Why |
|---|---|---|
| Two-stage subprocess grader (claude --print retriever + grader) | Yes | Wrong architecture — main session is the grader. FEP says one generative model performs both action and inference |
| Confidence score field on probe output | Yes | LLM self-reported confidence is poorly calibrated. Rationale field carries the real signal (provenance) |
| `DONT-KNOW` as expected answer for negative tests | Yes | Throws away useful signal. Better: every test has a real expected; hallucination = high-rationale-confidence + wrong answer |
| Random sampling in /dream's quality and staleness checks | Yes | Oldest-mtime-first is a better priority. Random was the dumb default |
| Custom subagent definitions (mvm-retriever.md, mvm-grader.md) | Yes | Mike: skills only, no agent definitions. Prompt-level tool restriction works empirically (15/15 compliance on haiku) |
| Separate sink files (quality-failures.jsonl, staleness-flags.jsonl, contested.jsonl) | Yes | Sinks = leaks. Anomalies resolve in-cycle via cross-check + action; transient ones are recorded in dream-log |
| Sequential Phase 0 / Phase 1 / Phase 2 in /mvm-recall | Yes | Parallel-3 probes is ~300× cheaper than opus and gives differential signal on every recall |
| Heavy + light ingest modes | Yes | Light mode skipped source extraction; "thorough always" is the right default. Recall-derived Q+A is a seed, not a shortcut |
