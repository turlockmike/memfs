---
name: mvm-dream
description: "MVM offline integration. Reads mvm stats + recall-log; resolves every detected anomaly within the same cycle (no sinks). Coverage → proactive ingest. Quality failure → cross-check → re-ingest if confirmed. Staleness → cross-check → supersede if confirmed. Contested/gap → user escalation. Triggers: '/mvm-dream', 'mvm dream', 'mvm consolidate', 'mvm maintenance'."
---

# /mvm-dream

Selection rule for quality + staleness: **least-recently-VERIFIED first** via `dream-verify-pick` (shipped 2026-06-06 dream-cycle), NOT raw oldest-mtime. WHY the change: a PASS never touches a file's mtime, so "oldest mtime first" re-probed the SAME ~8 oldest canonicals every full pass forever while ~29 of 37 test-bearing docs were NEVER quality-verified (coverage blind spot). `dream-verify-pick pick <N> [exclude...]` reads ledger `~/mvm/state/quality-verified.jsonl` and returns the N least-recently-verified canonicals (never-verified rank epoch-0, tiebreak oldest-mtime — so it degrades to the old behavior on a cold ledger). After each probe, stamp it: `dream-verify-pick record <doc> <PASS|FAIL> <quality|staleness>` (this is what rotates the next cycle to fresh docs). v0.1 will refine to `priority = age × log(retrievals + 1) × domain_velocity`.

**FEP rule: every detected anomaly resolves in this cycle.** No anomaly leaves dream as a flat log entry — it either *dissolves* (cross-check disconfirms) or *drives a substrate change* (cross-check confirms → ingest/supersede). Per-cycle cap: 3 substrate-changing ingestions.

## Steps

0. **Meta-audit (self-audit of dream-log).** Read the last 10 entries from `~/mvm/state/dream-log.jsonl`. Detect patterns:
   - Same canonical path appears in `quality_repairs` ≥ 3 cycles → source URL is bad. Edit the canonical's frontmatter to add `status: chronic_failure`; surface to user with the path and history.
   - Same topic appears in `coverage_ingests` ≥ 3 cycles AND fallback rate not dropping → ingestion isn't sticking; escalate to user (suggest manual ingest of authoritative source).
   - `transients` outpacing `quality_repairs` over the window → cross-check disconfirming too often; surface to user (cold-clone may be noisy or threshold may be wrong).
   - `duration_ms` trending up monotonically → schedule a consolidation pass (v0.1) and surface.
   - Same question appears in `contested_resolved: still_split` ≥ 2 cycles → genuinely controversial; escalate permanently to user, stop re-attempting.
   Every pattern either drives a meta-action or escalation. Nothing flat-logs.

1. **Read dashboard** — Bash `mvm stats --window 7d --json`. Capture top fallback topics, source mix.

