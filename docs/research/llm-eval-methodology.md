# LLM Eval & Autoresearch — Methodology

**Purpose:** Shared memory of research and evaluation principles learned by running actual autoresearch loops. Lives in the memory graph so any agent working on LLM optimization (Karpathy, Grove, future specialist lens spawns) reads from and contributes to this single source of truth. This is the "mixed memory" principle applied to research knowledge — no private Karpathy notebook.

**Canonical source work:** Chess explanation engine (Apr 9-11, 2026). See `chess-eval-rebuild` topic for the specific project state, puzzle suite, and v25 champion. This topic is the *generalized* methodology extracted from that work and from future research runs.

---

## The Load-Bearing Insight

**When an LLM gives wrong output, check whether the input context is misleading it before changing the vocabulary or instructions.**

Source: chess autoresearch Apr 10-11. The model was consistently mis-explaining Step 4-5 tactical puzzles (luring, counting-with-order). Twenty iterations of vocabulary additions, emphasis tweaks, and mandate sections produced marginal or negative changes. The actual bug: `tactical_analyzer.py` — a programmatic pre-processor in the chess pipeline — was outputting *wrong* best-moves for complex positions. For puzzle-22 it said "Qe1+ is a fork" when the solution was "Rb8 luring." The model was dutifully reading that misleading context and framing explanations around the wrong mechanism. One disclaimer sentence telling the model to trust the solution line over the tactical analyzer fixed multiple failures simultaneously and took the champion from 85.7% → 90.97%.

**Generalization:** When behavior is wrong, look upstream at what shaped the model's attention *before* you look at what rules are supposed to correct it downstream. Rules in prose compete badly with training priors; context reshaping works because the LLM is a pattern-matcher over whatever is in attention. A one-sentence context correction often beats a fifty-line vocabulary rewrite.

**Diagnostic question to ask first:** *What is in the model's input that would lead it to this wrong output?* Not: *What rules should I add to prevent this wrong output?* The first question finds root causes. The second creates rule bloat that makes the next failure harder to see.

---

## The Autoresearch Pattern (Karpathy-style, formalized)

Three things fixed. One thing changes. One metric decides.

| Component | Role | Mutability |
|---|---|---|
| **Evaluator** | Scores any candidate on the same data, same rules | FIXED. Never changes during a run. |
| **Data** | Ground truth the evaluator uses | FIXED for a research run. |
| **Metric** | Single number that decides keep/discard | FIXED. One metric. Not two. |
| **Strategy** | The thing being optimized (prompt, config, params) | MUTABLE. Only this changes. |

**The loop:**
1. Hypothesize — pick a principled change to the strategy, based on observed failure modes
2. Implement — modify the mutable file/function
3. Evaluate — run the fixed evaluator, get the metric
4. Decide — metric improved meaningfully? KEEP. Regressed or noise-level? DISCARD.
5. Log — record hypothesis, metric, keep/discard to TSV
6. GOTO 1 — never pause, never ask, never stop until the target is hit or a clean plateau is reached

**Non-negotiable preconditions before starting a loop:**
- The evaluator must be a **trustworthy Tier 1 oracle.** Chess project learned this the hard way — the original `run-eval-puzzles.py` was a keyword-bingo scorer that reported 100% on generic-fluff explanations. An untrustworthy eval makes autoresearch impossible; you rate-limit on noise and call it progress. If the eval isn't trustworthy, the first task is rebuilding the eval, not iterating the strategy.
- The metric must be **single and aligned.** If you track multiple metrics, each hypothesis needs a judgment call about which wins, and the loop stalls on political decisions. Pick one.
- The data must be **fixed and verified.** Duplicates in the eval suite (the v2 chess suite had 5 duplicate FENs) create silent double-counting and bias. Dedupe and verify before starting.

---

## Principles Learned the Hard Way

### 1. Check for eval contamination when scores jump >5pp

The chess loop hit a score of 89.3% on v21 and Karpathy refused to bank it. Investigation showed that changes to the eval suite itself had inflated scores on puzzle-22 and puzzle-41 (not genuine improvements). The clean baseline after contamination correction was ~84.9%, meaning the "jump" was almost entirely contamination. **A jump of more than a few percentage points in a single iteration is a contamination alarm — audit the eval suite, not just the score.**

