---
name: mvm-dream
description: "MVM offline integration. Reads mvm stats + recall-log; resolves every detected anomaly within the same cycle (no sinks). Coverage → proactive ingest. Quality failure → cross-check → re-ingest if confirmed. Staleness → cross-check → supersede if confirmed. Contested/gap → user escalation. Triggers: '/mvm-dream', 'mvm dream', 'mvm consolidate', 'mvm maintenance'."
---

# /mvm-dream

Selection rule for quality + staleness: **oldest mtime first**, never random. v0.1 will refine to `priority = age × log(retrievals + 1) × domain_velocity`.

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

3. **Quality — re-verify + repair** (oldest 5 canonicals):
   For each, pick a random test. Spawn injected-mode cold-clone (haiku, ANSWER/RATIONALE format). Grade.
   - **PASS** → done.
   - **FAIL** → spawn second cold-clone immediately for cross-check.
     - **Both fail** → spawn `/mvm-ingest` on the doc's original `source:` URL (overwriting re-ingest).
     - **Second passes** → record as transient in dream-log; no action.

4. **Staleness — spot-check + supersede** (oldest 3 not in step 3):
   For each, take the first test's `q`, run KB-clone (Read doc) + web-clone (WebSearch) in parallel.
   - **Agree** → done.
   - **Disagree, web has fresher-source markers** → run second web probe with different phrasing.
     - **Second web probe confirms** → spawn `/mvm-ingest` on fresh URL; mark old canonical `status: superseded` in frontmatter; add `superseded_by:` pointer.
     - **Second web probe disconfirms** → record as transient; no action.

5. **Contested + gap follow-up + cross-contradiction sweep** (from `recall-log.jsonl` + KB):
   - Entries with `decided_source: "contested"` since last dream → spawn 5 web probes with paraphrased queries; if convergence emerges, ingest the consensus; if still split, escalate to user.
   - Entries with `decided_source: "none"` (hard misses) → propose to user as curriculum items.
   - **Cross-contradiction sweep (5 vector-near pairs):** Bash `mvm search "<random topic-keyword from a recent canonical>" --top-k 3` → if top-2 results have similarity > 0.7 to each other, spawn 2 Agent cold-clones asking each doc the same probing question; if answers contradict → flag both with `status: cross-contested` and surface to user.

6. **Log cycle** (one line) to `~/mvm/state/dream-log.jsonl`:
   ```json
   {"ts":"...", "session_id":"...", "stats_snapshot":{...},
    "meta_audit":{"chronic_failures":["path"], "stuck_topics":["topic"],
                  "noisy_threshold":bool, "permanently_contested":["q"]},
    "actions":{"coverage_ingests":["url1"], "quality_repairs":["path1"],
               "staleness_supersedes":["path2 -> path2-new"],
               "contested_resolved":["q1"], "gaps_surfaced":["q2"],
               "transients":[{"kind":"quality","path":"..."}, ...]},
    "duration_ms":N}
   ```

   **JSON-validate before append** (added 2026-05-14 per auditor #134 D134-V6-1 — `"count_7d":~11` human-prose tilde leaked into a numeric field, breaking `json.loads()` on the appended line). Two-line discipline:
   1. **Compose the entry as a Python dict and `json.dumps(d, separators=(',', ':'))`** so prose can't slip past — numbers stay numbers, NaN/Infinity get caught at dump time. Equivalent: hand-author the line then `python3 -c 'import json,sys; json.loads(sys.argv[1])' "$LINE"` BEFORE the `>> dream-log.jsonl` append.
   2. **Never write `~N`, `approx N`, `est N`, `~$N`, etc. inside a JSON numeric field.** Approximations belong in string fields with explicit prefix (`"count_7d_approx":"~11"`) or as a separate `notes` field. A tilde inside a number-typed slot is always a bug.

   If validation fails: fix the entry in-context, re-validate, then append. Do NOT append a broken line "and fix it later" — programmatic consumers (auditor, dream-pass-coverage) skip the entire malformed entry and silently report "no recent dream pass," masking the work that was done.

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
