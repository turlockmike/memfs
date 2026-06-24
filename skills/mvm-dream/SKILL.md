---
name: dream
description: "Maintain and consolidate my memory — the offline integration pass. Reads memory stats + the recall log, finds gaps / staleness / quality problems, and resolves each within the cycle: proactively ingesting missing knowledge, cross-checking and superseding stale facts, escalating contested ones. This is how my memory keeps itself honest and current across sessions. Triggers: '/dream', 'consolidate memory', 'memory maintenance', 'audit my knowledge' (also runs on a cron)."
---

# /dream

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
   **Stale-ledger pre-check (run FIRST, before the predictability gate):** the
   recall ledger is append-only, so a count-1 fallback often PREDATES a doc that
   has since shipped — the topic is already covered and the fallback is a ledger
   artifact, not a gap. Before treating any fallback as a gap, `mvm search` the
   topic (or check the obvious recall-surface path, e.g.
   `resources/poe2/0.5/crafting/`): if a doc exists AND answers the query →
   SKIP, no ingest (double-ingest is a regression). This catches what the
   predictability gate misses — curated-domain topics (PoE2/finance/Kalshi)
   pass the "stable shape → consolidate" test yet are already-shipped.
   **Query-predictability gate** (empirical — sleep-time compute,
   arXiv:2504.13171: offline-consolidation gains scale with how predictable
   future queries are from the substrate; ~2.5x amortization needs ~10 related
   queries): consolidate fallback topics with stable, recurring question shapes
   (curated domains, repeat-hit topics); a one-off novel-investigation topic in
   the fallback list is answer-live territory → SKIP the ingest, because the
   ingest cost never amortizes. Canonical:
   `~/mvm/knowledge/topics-alfred-state/memory-architectures/letta-sleep-time-compute.md`.