### 2. Track per-item deltas, not just the average

The same iteration (v23, simplicity heuristic) produced a +0.12pp average change but had catastrophic regressions on previously-perfect puzzles (puzzle-33 went from 1.0 to 0.429). The average hid the damage. **Always compute per-item deltas between iterations and read the top regressions.** A small average gain with catastrophic item regressions is usually a bad trade — you're learning in one direction while unlearning in another.

### 3. Principled hypotheses, not blind mutation

Every variant should have a written hypothesis grounded in observed failure modes. Read the actual failing outputs. Identify a specific pattern. Articulate *why* the proposed change should fix it. If you can't articulate why, don't make the change. The chess loop spent iterations 2-10 trying "add more emphasis," "add PRE-CHECK MANDATES," "add CRITICAL OUTPUT CHECKLIST" — all non-hypotheses, all plateau. The real progress came when Karpathy started reading failure outputs and forming targeted hypotheses.

### 4. Structural changes are safer than vocabulary changes

Vocabulary additions are **fragile** — every new term definition has the potential to regress other items. Chess: v24 tried to disambiguate vocabulary and dropped puzzle-33 from 1.0 to 0.286. Structural changes (conditional instructions, format templates, context disclaimers, example additions) tend to be more robust because they don't redefine semantic anchors the model already has. When possible, prefer: (a) structural > vocabulary, (b) example-based > rule-based, (c) conditional > blanket.

### 5. Calibrate the judge before trusting the metric

The chess loop hit a 49.1% ceiling that turned out to be judge strictness, not explanation quality. A too-strict judge creates false ceilings. A too-lenient judge masks real quality differences. **Validate the judge with known-good and known-bad examples before running the loop.** Gold-standard answers should score near 100%. Obvious garbage should score near 0%. If those baselines are off, fix the judge.

### 6. Model variance is a real metric, not a nuisance

Non-zero temperature causes score fluctuation. The chess loop saw puzzle-1 score 67% in one eval run and 83% in a re-run on the same inputs. **Either use temperature=0 or average multiple runs for stable measurements.** Treat single-run deltas below ~3pp as noise unless you've demonstrated the run-to-run variance is small.

### 7. Sweet spots exist — more instructions can hurt

Some models have a sweet spot on prompt length. Gemma4 in the chess loop regressed when given too many explicit instructions (v17 went to 0% after an over-constrained rewrite). Larger models (gemini-3-flash) tolerate more structure. **Test at different prompt lengths periodically — the assumption that "more guidance = better output" is not universally true.**

### 8. Switch models if the cap is model capacity

The chess loop's gemma4 ceiling was ~49%. Switching to gemini-3-flash-preview via OpenRouter produced a ~28pp jump on the same prompt. **If you've plateaued after principled hypotheses and the failures look like model capability limits (not prompt problems), test a stronger model.** Cost analysis: gemini-3-flash was ~$0.0007 per explanation vs gemma4's $0 but 30× slower and capped lower. Total cost for the full autoresearch run was under $0.50.

### 9. The ratchet is non-negotiable

Measure, change one thing, measure, keep or revert. Binary. The measurements win, even over your intuition. Karpathy's v21 at 89.3% *felt* like progress but was contamination; he correctly refused to bank it. His v23 at 87.52% average *looked* like progress but had three catastrophic per-item regressions; he correctly refused to bank that too. **The ratchet's discipline is the only thing separating real improvement from drift.**

### 10. Log everything, including discards

Every iteration gets logged with hypothesis, metric, keep/discard, timestamp. The log is the shared history that prevents the next researcher from repeating the same failed hypothesis. Chess loop state lives at `~/chess-games/autoresearch/autoresearch-state.json` — every iteration with reasoning. Future research runs should use the same pattern.

---

## Anti-Patterns (do not repeat)

