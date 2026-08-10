---
name: mvm-dream
description: "Maintain and consolidate the knowledge base — the offline integration pass. Reads memory stats + the recall log, finds gaps / staleness / quality problems, and resolves each within the cycle: proactively ingesting missing knowledge, cross-checking and superseding stale facts, escalating contested ones. This is how the memory keeps itself honest and current across sessions. Triggers: '/mvm-dream', 'consolidate memory', 'memory maintenance', 'audit my knowledge' (also good on a cron)."
---

# /mvm-dream

The offline pass that keeps the loop closed. Governing rule (free-energy
framing): **every detected anomaly resolves in this cycle** — it dissolves
(cross-check disconfirms), drives a substrate change (ingest / supersede /
annotate), or escalates to the user. No anomaly accumulates indefinitely.
Per-cycle cap: 3 substrate-changing ingestions.

Core budget principle: **enumeration is deterministic, judgment is not —
spend model tokens only on judgment.** `mvm sweep` finds the worklist in
seconds at zero tokens; the model decides what each row means.

## Steps

0. **Load the worklist — discovery is already done, don't redo it.**
   ```bash
   mvm sweep --check-fresh || mvm sweep    # regenerate only if stale (~seconds)
   mvm sweep --json                        # or read <state>/mvm-sweep/worklist.json
   ```
   Route the rows — each check has one owning step:
   | worklist check | feeds | the judgment call |
   |---|---|---|
   | `review_due` | step 3 | is the canonical still true? supersede / re-date / retire |
   | `cold_decayed` | step 3 | dead weight or seasonal? decay ≠ delete — prefer archive/merge |
   | `untested_hot` | step 4 | any doc grounding ≥2 real recalls must acquire locked tests |
   | `broken_links` | step 4 | repair the target, or delete the link if the referent is gone |
   | `index_drift` | step 4 | structural, usually mechanical |

   `never_retrieved` is a bounded COUNT, not a worklist — do not author tests
   against docs that have never grounded a recall; it is cost with no
   retrieval on the other side. Verification budget follows retrieval heat.

1. **Dashboard.** `mvm stats --window 7d --json` — top fallback topics,
   source mix, recall volume.

2. **Coverage — proactive ingest** (max 3/cycle). For each top fallback
   topic (questions the KB kept failing to answer), find the most-frequent
   web URL among its recall-log entries and spawn `/mvm-ingest <URL>` in
   background. Two gates before ingesting:
   - **Stale-ledger pre-check:** the ledger is append-only, so a fallback may
     predate a doc that has since shipped — `mvm search` the topic first; if
     a doc already answers it, skip (double-ingest is a regression).
   - **Query-predictability gate:** consolidate topics with stable, recurring
     question shapes (curated domains, repeat hits). A one-off novel topic is
     answer-live territory — the ingest cost never amortizes. Skip it.

3. **Quality — re-verify + repair.** Pick ~5 docs, least-recently-verified
   first. Per doc:
   ```bash
   mvm verify <doc> --test-id <one random id> --json   # single test, bounded cost
   ```
   (Never bare `mvm verify <doc>` in this loop — full suites on large docs
   time out, and a timeout judges nothing.)
   - PASS → done; record it so rotation moves on next cycle.
   - FAIL with an `error` field / empty output → the instrument failed, not
     the doc. Tag it `probe-noise` and never count it as a content defect.
   - Real FAIL → cross-check with ONE orchestrator-graded cold-clone (inject
     the doc, grade lenient-on-phrasing / strict-on-facts). Both fail →
     re-ingest from the doc's `source:` URL. Cross-check passes → tag
     `grader-strict` (the two-tier filter working as designed), no action.

   The three transient tags mean OPPOSITE things — `probe-noise` =
   infrastructure failing, `grader-strict` = designed strictness filter
   working, `staleness-disconfirm` = healthy skepticism. Tag at mint time;
   an untagged bucket makes the meta-audit (step 6) fire on corpus health.

