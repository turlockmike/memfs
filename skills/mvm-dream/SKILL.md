---
name: mvm-dream
description: "MVM offline integration. Reads mvm stats + recall-log; resolves every detected anomaly within the same cycle (no sinks). Coverage → proactive ingest. Quality failure → cross-check → re-ingest if confirmed. Staleness → cross-check → supersede if confirmed. Contested/gap → user escalation. Triggers: '/mvm-dream', 'mvm dream', 'mvm consolidate', 'mvm maintenance'."
---

# /mvm-dream

**FEP rule: every detected anomaly resolves in this cycle** — it dissolves
(cross-check disconfirms) or drives a substrate change (ingest/supersede).
Per-cycle cap: 3 substrate-changing ingestions. History & rationale for every
rule here: `~/resources/mvm-incident-history.md`.

**Selection rule (quality + staleness): least-recently-VERIFIED first** via
`dream-verify-pick pick <N> [exclude...]` (reads
`~/mvm/state/quality-verified.jsonl`; never-verified ranks epoch-0, tiebreak
oldest-mtime). After each probe: `dream-verify-pick record <doc> <PASS|FAIL>
<quality|staleness>` — this rotates the next cycle to fresh docs.

**Probe logging:** every kb/web verification probe below is logged as you
grade it (the fail-closed Stop hook requires ≥1 append when clones spawned):
```bash
echo '{"question":"<test q>","topic_hint":"dream-quality-verify",
 "decided_source":"kb","decided_answer":"<answer> — <doc> PASS/FAIL"}' | recall-log add
```
(`topic_hint` dream-quality-verify | dream-staleness-verify; the CLI infers
`kind:"dream-probe"` from it, so probes never pollute `mvm stats` — which
defaults to `--kind recall`. Use the honest enum source: `kb` for doc-grounded
probes, `web` for web-grounded ones.)

## Steps

0. **Meta-audit** — read last 10 entries of `~/mvm/state/dream-log.jsonl`:
   - Same canonical in `quality_repairs` ≥3 cycles → mark frontmatter
     `status: chronic_failure`; surface to user with history.
   - Same topic in `coverage_ingests` ≥3 cycles, fallback rate flat →
     escalate (suggest manual ingest of an authoritative source).
   - `transients` outpacing `quality_repairs` over the window → cross-check
     disconfirming too often; surface (clone noisy or threshold wrong).
   - `duration_ms` monotonically rising → schedule consolidation; surface.
   - Same question `still_split` ≥2 cycles → permanently contested; escalate,
     stop re-attempting.
   Every pattern drives a meta-action or escalation; nothing flat-logs.

1. **Dashboard** — `mvm stats --window 7d --json` (defaults to real recalls).
   Capture top fallback topics, source mix.

2. **Coverage — proactive ingest** (max 3/cycle): for each top-fallback topic,
   find the most-frequent web URL among `kind=recall` ledger entries; spawn
   `/mvm-ingest <URL>` in background.

3. **Quality — re-verify + repair** (`dream-verify-pick pick 5`): per doc,
   **first probe via the engine** — `mvm verify <doc> --test-id <random id>
   --json` (cold-clone + auto-grade; zero doc bytes through your context).
   Log probe; stamp `dream-verify-pick record <doc> <PASS|FAIL> quality`.
   - Engine PASS → done.
   - Engine FAIL → cross-check with ONE orchestrator-graded cold-clone
     (inject doc, grade per doctrine: lenient on phrasing/enumeration
     completeness, strict on facts — the engine grader is measurably
     stricter, exp6 2026-06-09). Both fail → `/mvm-ingest` the doc's
     `source:` URL (overwriting re-ingest). Cross-check passes → transient,
     no action.

