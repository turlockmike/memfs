# PoE2 Crafting Expert System
Last updated: 2026-04-09

## Goal
Make the poe2-expert agent "the foremost expert on PoE2 in the entire world when it comes to crafting" (Mike, Mar 26).

## Architecture
- **poe2-expert** — claude-loop agent at `~/.claude/agents/poe2-expert.md`
- **App** — `~/apps/poe2-craft-advisor/` (Next.js 16 frontend + Python FastAPI backend)
- **Research** — `~/apps/poe2-craft-advisor/research/` (eval framework, encyclopedia, analyses)

## CLI Tools
- `poe2-wiki` (`~/.local/bin/poe2-wiki`) — reference lookup against local encyclopedia. Searches by header/content match. Key to eliminating PoE1 contamination.
- `poe2-craft-cost` (`~/.local/bin/poe2-craft-cost`) — cost optimizer. Known bugs: flat 25% recombine rate (should be weight-based 3-45%), mod matching overly broad (lightning damage matches both flat and %), no expedition artifact cost for recombination.
- `poe2-currency` (`~/.local/bin/poe2-currency`) — real-time currency prices from poe2scout.com API (hourly cache at `research/data/currency-prices.json`).
- `poe2-trade` (`~/.local/bin/poe2-trade`) — **NEW Apr 8.** GGG Trade API client with built-in rate limit protection. Fetches trade history for PoE2 (and PoE1 via `--poe1`). Non-blocking design — returns immediately with `retry_at` timestamp if rate limited (exit code 2). 9 tests at `~/.config/poe2-trade/test_rate_limiter.py`. State at `~/.config/poe2-trade/rate-state.json`.
  - **GGG Rate Limits (verified Apr 9):** 3-tier rolling window: Tier 1 = 5 req/60s (ban 60s), Tier 2 = 10 req/600s (ban 120s), Tier 3 = 15 req/10800s (ban 3600s). Safety margin = 1 (blocks at max-1). 1-min tier genuinely resets after 60s window. Higher tiers accumulate independently.
  - Current league: "Fate of the Vaal"

## Reference Documents
- `research/poe2-currency-encyclopedia.md` — 697 lines, 19 sections covering all PoE2 currencies. Source of truth for poe2-wiki.
- `research/poe2-recombination-research.md` — 781 lines, 20 sections on recombination mechanics (weight-based odds, ilvl breakpoints, low-tier strategy, expedition costs).
- `research/breach-ring-analysis-v2.md` — 469 lines, analysis with REAL prices. Key finding: any strategy requiring annulments (18 ex) or fracturing (6835 ex) is non-viable.

## Eval Framework
- Quiz: `research/eval/currency-quiz.json` — 85 questions, 14 categories
- Runner: `research/eval/run-quiz.py` — feeds questions to poe2-expert, scores via LLM judge
- Scorecard: `research/eval/quiz-scorecard.md`
- Results: `research/eval/quiz-results.json` (V1), `quiz-results-v2-rerun.json` (V2 rerun of failures)

## Performance
- **V1 (raw LLM):** 61/85 (72%) — heavy PoE1 contamination
- **V2 (tool-first prompt):** 79/85 (93% projected) — 18/24 failures fixed
- **Remaining 6 failures:**
  - 3 max-turns timeouts (Q039, Q056, Q083) — infrastructure, increase max-turns
  - Q022: Omen of Corruption — partial (missed downside framing)
  - Q060: Expedition vendors — Tujen role wrong in encyclopedia
  - Q082: 3-to-1 vendor recipe — denied it exists, needs encyclopedia entry

## Key Insight: PoE1 Contamination
LLM training data has far more PoE1 than PoE2 content. Without the poe2-wiki tool, the agent defaults to PoE1 mechanics (Chaos rerolls, Transmutation adds 1-2 mods, etc.). The tool-first prompt ("BEFORE answering, you MUST run poe2-wiki") eliminates this: V1 72% → V2 93%.

## Real Price Economics (Mar 26)
- Annulment Orb: 18 ex → aug-annul cycle costs ~380 ex for targeted mods
- Fracturing Orb: 6,835 ex → kills all fracture strategies
- Only annulment-free strategies survive at current prices
- Recombination is the budget path (~0.9-1.4 ex for Breach Ring recipes)

## Next Steps (QUEUED — do these next session)
1. **Fix encyclopedia** (`research/poe2-currency-encyclopedia.md`):
   - Omen of Corruption: add "both GOOD and BAD outcomes become more likely" (Q022 failure)
   - Add Expedition Vendors subsection: Gwennen=gambling, **Tujen=currency exchange/haggling (NOT crafting)**, Rog=crafting+recombination, Dannig=logbooks (Q060 failure)
   - 3-to-1 recipe: rename to "3-to-1 Vendor Recipe (exists in PoE2)", add bold note it IS present, list confirmed applications (Q082 failure)
   - Currency shards: expand with "10 shards auto-combine" mechanic, source table (Q083 timeout but also knowledge gap)
2. Increase `--max-turns` from 8 to 12 in `run-quiz.py` (3 timeouts caused by agent spending too many turns on tool lookups)
3. Re-run full quiz (`python3 run-quiz.py --fresh`) to verify 93%+ achieved
4. Update `playbook.md` Session 38 log (couldn't write from restricted sandbox)
5. Update poe2-craft-cost: weight-based recombine rates, expedition artifact cost, mod matching fix
6. Re-run breach ring analysis with fully-equipped agent