4. **Staleness — spot-check + supersede.** Pick ~3 old docs. Per doc:
   - Skip-guard: `mvm relations <doc> --rel superseded_by --json` — already
     superseded → next.
   - Take the doc's first test `q` → spawn `mvm-kb-clone` (doc) and
     `mvm-web-clone` (open web) in parallel.
   - Agree → done. Disagree with fresher-source web markers → one more web
     probe, different phrasing. Confirmed → `/mvm-ingest` the fresh URL, mark
     the old canonical `status: superseded` + `superseded_by: <relpath>`,
     `mvm index`, then **verify the edge resolves** (`mvm relations <old-doc>
     --rel superseded_by` must list the new doc — empty = dangling pointer).
     Disconfirmed → tag `staleness-disconfirm`.
   - A web-clone "NOT-FOUND" that cites blocking/403 is a fact about the
     CLONE's reach, never about the world — retry the URL yourself with
     better fetch tooling before logging anything.

5. **Contested + tension discovery.**
   - Questions logged `contested` since last cycle → several paraphrased web
     probes; convergence → ingest the consensus; still split after 2 cycles →
     permanently contested; escalate to the user, stop re-attempting.
   - Hard misses (`decided_source: none`) → propose as curriculum items.
   - **Test-coverage debt:** `mvm heat --untested --top 10` — author locked
     tests for the single hottest untested doc (ingest-style, injected-verify
     before commit). One per cycle.
   - **Cross-contradiction sweep:** `mvm search "<recent-canonical keyword>"
     --top-k 3 --json`; consider probing results #1 and #2 as a pair. Gate
     BEFORE spending clones — all must hold:
     1. same `kind` (a recipe and a decision answer different questions);
     2. same declared version, or both version-less (a v1 doc and a v2 doc
        disagreeing is correct versioning, not tension) — same for dated
        siblings (`2026-05-13.md` vs `2026-05-19.md`) and enumerated series
        (`part-01` vs `part-02`): a differing scope EXPLAINS a disagreement;
     3. ≥2 shared subject terms in the titles AFTER dropping the domain
        name, version markers, and corpus boilerplate — or the shared terms
        covering one doc's entire subject. If the DIFFERING terms name the
        subject itself (`boots` vs `wand`), they're family siblings answering
        different questions — reject, don't spend clones;
     4. neither doc is an aggregator (index files, backlogs, dashboards,
        >40KB grab-bags) — aggregators pair with everything and assert
        nothing.
     Pair passes → ask both docs the same probing question via 2 cold-clones.
     Contradiction → flag both `status: cross-contested` + reciprocal
     `in_tension_with:` edges, `mvm index`, surface to the user. Also
     re-probe any existing `in_tension_with` pairs on docs touched this
     cycle; agreement → remove the edge.

6. **Log + meta-audit.** Append the cycle to `~/mvm/state/dream-log.jsonl`:
   ```json
   {"ts":"<ISO8601>","coverage_ingests":[],"quality_repairs":[],
    "staleness_supersedes":[],"transients":["probe-noise: ..."],
    "escalations":[],"duration_ms":N}
   ```
   Then read the last few cycles and act on trends — every pattern drives a
   meta-action or escalation, nothing flat-logs:
   - same doc in `quality_repairs` ≥3 cycles → mark `status: chronic_failure`,
     surface with history;
   - same topic in `coverage_ingests` ≥3 cycles with a flat fallback rate →
     the ingests aren't taking; suggest a manual authoritative source;
   - `probe-noise` outpacing `quality_repairs` AND still occurring in the
     most recent cycles → infrastructure degrading, surface. (The recency
     half is load-bearing: without it the alarm keeps firing on a defect
     that was already fixed, and an alarm that can only be cleared by
     waiting is a constant — a constant carries no information.);
   - `duration_ms` monotonically rising → the corpus outgrew the cycle
     budget; consolidate.