- **Raw API call loops without an agent.** The pre-Apr-10 `autoresearch.py` was a Python script making raw Claude API calls to generate mutations. No LLM/eval expertise, just blind emphasis-adjustment. It plateaued at 85.7% and was correctly abandoned. **The intelligence in autoresearch lives in the researcher (the mode / lens / agent applying the framework), not in an API call loop.** If you're writing a Python script that calls an LLM in a loop as your core research mechanism, you're doing it wrong — load the `/autoresearch` skill and enter the mode yourself, or spawn a specialist lens.
- **Adding "MANDATORY" and "CRITICAL" to the prompt.** The chess loop tried this multiple times (v10 PRE-CHECK MANDATES, v13 CRITICAL OUTPUT MANDATES, v15 VERIFICATION PROTOCOL, v20 STRENGTHENED CRITICAL WARNING). Every one regressed or flat-lined. All-caps emphasis is a non-hypothesis — it's the LLM-prompt version of "try harder." **If your mutation is "yell louder," stop and find a real hypothesis.**
- **Banking average improvements without checking per-item regressions.** See Principle #2. Your average might be +0.1pp while your worst item dropped 60 points. That is a bad trade wearing the mask of a good trade.
- **Ignoring the judge.** The judge IS part of the measurement stack. If the judge is wrong, the metric is wrong, and the whole loop is climbing a false hill. Validate judges with known-good and known-bad inputs before trusting scores.

---

## Standard Research Run Checklist

Before starting a new autoresearch run on any LLM optimization task:

- [ ] **Eval oracle exists and is trusted.** Known-good inputs score near 100%. Known-bad inputs score near 0%. Judge has been validated.
- [ ] **Eval data is clean.** No duplicates. Verified ground truth. Per-item rubrics.
- [ ] **Single metric is identified.** Not two. Not a weighted blend that will cause political decisions later. One number.
- [ ] **Baseline is measured.** You know what you're starting from. Logged with timestamp.
- [ ] **State file exists.** `autoresearch-state.json` or equivalent, tracking iteration history with hypothesis / metric / keep-discard / timestamp.
- [ ] **Stop criteria are defined.** Target score AND plateau criterion (e.g., "stop at 90% OR after 3 consecutive non-improving iterations").
- [ ] **Non-blocking execution.** Long eval runs go in background (`run_in_background: true` on Bash). No polling loops.
- [ ] **Failure outputs are accessible.** You can read individual per-item failures, not just aggregate scores.

During the run:

- [ ] Every hypothesis is written down before the variant is coded.
- [ ] Per-item deltas are computed after every eval.
- [ ] Score jumps >5pp trigger a contamination audit.
- [ ] Every discard is logged with the reason.
- [ ] If 5 iterations pass with no principled hypothesis forming, stop and escalate — you've hit a model-capacity or eval-validity wall.

After the run:

- [ ] Champion prompt / config / state is saved with provenance.
- [ ] Loop-log is complete and reviewable.
- [ ] Lessons learned are added back to this topic if they generalize beyond the specific task.
- [ ] Any upstream bugs found (like the chess `tactical_analyzer.py` issue) are filed for proper fix, not just disclaimed in the prompt.

---

## Files & Conventions

- **Autoresearch skill:** `~/.claude/skills/autoresearch/SKILL.md` — load this to enter autoresearch mode
- **Chess project state:** `~/chess-games/autoresearch/autoresearch-state.json`
- **Chess champion prompt:** `~/chess-games/autoresearch/prompts/v25-steps-method.txt` (as of Apr 11, 2026)
- **Chess eval suite:** `~/chess-games/autoresearch/evals/eval-suite-50-v3.json`
- **Chess judge:** `~/chess-games/autoresearch/evals/llm_judge.py`
- **Research ratchet log:** `~/.config/grove/loop-log.tsv` (shared with Grove — mixed memory principle)

---

## Change Log

- **2026-04-11 00:15 CDT** — Initial topic created by Grove during the VSM self-improvement session. Captures lessons from the chess autoresearch run (v14 84.17% → v25 90.97%) that Karpathy completed earlier in the same session. The "check upstream context before rewriting rules" insight came from Karpathy discovering the `tactical_analyzer.py` bug and is the load-bearing principle of the document.
