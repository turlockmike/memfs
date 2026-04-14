# Optimization Techniques

Named, codified techniques for making Claude Code sessions and skills more effective, efficient, and adaptive. Loaded by skills-optimizer, extend-skill-builder, and session-review as a shared reference.

Each technique has a defensive rule because the model lacks training data on optimal tool use — without explicit instruction, it defaults to patterns that waste tokens.

**Mixed-memory note (Karpathy, 2026-04-11):** Mike taught me this article after I shipped origination discipline. Reading it made me notice that my `mike-state.md` step-0 design was doing LLM reasoning for work that belongs in a plumbing script. Applied T1 immediately — see "Retroactive application" at the bottom.

---

## T1: Plumbing Extraction

**Rule:** Before touching any prompt, audit for deterministic work the LLM is doing that a script could do. Extract it to a script.

**Why:** The model defaults to reasoning about everything in output tokens — including formatting, counting, parsing, and transforming data. These operations are deterministic and can run at zero token cost as scripts. This is consistently the highest-ROI optimization, outperforming all prompt compression combined.

**The test:** Could a domain expert define the complete mapping from inputs to outputs in advance? Two questions:

1. Is the correct output fully determined by the input? (no external judgment needed)
2. Could you enumerate the rules exhaustively? (not "would it be tedious" but "is it possible in principle")

Both yes → plumbing. Either no → intelligence.

This handles edge cases well: regex-for-meaning fails not because it's nondeterministic, but because you can't enumerate the mapping — natural language meaning isn't a closed set. Format templates pass because the mapping is fully specifiable even if complex. The secondary check "could you write unit tests?" needs sharpening: the real version is "could you write tests that cover ALL valid inputs" — for intelligence work, the answer is always no.

**Before:**
```
SKILL.md: "Count the items in each category, compute the percentage, and format as a markdown table with aligned columns..."
→ Model spends 200+ output tokens doing arithmetic and formatting
```

**After:**
```
Script: python3 scripts/format-summary --format markdown < data.json
SKILL.md: "Run format-summary and present the output"
→ Zero reasoning tokens for deterministic work
```

**Expected impact:** 10-20% cost reduction per skill.

---

## T2: Subagent Delegation for Data Extraction

**Rule:** MUST delegate file reading and data collection to subagents. MUST NOT read files into parent context before delegating analysis.

**Why:** The model's default behavior is to read everything into its own context first, then reason about it. This bloats the parent context with raw data that could stay isolated. Subagents are completely isolated context windows — only the summary returns.

**Before:**
```
Parent reads 12 files (8K tokens into context) → analyzes → produces 200-token summary
Parent context: bloated with 8K of raw file content for the rest of the session
```

**After:**
```
Parent delegates: Agent("Read these 12 files and summarize X") → receives 200-token summary
Parent context: lean, only the summary
```

---

## T3: Minimal Subagent Returns

**Rule:** Subagent prompts MUST specify exactly which fields to return. Never accept full payloads when only specific fields are needed.

**Why:** Without explicit instruction, subagents return everything they found — full JSON payloads, complete file contents, verbose explanations. Only the specific fields needed for the parent's decision should cross the boundary.

**Before:**
```
Agent("Search for PRs by this author")
→ Returns full PR objects: body, diff stats, labels, reviewers, timeline (2K tokens)
```

**After:**
```
Agent("Search for PRs by this author. Return ONLY: number, title, state, mergedAt as NDJSON")
→ Returns 4 fields per PR (200 tokens)
```

---

## T4: Web Search Delegation

**Rule:** Web searches MUST run in a subagent, never in the parent context.

**Why:** Web search results are verbose, unpredictable in size, and often irrelevant. Running them in the parent bloats context with content that can't be removed. A subagent absorbs the noise and returns only the answer.

**Before:**
```
Parent runs WebSearch("Claude Code hooks") → 3K tokens of search results in context
Parent runs WebSearch("Claude Code settings") → another 3K tokens
Context: 6K+ tokens of raw web content permanently in session
```

**After:**
```
Agent("Research Claude Code hooks and settings. Return: how to configure hooks, valid settings keys")
→ 300-token summary, parent context stays clean
```

---

