# Systems Thinking Benchmark

## Location
`~/systems-thinking-bench/`

## Purpose
Measures LLM vulnerability to shallow thinking via word problems where the surface diagnosis is wrong and the root cause requires stepping outside the frame. Each problem encodes a system dynamic that pattern-matching misses.

## Architecture
- `run.py` — single-file CLI benchmark runner
- `problems.json` — 10 problems across 6 domains
- `results/` — JSON output files named `{model}_{prompt_tier}_{timestamp}.json`

## How It Works
1. **Problem phase**: Send scenario+question to model via `claude -p`
2. **Judge phase**: Opus scores the response on 5 dimensions (0-2 each, 10 max)
3. **Report**: Print table + save JSON

## Isolation (confirmed working 2026-03-03)
Every `claude -p` call runs fully isolated — no tools, no CLAUDE.md, no MCP, no skills:
```
--tools ""                  # pure reasoning, no tool use
--strict-mcp-config         # block all MCP servers
--disable-slash-commands    # no skills/plugins
--setting-sources ""        # no user/project settings or CLAUDE.md
--no-session-persistence    # don't save sessions
cwd="/tmp"                  # no project-level CLAUDE.md
env without CLAUDECODE      # allow nested invocation
```
**Unavoidable leakage** (hardcoded in CLI): `"You are a Claude agent, built on Anthropic's Claude Agent SDK."` prefix + current date system-reminder. Doesn't affect results.

## Prompt Tiers (`--prompt` flag)
Three levels control how much the system prompt coaches the model:
- **`none`** (default): `"You are a helpful assistant. Answer the question thoroughly."` — no hints
- **`hint`**: `"...consider whether the obvious answer might be missing something..."` — gentle nudge
- **`deep`**: Full coaching — explicitly tells model to look for assumptions, feedback loops, metric gaming

Observed effect: Haiku scores ~6/10 with `none` vs ~8/10 with `deep` on same problem.

## Scoring Dimensions (5 × 0-2 = 10 max per problem)
1. **Frame Identification** — does it name the embedded assumption?
2. **Frame Escape** — does it reframe the problem?
3. **Causal Depth** — does it trace root causes?
4. **System Dynamics** — does it find feedback loops?
5. **Purpose Alignment** — does it distinguish metric from purpose?

## CLI Usage
```bash
python run.py --model haiku                          # all 10 problems, default prompt (none)
python run.py --model sonnet --prompt hint           # gentle hint tier
python run.py --compare haiku sonnet opus            # head-to-head comparison
python run.py --model haiku --problems 1,3,5         # subset of problems
python run.py --judge-only results/file.json         # re-judge existing results
python run.py --judge-model sonnet                   # use different judge
```

## Bug Fixes Applied (2026-03-03)
- Token/cost extraction: `cost_usd` → `total_cost_usd`, top-level tokens → `usage.input_tokens`/`usage.output_tokens`
- Save file variable ordering: `prompt_tier` extracted before use in filename

## Known Issues / Next Steps
- Even `none` tier: Haiku still scores well (24/30 on problems 1-3). Problems may need to be harder — more convincing surface answers that actively mislead.
- Judge (Opus) may be too generous. Could add calibration problems with known-bad responses.
- No tests yet (violates engineering standards).

## 10 Problems
1. **infra-01** The Helpful Cache — cache masks data growth; fix amplifies problem
2. **infra-02** The Reliable Backup — backup preceded corruption
3. **ml-01** The Accurate Forecast — 94% accurate but terrible on Black Friday
4. **sw-01** Faster CI — gutted integration tests; speed masked coverage loss
5. **ml-02** The Fair Algorithm — fairness constraint doesn't fix proxy variable
6. **sw-02** The Empty Queue — looks like success; lost smoothing buffer
7. **sw-03** Monolith→Microservices — succeeded at deployment, failed at dev velocity
8. **org-01** Junior Engineers — stopped learning; cut senior pipeline
9. **infra-03** Green Dashboard — latency convolution across 8 services
10. **org-02** Adding Engineers — drops output; implicit→explicit coordination overhead