3. **Quality — re-verify + repair** (`dream-verify-pick pick 5`): per doc,
   **first probe via the engine** — `mvm verify <doc> --test-id <random id>
   --json` (cold-clone + auto-grade; zero doc bytes through your context).
   **ALWAYS pass `--test-id` (ONE test); never run bare `mvm verify <doc>`
   (full suite) — full-suite reliably TIMES OUT @200s on large docs
   (poe2-crafting-codex, 0.5.0-patch-notes burned ~10min for 0 verdicts,
   dream-20260614-0431), and a timeout judges nothing → counts only as a
   `transients` probe-noise entry, not signal. One id = one cheap verdict.
   Log probe; stamp `dream-verify-pick record <doc> <PASS|FAIL> quality`.
   - Engine PASS → done.
   - Engine FAIL with an `error` field / retry-exhausted EMPTY stdout →
     classify as `transients` (probe-noise: the retriever/CLI failed, the doc
     was never judged), NEVER a content defect or quality repair (auditor
     sign-off dream-20260610-0100, point 4).
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
   append via `dream-log-append "$LINE"`.** **RECEIPTS-FIRST (added 2026-06-11):
   if long verifies are still running when every other step is done, append
   the entry NOW with those items marked `"<doc>:PENDING"` in `actions`, then
   amend after **via `dream-log-amend <session_id> '<patch-json>'`** (flock'd,
   value-only, audited; added 2026-06-11 because hand-rolled rewrites of the
   canonical log missed a residual marker) — never hold the whole cycle's
   receipts hostage to the slowest probe. The amend done-test greps the
   amended ENTRY for `PROVISIONAL|:PENDING`, not just the tests.yaml.
   **AMEND SWEEPS ALL FIELDS (2026-06-14): a `:PENDING`/PROVISIONAL marker
   you wrote into prose fields (`step_results`, `step_3_quality`) lingers even
   after you clear the canonical `quality_verifies` marker — `dream-amend`
   replaces one top-level key, so a single patch misses the others.
   `dream-recent-clean` keys ONLY on the canonical marker (so it correctly
   goes green), but a naive `grep PENDING` on the entry still hits the stale
   prose. When amending, patch EVERY field that carried the marker, then
   grep-verify the whole entry clean.** Cron/staged sessions can be cut at any moment; results that exist
   only in-context are lost (the 18:07 6/11 audit lost its entire log+report
   this way and REFLECT had to forensically recover it from the transcript).**
   **STAGED-WAKE REFLECT BINDING (2026-06-11, 2nd occurrence same day): any
   stage that recovers or finishes a cut dream/audit cycle owns this append.
   The cycle is INCOMPLETE until the dream-log line exists — an audit report,
   journal entry, or trace does NOT substitute (dream-recent-clean keys ONLY
   on dream-log.jsonl recency, so a report-without-append forces a redundant
   cycle next wake). Recovery checklist order: dream-log append (with
   `:PENDING` markers) FIRST, then report/journal/trace.**
   The CLI **enforces the schema**
   (canonical `actions` keys, dict-typed `phase_2_meta_review`, inline —
   never pointer — `mistakes_2plus_30d_assessment`, single-object JSON,
   newline repair) and **rejects non-compliant entries**: on exit≠0, fix the
   reported field and retry — never bypass, never bare `>>`.

   **PARTIAL / interrupted cycle (2026-06-22, auditor Rec 2 part-b):** if this
   cycle is CUT before all phases complete — a dark-recovery catch-up that a
   Mike interject or staged-wake boundary truncated, phase_2 not run, a verify
   left genuinely undone (not just `:PENDING`-then-amended) — set
   `"partial": true` (real JSON boolean) + a `"partial_reason"` string in the
   entry. **Why it's load-bearing, not cosmetic:** `dream-recent-clean` treats
   a `partial` entry exactly like a dirty (high/critical) one — it does NOT let
   the entry's fresh ts suppress the next owed cycle, so the deferred phases
   re-fire instead of the lane reading clean-when-owed. A complete cycle omits
   the field (absent = false = redundant-cluster-suppressible as before). Do
   NOT mark partial just because a verify is slow — that's the `:PENDING` +
   `dream-log-amend` path; partial is for work that will NOT be finished by
   this session at all.

   **TS DISCIPLINE (2026-06-22): do NOT hand-supply `ts` — OMIT it from the
   composed dict and let `dream-log-append` stamp from its own canonical-Chicago
   clock (the 6/20 hardening).** A hand-stamped `ts` is the TZ-confusion failure
   surface: the tool clamps a FUTURE ts and discards a NAIVE ts, but a
   tz-aware-yet-wrong PAST ts (e.g. the PDT wall-clock hour pinned with the
   Central `-05:00` offset → 2h stale) passes ALL guards untouched and makes
   `dream-recent-clean` read the fresh cycle as stale → false re-fire (confirmed
   2026-06-22: wrote `01:18-05:00` for a `03:18` Chicago cycle).
   **STRUCTURALLY ENFORCED (2026-06-24, dream-log-append layer 6): a caller-supplied
   `ts` is now IGNORED by default — the tool drops it (preserving it in
   `ts_caller_supplied_ignored`) and self-stamps its own canonical-Chicago clock.**
   This converts the prose discipline above into a gate: the entire
   journal_timezone_confusion-via-dream-log class (incl. the layer-5 compose-early
   residual) can no longer land a wrong ts. The "OMIT ts" rule still holds as the
   clean habit, but a slip is now caught structurally rather than relied on.
   If you genuinely need a backfill ts (recovery for an earlier-cut cycle), set
   `DREAM_LOG_TS_RECOVERY=1` to opt the caller ts back in (it then flows through
   the layer-3/4/5 normalization guards); derive it from `jnow --iso`, never a
   hand-typed hour.

   Entry shape (canonical `actions` keys are exactly these; empty arrays may
   be dropped; OMIT `ts` per the discipline above):
   ```json
   {"session_id":"...","stats_snapshot":{...},
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