## T5: Outcomes + Why > Procedures

**Rule:** Skills MUST define outcomes (what success looks like) and why (reasoning behind constraints). MUST NOT teach the model step-by-step procedures for tools it already knows.

**Why:** The model already knows how to use `gh`, `curl`, `jq`, `aws`, and most well-known CLIs. Restating procedures wastes tokens and competes with the model's trained knowledge. But the model does NOT know your specific goals, constraints, or the reasoning behind organizational decisions — that's what skills must provide.

**Before:**
```
SKILL.md: "Step 1: Run gh pr list --json number,title. Step 2: Filter by state=open. Step 3: For each PR, run gh pr view..."
→ 15 lines teaching the model things it already knows
```

**After:**
```
SKILL.md: "Find open PRs for this repo. We prioritize by age because our SLA requires review within 48h."
→ 2 lines: outcome + why. Model figures out the gh commands.
```

**Note:** "Why" explanations are load-bearing. Overview sections and domain framing enable the model to exercise judgment on edge cases. A model that knows "we do X because of compliance requirement Y" can generalize. A model that only knows "do X" can't. When compressing, protect the "why."

---

## T6: Model Selection

**Rule:** Model selection is a forked search, not a single swap. Fork the optimization from the current best experiment, run an independent loop per candidate model, compare converged results.

**Why:** A single search trajectory can't explore model+prompt combinations without either bundling (loses isolation) or sequencing (hits local maxima where an Opus-optimized prompt fails on Sonnet not because Sonnet can't do the task, but because the prompt was tuned for a different model). Forking the search avoids both problems — each model gets its own optimized prompt, and each fork is fully isolated.

**Procedure:**
1. **Complete prompt optimization on the current model first.** Plumbing extraction, context management, compression — get the prompt stable and converged.
2. **Fork per candidate model.** Create a branch from the current best experiment. Run a fresh optimization loop on Sonnet (and optionally Haiku) with the converged prompt as the starting point.
3. **Bail early.** If a model's baseline pass rate is catastrophic (<70%), don't invest in a full loop — the capability gap is too large to bridge with prompt changes.
4. **Compare converged results across forks.** The winner is the best cost/effectiveness tradeoff across all model+prompt combinations, not just the cheapest model that barely passes.
5. **Watch for failure mode shifts.** A model might maintain 95% pass rate but fail on different cases. Compare WHICH evals fail, not just how many. If Sonnet passes 19/20 but fails on the guardrail test that Opus handles, that's not a clean swap.

**Haiku warning:** Haiku's failure modes are qualitatively different — more likely to hallucinate tool names, skip multi-step reasoning, and miss conditional logic. It's not "cheaper Sonnet." The forked search will surface these differences; don't skip the full loop.

**Before:**
```
Single search: swap to Sonnet, prompt regresses, conclude "Sonnet can't do this"
→ Local maximum — Sonnet could work with 2 instructions added back
```

**After:**
```
Forked search: Opus branch converged at $0.15/run, Sonnet branch converged at $0.07/run
→ Sonnet needed 2 extra instructions but still 53% cheaper
```

---

## T7: Context Tier Management

**Rule:** Push knowledge to the cheapest tier that works. Always-loaded content (CLAUDE.md, MEMORY.md) is the most expensive. On-demand content (skills, path-scoped rules) costs zero when idle. Isolated content (subagents) never enters the main context.

**Why:** The model processes all always-loaded content on every turn. A 200-line CLAUDE.md costs tokens every message. A skill with the same content costs zero until invoked.

| Tier | Cost | Examples |
|------|------|----------|
| Always | Highest | CLAUDE.md, MEMORY.md (first 200 lines), skill descriptions |
| On demand | Zero when idle | SKILL.md body, `.claude/rules/*.md`, memory topic files |
| Isolated | Never enters parent | Subagent context, worktree file state |

---

## T8: Format Template Preservation

**Rule:** MUST NOT compress or shorten format templates (display examples with specific icons, structure, or layout).

**Why:** Confirmed load-bearing across multiple skills (slack-use, todo, email). The model needs the full example to reproduce the format exactly. Shortening it causes regressions or forces the model to spend MORE tokens reasoning about what the format should look like. This is counter-intuitive — it looks like removable verbosity but is actually the cheapest way to specify format.

