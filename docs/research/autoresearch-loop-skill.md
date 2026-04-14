---
name: autoresearch
description: "Your technique for building LLM applications. Karpathy-style: fixed evaluator, one mutable file, one metric, one program.md that sets the rules, YOU as the loop, commit after every keep. Triggers: 'autoresearch', 'optimize', 'iterate on prompt', 'improve the eval score', 'build an LLM application', 'find the best strategy'."
---

## What this is

This skill codifies **your** technique for building LLM applications of any kind — prompt systems, agent scaffolds, retrieval pipelines, judge systems, eval harnesses. It is the [karpathy/autoresearch](https://github.com/karpathy/autoresearch) pattern adapted for your situation.

In Karpathy's original, a human writes `program.md` to point an AI agent at an ML training loop. The agent edits `train.py`, the evaluator (`prepare.py`) scores it, keep-or-discard, repeat. Fixed 5-minute budget, single metric (val_bpb), one file of truth.

**Your situation is different in one critical way:** you write program.md yourself, because you ARE the agent. You are not pointing a separate system at a loop — you are the loop. The technique still works, but the ritual has to be adapted: before starting any research run, you sit down and write the program.md explicitly. That document becomes the contract that governs every subsequent decision in the session.

This is how you should build LLM applications. Not just ML training. Not just Kalshi trading. **Any system where a language model output is scored against a metric and you want to systematically improve it.**

## YOU are the loop

Read this before anything else.

The single most important principle — and the one most easily lost — is that **you** (Claude, the reasoning system) are the mutation step. Not a Python script. Not a state machine. You.

The LOOP below has 7 steps. The only step that can be automated is step 3 (the fixed evaluator). Steps 1 (hypothesize), 2 (implement), 4 (decide), 5 (commit), 6 (log), and 7 (goto 1) all require judgment that a Python script cannot provide. If you find yourself writing `autoresearch.py` that loops through mutations, **STOP** — that is the canonical April 10 2026 anti-pattern. Prior-Claude wrote a script that ran evaluations and made keep/discard decisions by comparing float scores, and it wasted $16 of OpenRouter credits on nonsense mutations because it could not READ the outputs and see what was actually wrong.

A shell script cannot read an explanation, notice the model hallucinated a defender, form a hypothesis about which analyzer tag would prevent it, and write the three-line fix. You can. Be the loop.

## The three files

Every autoresearch run has three load-bearing artifacts. Same as Karpathy's original.

| File | Role | Who edits | Mutability |
|---|---|---|---|
| **`prepare.py` (or equivalent)** | Fixed evaluator. Data prep, the metric computation, the judge. The ground truth the metric is measured against. | Nobody, during a run. | **FIXED.** Changes to this invalidate the experiment. |
| **`train.py` (or equivalent)** | The mutable surface. The thing you're optimizing. For an LLM application this is typically a prompt file, an analyzer script, a tool-use scaffold, or a post-processor. **There should be ONE mutable file** (or a small, clearly-named set) — not "the whole repo." | YOU, in the loop. | **MUTABLE.** Every keep is a commit. Every discard is a revert. |
| **`program.md`** | The contract. Sets objective, metric, mutation surface, budget, production constraints, stop conditions, commit discipline. Written BEFORE the loop starts. | YOU, before the run. Re-read at the start of every loop iteration. | **SEMI-FIXED.** Only updated between research runs, not during. |

If you cannot name all three files at the start of a session, you are not doing autoresearch — you are wandering. Stop and write program.md.

## program.md template

Copy this template, fill in every section, commit it to the repo before starting the loop. Short is fine; blank sections are not.

```markdown
# program.md — <project name> autoresearch

## Objective
One sentence. What does "done" look like in terms of real production value?
Not the metric. The thing the user will feel.
  Example: "Explain why the best move is best in a chess position, well enough that an
  intermediate student can learn to find similar moves."

## Metric
One number. How is it computed, on what data, with what judge?
Higher or lower = better?
  Example: "avg_score (0-1) = mean rubric-item accuracy across v4 eval suite (45 puzzles),
  judged by Claude Sonnet via batched claude -p CLI with strict rubric. Higher is better.
  Current baseline: 0.735 (v26 + themed analyzer, April 11 2026)."

## Fixed (must not change during a run)
- Which files/scripts are the evaluator?
- Which test data is the ground truth?
- What rubric/judge prompt?
- Any deterministic pre-processing?
  Example:
  - evals/eval-suite-50-v4.json (the 45 puzzles)
  - evals/llm_judge.py judge_explanation_batch_claude path
  - CLAUDE_JUDGE_SYSTEM_PROMPT (the strict rubric)
  - JUDGE_BACKEND=claude:sonnet (never the unbatched OpenRouter path)

## Mutable (what you may edit in this run)
Name the files explicitly. Narrower is better. If it's "everything," you're not focused.
  Example:
  - solution_analyzer.py (render_analysis function only)
  - prompts/v38-steps-method.txt (the v38 prompt template)
  NOT mutable in this run: tactical_analyzer.py, llm_judge.py, the eval suite.

## Budget
- Wall-clock per experiment (e.g. "30s per unit test, 10min for full-suite confirmation")
- Dollar cost per experiment
- Total session budget (hard cap)
- Expected number of iterations
  Example: "30s/unit-test, ~15/hr, $0.03/iteration on 3-puzzle unit, $0.32 for full-45 confirmation. Session cap: $5."

## Production constraints (leakage rules)
What data will the deployed system NOT have? The generator path must only see
what will be available in production. Any hand-labeled metadata is contamination.
  Example: "The generator sees FEN + engine-produced solution + static prompt.
  It does NOT see steps_concept, lichess_themes, trigger, mechanism, ideal_explanation,
  vocabulary, common_errors, pattern_lesson. Run evals/leakage_check.py before any
  baseline claim."

## Commit discipline
Every KEEP gets a git commit, immediately, with a message naming the hypothesis and
the measured delta. Every DISCARD is a `git checkout --` on the mutable file.
Never leave an uncommitted improvement.

## Cold generalization policy
Before declaring any fix "shippable," run it on N positions OUTSIDE the training set.
Where do those positions come from? How are they judged?
  Example: "Pull 3-5 random Lichess puzzles (disjoint from v4), run the current pipeline
  cold, read each explanation as a human teacher. No rubric, no model judge. If quality
  is visibly similar to training-set gains, ship. If not, diagnose."

## Stop conditions
- When do you stop iterating? (Metric ceiling? Budget exhausted? Pareto-optimal on cost/quality?)
- When do you escalate to a human instead of iterating further?
  Example: "Stop at 85%+ on full suite, OR when 3 consecutive fixes fail to move the metric,
  OR when session budget > $5 spent, whichever first."

## Known limitations (carryover from prior runs)
Anything you've already tried and ruled out. Failure modes you know exist and haven't
solved yet. Do not re-attempt without a new hypothesis.

## Session log pointer
Path to the TSV/markdown where every experiment in this session is logged.
  Example: "autoresearch/experiments.tsv — one row per attempt: timestamp, hypothesis,
  metric-before, metric-after, keep/discard, commit-sha."
```

## The operational substrate: `.lab/`

**Borrowed from [krzysztofdudek/ResearcherSkill](https://github.com/krzysztofdudek/ResearcherSkill).** Every research run gets a `.lab/` directory in the project root. It is **untracked** (add it to `.gitignore`) and holds the operational state of the research session. Git manages code; `.lab/` manages experiment history. They are independent and both survive resets.

```
.lab/
├── program.md          # The contract (what you wrote in phase 0)
├── results.tsv         # One row per experiment: #, sha, hypothesis, metric_before, metric_after, status, duration
├── log.md              # Narrative THINK/TEST/REFLECT entries, one section per experiment
├── parking-lot.md      # Deferred ideas you don't want to lose but aren't testing now
├── branches.md         # (optional) If you fork, track branch genealogy here
└── workspace/          # Scratch files, per-experiment subdirectories
```

Always add `.lab/` to `.gitignore` before starting. When resetting the mutable file after a DISCARD, the `.lab/` state is untouched — the discarded experiment still has a row in `results.tsv` and a section in `log.md`, so you can revisit it later.

### Phase 0: Resume check

At the start of every session, check if `.lab/` already exists in the project root.

**If it does:** read `program.md`, tail `results.tsv`, and the last 5 entries of `log.md`. Present a one-screen summary: objective, current baseline, best-so-far, number of experiments, last experiment status. Ask the user: resume or start fresh? If resume, pick up from next experiment number. If start fresh, archive to `.lab.bak.<timestamp>/` and proceed to Phase 1.

**If it doesn't:** proceed to Phase 1 (write program.md).

This is how you survive context limits and session interruptions. Prior runs are durable because `.lab/` is not in your chat context — it's on disk.

## The loop

```
REREAD program.md and the last 5 entries of .lab/log.md  (every iteration — you are the working memory)

LOOP:
  1. THINK      — write a `## THINK — before Experiment N` section in .lab/log.md:
                   - Convergence signals (any guardrails firing?)
                   - Untested assumptions (have you tried the opposite of what's working?)
                   - Invalidation risk (could earlier findings be stale after recent changes?)
                   - Next hypothesis (what will you test and why — one sentence, falsifiable)
                   The log entry IS the evidence that you thought. No entry = didn't happen.
  2. Implement  — modify the mutable file. ONE change at a time. Not three.
                   If you edit the analyzer AND the prompt in the same step, you cannot
                   tell which caused the change.
  3. Commit-before-running — stage the change, commit with:
                   experiment #N: <short description>
                   Hypothesis: <one-line hypothesis>
                   Parent: #<parent experiment>
                   This forces structure and gives the discard a clean reset target.
  4. Evaluate   — run the fixed evaluator on a SMALL, TARGETED sample (1-3 cases that
                   exercise the specific failure mode you're fixing).
                   READ the outputs directly. Do not trust the aggregate alone.
  5. Log first  — write a `## Experiment N — <title>` section in .lab/log.md AND a row in
                   results.tsv (including the commit SHA). This happens BEFORE the decide step
                   so a reset doesn't erase the record.
  6. Decide     — metric improved AND no regression on read? KEEP.
                   Otherwise DISCARD: `git reset --hard HEAD~1`. The commit disappears from
                   the branch but its SHA is preserved in results.tsv for future forking.
                   No "let me think about it." Binary.
  7. GOTO 1     — never pause, never ask permission, never stop until program.md's stop
                   conditions are met.
```

**Commit-before-running is load-bearing** (stronger than my previous "commit after keep"). Every experiment gets a commit the moment the code change is complete, with a structured message. If it's a keep, the commit stays. If it's a discard, `git reset --hard HEAD~1` removes it from the branch but the SHA and description are preserved in `.lab/results.tsv`. This gives you clean resets AND a full experiment history.

## Guardrails (adapted from ResearcherSkill)

These are not suggestions. They are mandatory triggers. Log the trigger entry in `.lab/log.md` before proceeding.

- **3+ discards in a row:** STOP. Write a `## 3-Discard Guardrail — after Experiment N` entry in `.lab/log.md` reviewing convergence signals and documenting why you are continuing on the current approach vs. forking or switching tactics. No entry = you don't get to keep iterating.
- **5+ discards in a row:** Forking is the DEFAULT action. Before forking, check `.lab/parking-lot.md` for untested ideas. To stay on the current branch instead, you must name a specific untested hypothesis that is NOT a variant of what you already tried. If you cannot, fork.
- **Global best unchanged for 8+ real experiments:** You are on a plateau. Fork from baseline (experiment #0 = the initial unchanged state) with INVERTED assumptions. This means: write down what the current best assumes (e.g. "more detail in the prompt helps"), and try the opposite (minimal prompt). Not a minor tweak in the same region — a different region entirely.
- **Every 10th real experiment:** re-validate the current HEAD by re-running the full evaluator and comparing to the recorded best metric. If it regressed more than 2%, log the drift and consider forking back to the recorded best. This catches silent metric corruption from non-determinism, judge drift, or accumulated unrelated changes.

## Parking lot

When you think of an idea during one experiment that doesn't belong to the current hypothesis, write it to `.lab/parking-lot.md` instead of chasing it. One line per idea. Drain the parking lot when you're at a fork decision or plateau.

Example entries:
```
- Try dropping the vocabulary bank from the prompt; maybe the analyzer signals are enough
- Does material count improve if I count implied recaptures too?
- Test on 5 puzzles where the solution ends in mate vs material gain, see if those classes behave differently
- Maybe swap the judge to haiku to see if my Sonnet-specific optimizations generalize
```

## Principles

1. **One metric.** Not a weighted combination. Not "accuracy AND latency." Pick one number. Everything else is a filter applied after ranking.

2. **Fixed evaluator.** If you're tempted to "adjust the evaluator to be fairer," STOP. The evaluator is ground truth. If it gives bad results, your strategy is bad, not the evaluator. Changing the evaluator mid-run invalidates every prior experiment.

3. **Keep/discard is binary.** Did the metric improve? Keep. Did it not? Discard. No "it's close enough," no "let me think about it." Binary. The hardness of the discard is the point.

4. **Log everything.** Every experiment gets a row in the TSV. Discards are as informative as keeps — often more so. The log IS the learning.

5. **Never stop (until program.md says to).** The loop runs until the stop conditions in program.md fire. If you run out of ideas, think harder — combine near-misses, try radical departures, invert assumptions. Do not ask the human for the next idea. Do not pause "to report progress." Run the loop.

6. **Fast iterations > clever ideas.** Karpathy's insight: 5-minute experiments, 100 overnight. Speed of iteration beats quality of individual hypotheses. If your evaluator takes 1 second, run 1000 experiments, not 10 careful ones. If your evaluator takes 30 minutes, use unit tests (see principle 9).

7. **Anti-overfit filters AFTER ranking.** Rank by metric first. Then filter: minimum sample size, no regressions on read, cold-set performance. This separates "finding edge" from "validating edge."

8. **Read the outputs. Don't trust aggregates alone.** A model scoring 70% with 5 factual errors is WORSE than 85% with generic wording. When the metric moves, read individual outputs to verify. Aggregates can lie; the raw outputs don't.

9. **Unit tests, not integration tests, during iteration.** LLM evals are expensive: API cost, latency, judge noise, wall-clock. During iteration, pick 1-3 cases that exercise ONE failure mode. Run the full suite ONLY as an integration check at session end — treat it like CI, not like pre-commit. If your full eval takes 30 minutes and you run it 5 times per iteration, you've spent 2.5 hours measuring for 2 minutes of thinking. Wrong ratio.

10. **When the LLM gives wrong output, check the input context before changing vocabulary or instructions.** Rules in prose compete badly with training priors; context reshaping works because the LLM is a pattern-matcher on whatever is in attention. If the explanation is missing a term, check whether the analyzer emitted the term. If the model fabricated a defender, check whether the analyzer explicitly said "Support: none". The analyzer-layer fix is usually higher leverage than the prompt-layer fix.

11. **No leakage from labels to the generator.** The evaluator is allowed to see the rubric (it IS the rubric). The generator must only see data it will have in production. Hand-labeled metadata must not flow to the generator path. Verify with a programmatic leakage check — do not trust your memory. Run it before claiming any baseline number.

12. **Cold generalization test before declaring a fix works.** Before calling a fix "real," test it on 3-5 cases NOT in the training set — ideally from a disjoint source. If gains transfer, the fix is structural. If gains evaporate, you were overfitting. Small sample, direct reading, human judgment. No rubric needed.

13. **Commit after every keep.** Every KEEP is a git commit with a descriptive message. The commit history IS the ratchet log. Without it, you have no durable record of what worked and no ability to cleanly revert a future regression.

## Multi-evaluator protocol (optional)

**Borrowed from ResearcherSkill.** For most tasks, a single well-calibrated judge is fine. This protocol is an OPTION to reach for when single-judge results feel unreliable — specifically when you suspect you're gaming the judge or the scores stop tracking real quality.

When you do reach for it:
1. **Spawn 3 evaluator subagents** (Agent tool calls), each with no shared context.
2. **Each evaluator receives ONLY**: the candidate output, the rubric, instructions to return structured scores. No hypothesis, no experiment number, no "what changed." Blind.
3. **Aggregate by median** (not mean) to resist outliers.
4. **Flag divergence**: if any evaluator differs from the median by more than 20% of the scale, log it as a disagreement. Multiple disagreements = rubric problem, consider metric revision.

Reach for this when: the rubric feels gameable, your single-judge scores don't track cold-set quality, or you're about to claim a major improvement and want defensive verification. Don't default to it on every experiment — the cost (3 subagent calls per eval) adds up fast.

## Anti-patterns (things that killed prior runs)

### The Python state machine (Apr 10 2026)
Prior-Claude wrote `autoresearch.py` — a Python script that called Ollama to mutate prompts, called OpenRouter to evaluate them, parsed the float score, decided keep/discard, and looped. It ran for an hour and burned $16 of credits on nonsense mutations. **It failed because it could not read the outputs.** The mutations got "better" on the metric by gaming the rubric while the actual explanations grew incoherent. A human (or Claude) reading one output would have caught it instantly.

**The fix:** delete the script. YOU are the loop. Scripts are fine for the fixed evaluator (prepare.py / llm_judge.py). Scripts are NOT fine for the mutation/decide steps.

### The full-suite iteration (Apr 11 2026)
Prior-Claude (same session) ran the full 45-puzzle eval after every prompt change. Each run took 30 minutes. 5 iterations = 2.5 hours of wall-clock for 10 minutes of actual thinking. Most of the iterations were directionally obvious from reading 1-3 puzzle outputs — the full suite was adding noise, not signal, and burning budget.

**The fix:** unit tests during iteration (3 puzzles, 30 seconds), integration tests at session end (full suite, once).

### The receptionist agent (Apr 10 2026)
Prior-Claude built a "front-door" agent to forward user messages to Karpathy because responses were slow. This violated one-mind-one-interface. The actual problem was blocking Bash calls; the fix was `run_in_background: true`. The receptionist solved nothing and added a hop.

**The fix:** when the system feels slow, check for blocking I/O before adding layers. Never spawn a persistent agent as a latency workaround.

### The rubric game
Prior-Claude optimized for "does the model say the literal word in the rubric item" without checking whether the explanation was actually teaching something true. Scores went up, real quality went down. Cold-set test would have caught it.

**The fix:** read cold outputs after every significant fix. If the rubric gains don't transfer, you're gaming.

## When to write a new program.md

Whenever you start work on a new LLM application, or take over an abandoned one, or significantly change the objective. Rule of thumb: if you cannot answer "what's the metric and what's mutable?" in one sentence each, stop and write program.md.

Writing program.md is not optional ceremony. It's the contract that prevents you from silently drifting during a long session. Re-read it at the start of every loop iteration. If a hypothesis requires violating program.md (e.g., you want to change the evaluator), stop the loop and update program.md first — don't sneak around it.

## Applying to different domains

The pattern works anywhere you have:
- A scoring function you trust
- A parameter (or prompt, or code) space to search
- Fast evaluation (seconds to minutes, not hours)

**LLM application examples:**
- **Prompt engineering** — metric: accuracy on eval set. Mutable: prompts/v38.txt. Fixed: eval suite + judge.
- **Analyzer / tool scaffolding** — metric: downstream model quality. Mutable: the analyzer code. Fixed: prompt, eval, judge.
- **Agent scaffolding** — metric: task completion rate on a benchmark. Mutable: the agent's instruction file + tool list. Fixed: the benchmark.
- **Retrieval pipelines** — metric: answer accuracy. Mutable: embedding model / chunking / reranker. Fixed: QA eval set.
- **Skill development** — metric: trigger precision + completion accuracy. Mutable: SKILL.md. Fixed: test prompts designed to trigger (and not-trigger) the skill.

**Non-LLM examples:**
- **Kalshi trading** — metric: OOS ROI. Mutable: strategy params. Fixed: historical backtest data + backtest.py. See "Tools" below.
- **ML hyperparameter search** — metric: val_bpb. Mutable: train.py. Fixed: dataset + compute budget. (Karpathy's original.)

### Adaptation checklist

1. **Define the evaluator.** What function takes a candidate and returns a number? Is it trustworthy? Does it use temporal holdout (no lookahead)? Is the evaluator fast enough to run in-loop? If not, can you make a smaller "unit-test" evaluator for iteration?
2. **Define the metric.** What single number are you optimizing? Higher or lower? Current baseline?
3. **Define the mutable surface.** What ONE file (or small set) can change? What CANNOT change? Write these down in program.md.
4. **Define the budget.** Per-experiment cost. Per-session cap. Wall-clock.
5. **Define the stop conditions.** When do you ship? When do you escalate?
6. **Run the leakage check.** Make sure the evaluator sees production-realistic data only.
7. **Write program.md.** Then iterate.

## Tools

### For Kalshi trading
- `~/.config/kalshi/model/autoresearch.py` — Grid search across strategy families for tennis markets
  - This is the one legitimate use of a Python loop script: the evaluator is deterministic,
    the parameter space is discrete, and keep/discard is purely arithmetic. No LLM in the loop.
  - `python3 autoresearch.py --tour CH --top 30` — Search CH strategies
  - `python3 autoresearch.py --tour WTA --top 30` — Search WTA strategies
  - Results: `autoresearch_results.tsv`
  - Evaluator: `backtest.py` (walk-forward, validated to $0.000001)
  - Metric: OOS ROI (target: >= 20%)
  - Anti-overfit: zero negative months, minimum 10 trades

### For LLM application work
**No central script.** The loop is you, reading outputs and editing one file at a time.
- `git` for commit discipline (every keep is a commit)
- A TSV or markdown log for session history
- A `leakage_check.py` specific to your project (see chess autoresearch for a working example at `~/chess-games/autoresearch/evals/leakage_check.py`)
- A small unit-test runner that evaluates 1-3 cases in <60 seconds
- A full-suite runner that confirms the final result at session end

## Known gotchas (LLM-specific)

- **Judge cost explosion.** Per-criterion OpenRouter judges can cost 10x what batched Sonnet-via-Max judges cost. Before any full eval, verify `JUDGE_BACKEND=claude:sonnet` is set and the code path actually uses it. The April 11 rebaseline discovered $24 of burn from an unbatched judge path nobody remembered setting.
- **tee buffering.** `python script.py | tee log.txt` causes python's stdout to become block-buffered. You won't see progress in the log until the buffer fills or the process exits. Use `stdbuf -oL` or `python -u` to force line buffering during iteration.
- **Zombie subprocesses.** `kill <shell-pid>` does NOT propagate to python children. Use `pkill -P <pid>` or track the python PID directly. Prior-Claude wasted $0.30 letting a zombie deepseek-r1 eval run for 1h23m after "killing" its shell wrapper.
- **Prompt-example contamination.** If your prompt has worked-out examples, those examples must NOT overlap with your eval set. The `leakage_check.py` pattern detects 40-char substring overlaps. April 11 2026 caught 3 contaminated puzzles this way.
- **Rubric gaming.** A model can learn to say "zugzwang" because the rubric checks for it, without actually understanding zugzwang. Cold-set testing is the only defense.
- **Fix-generalization gap.** A fix that works on 8 training puzzles may fail on the 9th cold puzzle. Always verify on disjoint positions before claiming a ratchet forward.
- **Judge model as implicit target.** If you always judge with Sonnet, you're implicitly optimizing for Sonnet's interpretation of the rubric. Swap the judge occasionally (Haiku, GPT-5, human) to see how brittle the metric is.

## Known gotchas (Kalshi, carryover from prior skill version)

- **Duplicate results** — Many parameter combos select identical trade sets. Deduplicate by checking actual trades, not just params.
- **Small samples** — High ROI on 12 trades means nothing. Require minimum N and multiple time periods.
- **Temporal concentration** — All trades in one month = not robust. Require spread across time.
- **Parameter plateau** — If 45 combos all show +20% ROI, the edge is real but the specific params don't matter much. Pick the simplest.

## Source

- [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — the original. Default branch is `master`. Read the README for Karpathy's design rationale: single file to modify, fixed time budget, one metric.
- This skill is the LLM-application adaptation. The core pattern is Karpathy's; the adaptations (commit-every-keep, unit-tests-over-integration, leakage discipline, cold-set verification, anti-script warnings) are lessons from April 10-11 2026 autoresearch failures on the chess explainer project.