4. **Staleness — spot-check + supersede** (`dream-verify-pick pick 3
   <step-3 docs...>`; stamp each `PASS staleness` after):
   - **Skip-guard first:** `mvm relations <doc> --rel superseded_by --json` —
     non-empty `out` edge = already superseded → skip, take next-oldest.
   - Else: first test's `q` → KB-clone (Read doc) + web-clone (WebSearch) in
     parallel; log probes.
   - Agree → done. Disagree with fresher-source web markers → second web probe,
     different phrasing. Confirmed → `/mvm-ingest` fresh URL; mark old
     canonical `status: superseded` + `superseded_by: resources/...`
     (root-relative); `mvm index`; **verify the edge resolves**
     (`mvm relations <old-doc> --rel superseded_by` must list the new doc —
     empty = dangling pointer, fix before moving on). Disconfirmed → transient.

5. **Contested + gaps + cross-contradiction sweep:**
   - `decided_source:"contested"` since last dream → 5 paraphrased web probes;
     convergence → ingest consensus; still split → escalate.
   - `decided_source:"none"` (hard misses) → propose as curriculum items.
   - **Hot-doc test coverage:** `mvm heat --untested --top 10` — author locked
     tests for the SINGLE hottest untested doc (3–8 cases, /mvm-ingest step 2
     style, injected-verify before commit). One per cycle; verification budget
     follows retrieval heat, not age. Longitudinal metric: `mvm heat` coverage
     % (baseline 35% @ 2026-06-09; target ≥90%).
   - **(a) Consume known tensions:** per doc touched in steps 3–4,
     `mvm relations <doc> --rel in_tension_with --json`; re-probe any pair
     (one cold-clone each). Now agree → remove `in_tension_with:` from both +
     re-index. Still contradict → leave the durable edge.
   - **(b) Discover new tensions:** `mvm search "<random recent-canonical
     keyword>" --top-k 3`; if top-2 similarity > 0.7, ask both docs the same
     probing question via 2 cold-clones; contradiction → flag both
     `status: cross-contested` + reciprocal `in_tension_with:` edges
     (root-relative), `mvm index`, surface to user.

6. **Log cycle — compose the entry as a Python dict, `json.dumps`, then
   append via `dream-log-append "$LINE"`.** The CLI **enforces the schema**
   (canonical `actions` keys, dict-typed `phase_2_meta_review`, inline —
   never pointer — `mistakes_2plus_30d_assessment`, single-object JSON,
   newline repair) and **rejects non-compliant entries**: on exit≠0, fix the
   reported field and retry — never bypass, never bare `>>`.

   Entry shape (canonical `actions` keys are exactly these; empty arrays may
   be dropped):
   ```json
   {"ts":"...","session_id":"...","stats_snapshot":{...},
    "meta_audit":{"chronic_failures":[],"stuck_topics":[],
                  "noisy_threshold":false,"permanently_contested":[]},
    "actions":{"coverage_ingests":[],"coverage_edits":[],"quality_repairs":[],
               "quality_verifies":[],"staleness_supersedes":[],
               "contested_resolved":[],"gaps_surfaced":[],"transients":[],
               "substrate_fixes":[]},
    "step_results":{...},
    "phase_2_meta_review":{
       "predictions_accuracy":{...},
       "mistakes_2plus_30d_assessment":"<full inline 2+/30d root-cause-clustered assessment>",
       "s4_initiatives":{...},
       "session_capsule_check":{"exists":true,"action":"skip"}},
    "escalations":[],"duration_ms":0}
   ```
   S4 path pin: the s4-initiatives ledger is
   `~/.local/state/alfred/areas/s4-initiatives/` (NOT `~/areas/` — stubs only).
   Approximations go in string fields (`"count_7d_approx":"~11"`), never a
   tilde in a numeric slot.

   **Hook-ship preserve gate:** if this cycle shipped a NEW hook, then BEFORE
   the log append: declare it in `~/.local/state/alfred/hooks-preserve.json`
   and confirm `hooks-preserve-check` PASS. FAIL blocks the append exactly
   like a schema FAIL (generic discipline: alfred.md Pre-Ship Gate #7).

7. **Report** (concise):
   ```
   Dream cycle complete.
   Meta:       N chronic-failure flags, M stuck topics  [if any]
   Coverage:   N ingests
   Quality:    N repaired, M transient
   Staleness:  N superseded, M transient
   Contested:  N resolved, M still need user input
   Gaps:       N surfaced for curriculum
   Escalations:  <items needing your attention>
   ```