**Before:**
```
"Format output as a table with status icons, priority, and age"
→ Model guesses format, sometimes wrong, spends tokens reasoning about layout
```

**After:**
```
"Format output exactly like this example:
1. ⬜ **[268]** Task title — `high/asap` — 3 days ago
   > Notes here..."
→ Model reproduces format directly, zero reasoning about layout
```

---

## T9: Defensive Prompt Compression

**Rule:** When compressing prompts for efficiency, delete each instruction one at a time and rerun all tests. Keep it deleted only if tests still pass. Never bulk-delete.

**Why:** Instructions interact in non-obvious ways. Removing instruction A may break behavior that appears to be governed by instruction B. The only safe way to compress is incremental deletion with full regression testing.

**Good deletions:**
- Instructions teaching the model things it already knows (CLI syntax, common patterns)
- Defensive padding ("Make sure to...", "Remember to...", "It is important that...")
- Redundant examples the model doesn't need
- Step-by-step procedures replaceable by outcome statements

**Bad deletions:**
- Domain framing and overview sections (enable judgment)
- "Why" explanations (enable generalization)
- Format templates (load-bearing — see T8)
- Guardrails discovered through test failures

---

## T10: Effort-Level Overrides

**Rule:** Set `effort: low` in SKILL.md frontmatter for mechanical skills to suppress extended thinking tokens.

**Why:** Extended thinking can consume 10k-50k output-priced tokens per request. For classification, formatting, data extraction, and other mechanical tasks, thinking tokens are wasted. Effort-level overrides are per-invocation — the rest of the session stays at normal effort.

---

## The Optimization Order

Work these levers in order of typical ROI. Don't start with prompt compression — it's the lowest-ROI lever.

1. **T1: Plumbing extraction** (10-20% cost reduction)
2. **T10: Effort-level overrides** (40-60% per invocation)
3. **T7: Context tier management** (varies)
4. **T2-T4: Subagent patterns** (30-50% context reduction)
5. **T9: Prompt compression** (stabilizes the prompt for current model)
6. **T6: Model selection** (fork the search — 40-60% for mechanical tasks, requires stable prompt as starting point)

---

## Retroactive application — Karpathy 2026-04-11

Audit of what I shipped earlier this session against the techniques:

**`mike-state.md` step-0 design.** The file currently has prose like "populate from `~/.config/grove/weight-log.tsv` on first beat." That's LLM reasoning for pure plumbing — tail the file, format, substitute into section. Fixed by building `mike-state-update` script (T1). The script pulls from ground-truth sources (weight-log.tsv, gym-log.tsv, kalshi balance, calendar) and writes updated sections in-place between markers. STEP 0 of my heartbeat now runs the script first, then reads the file — zero LLM tokens for deterministic updates.

**`origination-audit.py`.** T1-compliant by construction — classification of log entries by keyword patterns is fully enumerable. The edge cases I'm willing to misclassify (chess work without "mike" keyword) are acceptable because the classifier is biased toward being honest: anything that doesn't clearly signal reactive work is ORIGINATED, which makes the metric harder to game upward. That's the right direction for self-audit.

**`karpathy.md` heartbeat step descriptions.** T5 violation — I wrote procedural descriptions ("Load /kalshi. Run kalshi-v2-screen. Any candidates? Evaluate per trading playbook.") when outcomes+why would be shorter and let me use training priors. Compressed incrementally (T9 discipline) to "TRADING — screen, evaluate, decide, log. Never skip." The order of steps matters so I keep the numbering; the prose inside each step collapses.

**Karpathy identity section.** The first paragraph of karpathy.md is outcome+why (who Andrej is, what that means for how I think) — do not compress. Identity framing is load-bearing context that enables judgment across all domains (T5 note — protect the "why").

**Format template in playbook session log.** The session log examples in playbook.md are format templates. T8 applies — don't shorten the session log entries in existing templates because the shape teaches the model how to write the next one.

**Rule for future beats:** Before adding prose to karpathy.md or any skill, run the T1 test — could this be a script? If yes, build the script; reference it from the prose.