2. **Coverage — proactive ingest** (max 3 per cycle):
   For each top-fallback topic, find the most-frequent web URL in `recall-log.jsonl`. Spawn `/mvm-ingest <URL>` in background.

   **RECALL-LOG APPEND (mandatory, steps 3+4) — the `mvm-(kb|web)-clone` spawns below trip the fail-closed Stop hook `stop-recall-log-enforce.sh`, which BLOCKS at session end if the ledger got zero appends (it can't tell dream-QA clones from recall-skill probes; ingest is exempted by using naked-only clones, dream cannot — it needs kb/web).** Each verification probe IS a genuine kb/web retrieval with a decided answer, so log one line PER probe as you grade it: `echo '{"question":"<test q>","topic_hint":"dream-quality-verify|dream-staleness-verify","decided_source":"kb","decided_answer":"<answer> — <doc> PASS/FAIL"}' | recall-log add`. Use **`decided_source":"kb"`** even for staleness web-corroboration (the KB canonical is the trust-hierarchy winner when it holds) so these don't register as false `web`/`none` fallbacks that spuriously trigger next cycle's coverage ingest. ts/session_id auto-fill.

3. **Quality — re-verify + repair** (5 least-recently-verified canonicals: `dream-verify-pick pick 5`):
   For each, pick a random test. Spawn injected-mode cold-clone (haiku, ANSWER/RATIONALE format). Grade. **Log the probe to recall-log (see RECALL-LOG APPEND above) AND stamp the rotation ledger: `dream-verify-pick record <doc> <PASS|FAIL> quality`.**
   - **PASS** → done.
   - **FAIL** → spawn second cold-clone immediately for cross-check.
     - **Both fail** → spawn `/mvm-ingest` on the doc's original `source:` URL (overwriting re-ingest).
     - **Second passes** → record as transient in dream-log; no action.

4. **Staleness — spot-check + supersede** (3 least-recently-verified not in step 3: `dream-verify-pick pick 3 <the 5 step-3 docs...>`; stamp each after via `dream-verify-pick record <doc> PASS staleness`):
   **Typed-edge skip-guard (mechanical, runs FIRST per candidate):** Bash `mvm relations <doc> --rel superseded_by --json`. If it returns a non-empty `out`-direction edge, the doc is ALREADY superseded → SKIP it (don't burn web probes re-validating a known-dead canonical; pick the next-oldest instead). This is the typed-edge CONSUMER replacing a prose re-read.
   For each surviving candidate, take the first test's `q`, run KB-clone (Read doc) + web-clone (WebSearch) in parallel. **Log the probe to recall-log (see RECALL-LOG APPEND above).**
   - **Agree** → done.
   - **Disagree, web has fresher-source markers** → run second web probe with different phrasing.
     - **Second web probe confirms** → spawn `/mvm-ingest` on fresh URL; mark old canonical `status: superseded` in frontmatter; add `superseded_by: resources/...` pointer (knowledge-root-relative, NOT `~/` — index.py normalizes either, but write root-relative). **Then `mvm index` and VERIFY the edge resolved: `mvm relations <old-doc> --rel superseded_by` MUST list the new canonical. Empty output = the pointer dangled (Gap-0 regression) → fix the path and re-index before moving on.**
     - **Second web probe disconfirms** → record as transient; no action.

5. **Contested + gap follow-up + cross-contradiction sweep** (from `recall-log.jsonl` + KB):
   - Entries with `decided_source: "contested"` since last dream → spawn 5 web probes with paraphrased queries; if convergence emerges, ingest the consensus; if still split, escalate to user.
   - Entries with `decided_source: "none"` (hard misses) → propose to user as curriculum items.
   - **Cross-contradiction sweep (typed-edge consumer FIRST, then 5 vector-near pairs):**
     - **(a) Consume known tensions:** for each doc touched in steps 3–4, Bash `mvm relations <doc> --rel in_tension_with --json`. Any non-empty result is a previously-recorded contradiction pair — re-probe THAT pair first (one cold-clone each); if they now AGREE, the tension resolved → remove the `in_tension_with:` frontmatter from both + re-index (don't leave a stale tension edge); if still contradict, leave it (durable record holds).
     - **(b) Discover new tensions:** Bash `mvm search "<random topic-keyword from a recent canonical>" --top-k 3` → if top-2 results have similarity > 0.7, spawn 2 Agent cold-clones asking each doc the same probing question; if answers contradict → flag both with `status: cross-contested`, **AND add a reciprocal `in_tension_with: resources/<other-doc>` frontmatter edge to BOTH docs (root-relative), then `mvm index`** so the contradiction becomes a queryable typed edge (consumed by (a) next cycle — closes the no-sinks producer→consumer loop), and surface to user.

6. **Log cycle** (one line) to `~/mvm/state/dream-log.jsonl`:
   ```json
   {"ts":"...", "session_id":"...", "stats_snapshot":{...},
    "meta_audit":{"chronic_failures":["path"], "stuck_topics":["topic"],
                  "noisy_threshold":bool, "permanently_contested":["q"]},
    "actions":{"coverage_ingests":["url1"], "coverage_edits":["path0"],
               "quality_repairs":["path1"], "quality_verifies":[{"doc":"...","note":"..."}],
               "staleness_supersedes":["path2 -> path2-new"],
               "contested_resolved":["q1"], "gaps_surfaced":["q2"],
               "transients":[{"kind":"quality","path":"..."}, ...],
               "substrate_fixes":["substrate/tooling fix shipped this cycle"]},
    "step_results":{...},
    "phase_2_meta_review":{
       "predictions_accuracy":{"new_predictions":N,"evaluable":N,"score":"...","resolution":"..."},
       "mistakes_2plus_30d_assessment":"INLINE per-root-class FIX-C string — see MANDATE below",
       "s4_initiatives":{"total":N,"unratified":["..."],"past_expiry_dropped":["..."],"flagged_for_attention":["..."],"notes":"..."},
       // S4 PATH PIN (added 2026-06-04 dream cycle 30; entry-12 recommended this, never shipped → path re-confused in entries 8 & 11). The s4-initiatives ledger lives at ~/.local/state/alfred/areas/s4-initiatives/ (the .md files + index.md + archived/). The path ~/areas/s4-initiatives/ holds ONLY auto-generated INDEX.md/index.md stubs — reading it reports a false "0 active". Always read the .local/state path.
       "session_capsule_check":{"exists":bool,"action":"skip|compress"}},
    "escalations":[{"severity":"...","kind":"...","finding":"..."}],
    "duration_ms":N}
   ```

   **`phase_2_meta_review` SCHEMA MANDATE (added 2026-05-17 per auditor #147 D147-META-1 — OPERATIONAL; #148 ANDON-armed on recurrence):**
   - `phase_2_meta_review` MUST be a **structured dict**, key spelled with the underscore (`phase_2_meta_review`, NOT `phase2_meta_review` — the latter was itself a 2026-05-17 entry-10 drift; the V6 grader + auditor tooling key off the underscore form, matching entries 2026-05-11→05-16).
   - `mistakes_2plus_30d_assessment` MUST be an **inline string** carrying the full FIX-C per-root-class 2+/30d root-cause-clustered assessment (the assessment the `alfred-dream-trigger.sh` PHASE 2 "Mistake patterns" bullet mandates). It is **NEVER a pointer** — `"see escalations[]"`, `"see journal Session-NNN"`, `"see above"`, or any reference-to-elsewhere is **BANNED**. The dream-log entry must be **self-contained**: an auditor reading ONLY this JSON line must find the complete 2+/30d clustering with no dereference. Root-cause of the ban: entry-10 (dream-20260517-0100) collapsed this field to the bare string `"see escalations[] and journal Session-152 entry"`; the journal write landed but raced the auditor's snapshot ⇒ the assessment appeared to dangle. A string pointer can dangle; an inline string cannot. (`escalations[]` is still a valid sibling field for the substantive permanent-escalation items — it is NOT a substitute for the inline assessment.)
   - `step_results`, `escalations`, `predictions_accuracy`, `s4_initiatives`, `session_capsule_check` are all retained every cycle (entry-10 dropped `step_results` — D142-V6-OBS cosmetic; restored here so the schema can't silently shrink).
   - **`actions` CANONICAL KEY SET (added 2026-05-29 per auditor #173 D173-V6-1 — 3 renames of the substrate-fix field in 4 cycles: `substrate_fixes_shipped` 5/26 → `substrate_fixes` 5/27,5/28 → `structural_fixes` 5/29):** every key under `actions` MUST be one of exactly `{coverage_ingests, coverage_edits, quality_repairs, quality_verifies, staleness_supersedes, contested_resolved, gaps_surfaced, transients, substrate_fixes}`. The substrate/tooling-fix field is `substrate_fixes` — **NOT** `structural_fixes`, `substrate_fixes_shipped`, or any synonym. Empty arrays may be dropped (schema minimization is fine); inventing a NEW key name is the banned drift. This set is the SINGLE pin shared with `test-auditor-lockbox-migration.sh` (`CANONICAL_ACTION_KEYS`, key `V6:dream_log`) and the auditor role-file V6 descriptor — **changing it requires editing all three in the same commit (co-maintenance contract).**
   - Self-check before append (now also asserts the canonical actions key set so a rename can't escape): `python3 -c "import json,sys; e=json.loads(sys.argv[1]); p=e['phase_2_meta_review']; a=p['mistakes_2plus_30d_assessment']; CANON={'coverage_ingests','coverage_edits','quality_repairs','quality_verifies','staleness_supersedes','contested_resolved','gaps_surfaced','transients','substrate_fixes'}; bad=set(e.get('actions',{}))-CANON; assert not bad, f'non-canonical actions key(s): {bad} — substrate-fix field is substrate_fixes'; assert isinstance(p,dict), 'phase_2_meta_review must be dict'; assert isinstance(a,str) and not any(s in a.lower() for s in ['see escalations','see journal','see above','see the ']), 'mistakes_2plus_30d_assessment must be inline, not a pointer'" "$LINE"` — if this assertion fails, the entry is non-compliant; fix the key / inline the assessment and re-validate BEFORE the append.

   **JSON-validate before append** (added 2026-05-14 per auditor #134 D134-V6-1 — `"count_7d":~11` human-prose tilde leaked into a numeric field, breaking `json.loads()` on the appended line). Two-line discipline:
   1. **Compose the entry as a Python dict and `json.dumps(d, separators=(',', ':'))`** so prose can't slip past — numbers stay numbers, NaN/Infinity get caught at dump time. Equivalent: hand-author the line then `python3 -c 'import json,sys; json.loads(sys.argv[1])' "$LINE"` BEFORE the append.
   1b. **APPEND VIA `dream-log-append "$LINE"`, NEVER bare `>> dream-log.jsonl`** (shipped 2026-06-07 dream cycle). WHY: a bare `>>` fuses the new entry onto a prior line that lacked a trailing newline — observed 2026-06-07 when the 10:47 reduced entry was written unterminated, so the next append produced one 14KB concatenated line that `json.loads()` rejected, masking the ENTIRE entry from every consumer (auditor, dream-pass-coverage). `dream-log-append` (a) raw_decode-validates the line is exactly one JSON object (rejects concatenation/trailing junk, non-zero exit), and (b) ensures the file ends in `\n` before appending. This is the durable fix for the recurring concatenation class (prior occurrence: entry-4 split-fix).
   2. **Never write `~N`, `approx N`, `est N`, `~$N`, etc. inside a JSON numeric field.** Approximations belong in string fields with explicit prefix (`"count_7d_approx":"~11"`) or as a separate `notes` field. A tilde inside a number-typed slot is always a bug.

   If validation fails: fix the entry in-context, re-validate, then append. Do NOT append a broken line "and fix it later" — programmatic consumers (auditor, dream-pass-coverage) skip the entire malformed entry and silently report "no recent dream pass," masking the work that was done.

   **Hook-ship preserve gate (added 2026-05-18 dream-cycle #11; generalized to all-worker-type 2026-05-18 per auditor #153 D153-V2-1).** If THIS cycle shipped a NEW hook (PreToolUse/Stop/PostToolUse/UserPromptSubmit), then BEFORE composing+appending the dream-log entry you MUST: (a) declare it in `~/.local/state/alfred/hooks-preserve.json` (`entries[]`: `{event, matcher, command}`, leading `bash ` stripped), and (b) run `~/.local/bin/hooks-preserve-check` and confirm `PASS` / no drift. A FAIL here blocks the cycle-log append exactly like a JSON-validation FAIL. **This dream-cycle-scoped gate is now the dream-path instance of the GENERIC discipline** — see `alfred.md` Pre-Ship Gate **#7** (the declare-step for ANY hook ship from ANY worker type) and its executable backstop, the universal Stop hook `~/.claude/hooks/stop-hooks-preserve-check.sh` (catches ship-and-forget at the next session boundary regardless of worker type, since the original scoping-to-this-skill is exactly what let the class recur 3× — 2026-05-12 tg-gate · 2026-05-16 recall-gate · 2026-05-18 recovery-gate). Keep this gate here as the first-time-right path for dream-cycle ships; the Stop hook + Pre-Ship Gate #7 cover the heartbeat/interjection/other paths this skill never sees.

7. **Report** to user (concise):
   ```
   Dream cycle complete.
   Meta:       N chronic-failure flags, M stuck topics  [if any]
   Coverage:   N ingests
   Quality:    N repaired, M transient
   Staleness:  N superseded, M transient
   Contested:  N resolved, M still need user input
   Gaps:       N surfaced for curriculum
   Escalations:  <list of items needing your attention>
   ```
