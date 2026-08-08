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
**A doc that is correctly NOT verifiable** (a deliberate RETIRED tombstone, e.g.
`resources/kalshi.md`) gets `dream-verify-pick skip <doc> --reason "<why>"`
(added 2026-07-27). It advances rotation and records `result=SKIP, verified=false`
plus the reason, because rotation used to advance only on `record` — so the
honest action (don't verify a tombstone) and the ledger-advancing action
(record a PASS) were in conflict, and the doc was re-picked top-of-list forever.
A SKIP is **never** a pass, and the reason is mandatory (≥12 chars, refused
otherwise) so it cannot become a laundering path for "didn't feel like it".

**Probe logging — USE THE ATOMIC SUBCOMMAND, do NOT hand-chain the two calls
(structural remedy shipped 2026-08-08, `dream-verify-pick selftest` 50/50):**
```bash
dream-verify-pick probe <doc> <PASS|FAIL> <quality|staleness> \
  --q "<the test question>" --a "<the graded answer>" [--source kb|web]
```
One call. It appends the recall-log row FIRST and stamps the rotation ledger
SECOND, so the ordering is code rather than something you re-remember each
cycle. It refuses an empty `--q`/`--a`, a step naming neither axis, and a
`--source` outside the `kb|web` enum — a probe with no evidence in it can never
be laundered into a well-formed-looking row.
⛔ **The old two-call shell chain is the record-before-probe failure and it fired
SIX times** — 2026-07-25, 07-27, 07-28, 07-29, 08-08 00:50, and 08-08 03:02 — in
this same rung, every time caught by `_probe_evidence_exists` (so no false PASS
ever landed; the ORACLE was sound the whole time) and every time "fixed" with
more prose. **A gate that fires every single cycle is not a gate working — it is
a missing affordance being re-detected.** Prose plateaued; the subcommand is the
fix. Hand-chaining `recall-log add && dream-verify-pick record` still works and
is not forbidden, but it re-opens the exact degree of freedom that failed six
times, so reach for it only when the probe genuinely has no doc (step-5 clone
probes below, which log via `recall-log add` directly).

(`topic_hint` dream-quality-verify | dream-staleness-verify; the CLI infers
`kind:"dream-probe"` from it, so probes never pollute `mvm stats` — which
defaults to `--kind recall`. Use the honest enum source: `kb` for doc-grounded
probes, `web` for web-grounded ones.)

## Steps

0. **Meta-audit** — FIRST run `dream-cycle-begin` (stamps the cycle-start epoch
   so the final `dream-log-append` self-stamps a real `duration_ms`, reviving the
   monotonic-rising branch below — auditor #48). Then **run `dream-meta-audit`**
   (rc=0 no rule fires · rc=1 ≥1 finding needs judgment · rc=2 instrument
   failure, never read as all-clear). It owns the ENUMERATION of every rule
   below — tag counts, chronic docs, stuck topics, contested questions, duration
   trend — so only the dispositions cost model tokens. Same split as `mvm sweep`
   (0.5), `dream-coverage-triage` (2), and `dream-pending-carry` (6). ⛔ Do NOT
   hand-roll inline Python over `~/mvm/state/dream-log.jsonl` to re-derive these
   counts; that is what the tool replaced (every cycle was writing its own
   parser). Read the log directly only to judge a finding the tool surfaced.
   - Same canonical in `quality_repairs` ≥3 cycles → mark frontmatter
     `status: chronic_failure`; surface to user with history.
   - Same topic in `coverage_ingests` ≥3 cycles, fallback rate flat →
     escalate (suggest manual ingest of an authoritative source).
   - **`probe-noise` transients** outpacing `quality_repairs` over the window
     **AND still occurring in the most recent `--recent-k` (default 3) cycles** →
     the retriever/CLI is failing too often; surface (infrastructure degrading).
     ⚠ **THE RULE IS CONJUNCTIVE, and the recency half is load-bearing
     (2026-07-28, measured).** Without it the rule fires on a HEALED defect for
     a full window after the repair lands: this cycle counted **7 probe-noise vs
     1 quality_repair** — rule fires — but all 7 are dated 2026-07-26 and **ZERO**
     landed in the five 2026-07-27 cycles, because the causes were fixed
     (mvm@672857b `web` tier-ladder repair, mvm@82098dd killed-probe self-report,
     the timeout-900 correction). **An alarm that cannot be cleared by fixing the
     thing it alarms about can only be cleared by waiting — so it is a CONSTANT,
     and a constant carries no information.** Same class as gate #20 (a threshold
     no real input can cross) and the 2026-07-27 escalation-severity finding (a
     `high` on an externally-blocked item pins the lane dirty until an outsider
     acts). `dream-meta-audit` reports `cycles_since_last_probe_noise` and prints
     the explicit *"outpacing but NOT recent → the cause was fixed and the
     evidence is aging out"* verdict; trust that line rather than re-deriving.
     ⚠ **Count the CAUSE TAG, never the raw `transients` bucket** (2026-07-25):
     `transients` holds three causes with OPPOSITE meanings, and only
     `probe-noise:` is infrastructure signal. `grader-strict:` entries are the
     designed strict-engine→lenient-cross-check filter WORKING, and on a healthy
     corpus they NATURALLY exceed `quality_repairs` (few real defects, some
     strict-tier false positives); `staleness-disconfirm:` is healthy skepticism.
     Counting the bucket raw makes this rule fire on corpus HEALTH — it did on
     2026-07-25 (5 transients vs 3 repairs → discriminated: 4 grader-strict + 1
     timeout ⇒ healthy, no escalation). Tags are now **enforced at the write gate**
     (`dream-log-append` rc=2 on an untagged transient), so this count is
     deterministic — and `dream-meta-audit` now performs it, reporting all four
     buckets (`probe-noise` / `grader-strict` / `staleness-disconfirm` /
     `UNTAGGED`) separately. It **never** folds `UNTAGGED` into `probe-noise`
     (a mutant that does fails 3 selftest cases by name), because that would
     re-create the exact bucket-vs-tag conflation this paragraph exists to kill.
   - `duration_ms` monotonically rising → schedule consolidation; surface.
   - Same question `still_split` ≥2 cycles → permanently contested; escalate,
     stop re-attempting.
   Every pattern drives a meta-action or escalation; nothing flat-logs.

0.5 **Load the sweep worklist — DISCOVERY IS ALREADY DONE, DO NOT REDO IT**
   (hermes-parity build #4, 2026-07-22; `~/projects/hermes-parity/PLAN.md:45-51`).

   ```
   mvm sweep --check-fresh || mvm sweep      # regenerate only if stale (~4s, 0 tokens)
   mvm sweep --json                          # or read ~/.local/state/alfred/mvm-sweep/worklist.json
   ```

   **The split this enforces, and why it is the whole point of the build:** finding
   *which* docs are stale, dead, broken-linked, or past `review_at` is a
   **deterministic file-scan** — ~4,100 docs in ~4 s at **zero model tokens**. Judging
   what to DO about a flagged doc is the part that needs a model. Before this step
   existed, the dream pass spent Claude tokens rediscovering the first half every
   cycle. **Spend tokens on the judgment calls, never on the enumeration.**

   The `mvm-sweep-producer` cron (`20 */6`) runs 10 minutes ahead of this pass, so
   the worklist is normally ≤10 min old and the `--check-fresh` line is a no-op guard,
   not a regeneration. It regenerates only when the producer genuinely missed.

   **Route the rows — each check has ONE owning step, so nothing is worked twice:**
   | worklist check | feeds | what the model actually decides |
   |---|---|---|
   | `review_due` (+ `unparseable`) | step 4 | is the canonical still true? supersede / re-date / retire. **`unparseable` rows are a `review_at` field that is prose, not a date — fix the field, and do not count it as reviewed.** |
   | `cold_decayed` | step 4 | zero retrieves in 30 d with **≥1 lifetime** recall: genuinely dead weight, or seasonal (a lane that will wake)? **Decay ≠ delete** — prefer archive/merge; a doc that grounded real recalls once has earned a reason before removal. ⚠ **This cell read "≥2 lifetime" until 2026-07-27 and that was WRONG — it borrowed `untested_hot`'s `HOT_MIN_RECALLS`, which belongs to that check and nothing else.** `check_cold_decayed` applies NO floor above 1 (sweep.py: DECAYED = "had retrieval heat"). Measured when caught: 76 rows, **23** at ≥2 and **53** at exactly 1 — so four consecutive dream entries that wrote "N docs (≥2 lifetime recalls)" overstated that population ~3.3×, by transcribing this cell instead of the data. The tool now **publishes** `lifetime_recall_floor` + `rows_by_lifetime_recalls` + `rows_at_or_above_hot_threshold`; **quote those fields, never a floor you inferred from prose.** |
   | `untested_hot` | step 3 | any doc crossing the ≥2-recall threshold must acquire locked tests **before its next dream pass** — this is the live maintenance oracle in `PLAN.md:51`. Currently **0**; keeping it at 0 IS the success condition. |
   | `broken_links` | step 3 | repair the target, or delete the link if the referent is genuinely gone. |
   | `index_drift` | step 5 | `INDEX.md`/`index.md` collisions — structural, usually mechanical. |

   ⛔ **`never_retrieved` is a bounded COUNT, not a worklist** — ~3,879 docs have never
   grounded a single recall. Do **NOT** author tests, verify, or repair against that
   set: it is cost with no retrieval on the other side. The build's original "3.6%
   coverage gap" clause was **RETIRED on exactly this reasoning** (`PLAN.md:49,51`) —
   do not resurrect it from an older cached reading of the plan.

   ⚠ **If `--check-fresh` fails twice in a row, that is a PRODUCER outage, not a
   worklist problem** — say so in the cycle entry rather than quietly regenerating
   forever; the regeneration masks a dead cron, which is the exact class audit #110
   was about.

1. **Dashboard** — `mvm stats --window 7d --json` (defaults to real recalls).
   Capture top fallback topics, source mix.

2. **Coverage — proactive ingest** (max 3/cycle): for each top-fallback topic,
   find the most-frequent web URL among `kind=recall` ledger entries; spawn
   `/mvm-ingest <URL>` in background.
   **RUN `dream-coverage-triage` FIRST — it decides which topics even reach the
   two gates below** (shipped 2026-07-25; selftest 15/15). rc=0 = every fallback
   topic was already adjudicated on THIS SAME evidence ⇒ **step 2 is done, spend
   zero judgment**; rc=1 = at least one topic NEEDS-JUDGMENT ⇒ apply the gates
   below to those topics only; rc=2 = instrument failure (never read as
   all-clear). After ruling on a NEW topic, persist it:
   `dream-coverage-triage record <topic> <skip|ingest> --reason R --doc <covering-doc>`.
   **Why it exists (measured):** `kalshi/earning-lane/data-availability` is ONE
   recall row (2026-07-21T16:22) that cost a full model judgment in SIX separate
   cycles (dream-20260721-1850 → dream-20260725-1241), each re-deriving the same
   SKIP, with ~18 more owed before it aged out of the 7d window. Enumeration is
   deterministic; judgment is not — same split as `mvm sweep` (step 0.5) and
   `dream-pending-carry` (step 6). It is **fail-open toward judgment**: a topic
   re-surfaces as NEW whenever the evidence changes (new fallback rows —
   recurrence is exactly when predictability flips toward ingest), the
   adjudication expires (>30d), or the covering doc goes missing/superseded. An
   adjudication can only ever suppress the identical question on identical
   evidence, so it can never hide a growing gap.
   **Stale-ledger pre-check (the judgment applied to NEW topics):** the
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
   - **Canonical cross-check FIRST (cycle-level, deterministic, zero tokens):**
     `contamination-scan` — greps every curated PoE2 doc + its `.tests.yaml`
     answers against the canonical PoE1-only ban-lists. This catches the class
     `mvm verify` **structurally cannot**: a doc self-consistent with its OWN
     tests but contradicting a canonical allowlist (the test was authored from
     the same contaminated source, so it passes forever). Real case that
     motivated it (2026-07-24): `0.4/crafting/catalyst-mechanics.tests.yaml`
     test#4 hard-codes "Fertile catalyst (life tag)" — Fertile is PoE1-only;
     29 hits across ~10 docs. Each hit = fix the tested doc + its test, then
     re-run the injected-verify gate. Extend the ban-list by adding rows to
     `canonical/catalysts.md` § "Confirmed PoE1-only" (the tool parses it live).
   🔧 **RUN `mvm-mirror` BETWEEN A REPAIR AND ITS RE-VERIFY — the gate reads the
   `~/resources/` tree, and an Edit under `~/mvm/knowledge/` is NOT there until the
   `*/5` mirror cron fires (measured 2026-08-05).** The failure is silent and
   **inverted**, which is what makes it dangerous: the re-verify grades the *new*
   doc against the *old* `.tests.yaml`, so a CORRECT repair reports `FAIL` with the
   stale string in `expected` and the corrected string in `candidate` — it reads
   exactly like the repair broke the doc, when in fact it worked and the ORACLE was
   stale. A newly-added test id is worse: `mvm verify` returns
   `error: no test with id=N` + `available_ids` from the pre-edit file, which looks
   like a malformed edit. Measured on the ancient-infuser repair: the `.md` had
   mirrored but the `.tests.yaml` had **not** (May-10 mtime, 8 ids vs 9), so the two
   halves of one repair were graded against each other across a version skew.
   ⚠ **Never "fix" a post-repair FAIL by reverting the doc** — first check
   `ls -la` on BOTH trees, run `mvm-mirror` (rc=0, seconds), and re-verify. After the
   mirror here, both ids passed on attempt 1.
   **ALWAYS pass `--test-id` (ONE test); never run bare `mvm verify <doc>`
   (full suite) — full-suite reliably TIMES OUT on large docs
   (poe2-crafting-codex, 0.5.0-patch-notes burned ~10min for 0 verdicts,
   dream-20260614-0431), and a timeout judges nothing → counts only as a
   `transients` probe-noise entry, not signal.
   ⚠ **"One id = one cheap verdict" is TRUE ON AVERAGE and FALSE IN THE TAIL —
   budget for the tail (2026-07-27, measured).** One test id costs
   `(retries+1) * 2 subprocesses * MVM_VERIFY_TIMEOUT` = **3 × 2 × 120 = 720 s
   worst case**, because every attempt spawns a retriever AND a grader, and a
   FAILing test burns all 3 attempts. A probe that PASSES on attempt 1 finishes
   in **~50–75 s**; one that fails into retries blows past 200 s. **So wrap
   single-id probes in `timeout 900`, never `timeout 200`.** The old 200 s
   wrapper straddled exactly that boundary, which is why the resulting deaths
   were intermittent and famously "did not reproduce" on retry.
   **This one wrong number produced 9 of the last 10 cycles' `probe-noise`
   transients** — a 9:1 ratio against 1 real `quality_repair`, which tripped the
   step-0 "infrastructure degrading" rule every cycle while the retriever was
   healthy the whole time. `mvm verify` now self-reports the bound: ask it with
   `worst_case_wall_s` in any kill record, and do not re-derive it by hand.
   Log probe; stamp `dream-verify-pick record <doc> <PASS|FAIL> quality`.
   - Engine PASS → done.
   - Engine FAIL with an `error` field / retry-exhausted EMPTY stdout →
     classify as `transients` tagged **`probe-noise:`** (the retriever/CLI
     failed, the doc was never judged), NEVER a content defect or quality repair
     (auditor sign-off dream-20260610-0100, point 4).
   - ⛔ **`error: killed_by_signal` (rc=124/143) is NOT probe-noise — it is a
     CALLER-SIDE budget bug and it is YOURS to fix, not to log.** Since
     2026-07-27 `mvm verify` installs SIGTERM/SIGINT/SIGHUP handlers that emit a
     structured record (`judged:false`, `signal`, `elapsed_s`,
     `worst_case_wall_s`, `hint`) on stdout in `--json` mode plus a human line on
     stderr — so a killed probe can never again be silent. **Re-run it with a
     budget above the reported `worst_case_wall_s`; only file a transient if it
     dies for some OTHER reason.** Before that fix a SIGTERM produced **0 B on
     stdout AND 0 B on stderr**, which is indistinguishable from a crash, an OOM
     kill, or a hang — that ambiguity is what caused the misfiling. If you ever
     again see a genuinely empty 0 B/0 B result, that means the handlers were
     bypassed (SIGKILL, e.g. a real OOM) — check `oom-forensics`, do not assume.
   - Engine FAIL → cross-check with ONE orchestrator-graded cold-clone
     (inject doc, grade per doctrine: lenient on phrasing/enumeration
     completeness, strict on facts — the engine grader is measurably
     stricter, exp6 2026-06-09). Both fail → `/mvm-ingest` the doc's
     `source:` URL (overwriting re-ingest). Cross-check passes → transient
     tagged **`grader-strict:`**, no action.
   **TRANSIENT CAUSE TAG IS MANDATORY (2026-07-25, enforced — `dream-log-append`
   returns rc=2 on an untagged entry).** Every `transients` string starts with
   exactly one of `probe-noise:` · `grader-strict:` · `staleness-disconfirm:`
   (or be a dict with a canonical `cause` key). Why it's load-bearing: these three
   mean OPPOSITE things — probe-noise is infrastructure failing, grader-strict is
   the two-tier filter working as designed, staleness-disconfirm is healthy
   skepticism — so an untagged bucket makes the step-0 meta-audit rule fire on
   corpus health. Tag at mint time; the next cycle counts tags, not prose.

4. **Staleness — spot-check + supersede** (`dream-verify-pick pick 3
   <step-3 docs...>`; stamp each `PASS staleness` after):
   - **Distribution sanity-check (run ONCE per cycle, before the per-doc picks):**
     `lib-staleness-audit --json` — emits the curated-lib freshness histogram +
     oldest-N re-verification worklist + a deterministic header-zombie
     (SUPERSEDED/DEPRECATED/RETIRED) contradiction-RISK proxy. This measures
     least-recently-**DATED** (content freshness by in-doc date→git→mtime), a
     complementary axis to `dream-verify-pick`'s least-recently-**VERIFIED**.
     Use it two ways: (a) if `stale_frac` > 0.05 the lib is drifting — prefer the
     audit's oldest-N worklist docs over the verify-pick rotation this cycle;
     (b) any header-zombie that is NOT an intentional tombstone (e.g. `kalshi.md`
     RETIRED is a deliberate keep-live block) → resolve as a real contradiction.
     Baseline 2026-06-27: 232 docs, stale_frac 0.004, 1 benign zombie — healthy.
   - **Skip-guard first:** `mvm relations <doc> --rel superseded_by --json` —
     non-empty `out` edge = already superseded → skip, take next-oldest.
   - Else: first test's `q` → KB-clone (Read doc) + web-clone (WebSearch) in
     parallel; log probes.
   - Agree → done. Disagree with fresher-source web markers → second web probe,
     different phrasing. Confirmed → `/mvm-ingest` fresh URL; mark old
     canonical `status: superseded` + `superseded_by: resources/...`
     (root-relative); `mvm index`; **verify the edge resolves**
     (`mvm relations <old-doc> --rel superseded_by` must list the new doc —
     empty = dangling pointer, fix before moving on). Disconfirmed → transient
     tagged **`staleness-disconfirm:`** (mandatory tag — see step 3).
   - ⛔ **A web-clone NOT-FOUND that cites blocking/403/402/"access-restricted" is
     NOT evidence about the world and MUST NOT be logged as `probe-noise:` and left
     there — re-run the URL yourself through the `web` ladder FIRST (added
     2026-07-27, measured).** The clone has only WebSearch/WebFetch; it structurally
     cannot reach the repaired `web` tier-ladder, so its reachability verdict is a
     fact about the CLONE, never about the source. Measured this cycle: the clone
     reported `poe2wiki.net/wiki/Perfect_Orb_of_Transmutation` as access-restricted
     and returned NOT-FOUND; `web <same url>` returned **200 OK, 34,697 B, in 0.12 s
     at `tier=minimal`** — the CHEAPEST rung, no escalation needed — and confirmed
     the KB claim outright (Minimum Modifier Level 70; and 70/70 Transmute+Augment
     vs 50/50/50 Exalt+Regal+Chaos across five pages). **This converted what would
     have been a fourth consecutive cycle of "web leg blocked" probe-noise into a
     CONFIRMED first-party corroboration.** The prior three cycles
     (`dream-20260725-1832`, `-20260726-1238`, `-20260726-1843`) each logged this
     same class as instrument-limited and moved on; the limitation was real but the
     remedy was one command away the whole time. **Only after `web` ALSO fails may
     you log `probe-noise:`, and then say which tiers were tried.**

5. **Contested + gaps + cross-contradiction sweep:**
   ⚠ **EVERY cold clone spawned in THIS step gets a `recall-log add` append, same
   as steps 3 and 4 — this step used to be silent about it and that gap bites
   (2026-07-26).** Steps 3/4 each say "log probe"; step 5 spawned clones and said
   nothing, so a cycle that reached 5(b) without touching 3/4's clone paths ended
   with **zero** appends and tripped the fail-closed RECALL-LOG ENFORCER at Stop.
   The consequence is not cosmetic: `pretool-curated-write-gate` uses that ledger
   as its **only** evidence source, so an unlogged probe **blocks the next
   legitimate curated write**. Use an explicit `"kind":"dream-probe"` — the CLI's
   `infer_kind()` only maps the literal hints `dream-quality-verify` /
   `dream-staleness-verify` to `dream-probe`, so a step-5 hint like
   `dream-tension-discovery/...` would otherwise be counted as a **real recall**
   and inflate `mvm stats` (explicit `kind` wins over inference):
   ```bash
   echo '{"kind":"dream-probe","question":"<the probing question>",
    "topic_hint":"dream-tension-discovery/<topic>","decided_source":"kb",
    "decided_answer":"<answer> — <doc>, contradiction|dissolved",
    "evidence_paths":["<doc>"]}' | recall-log add
   ```
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
     keyword>" --top-k 3 --json`, then apply the **STRUCTURAL pair test** below to
     results #1 and #2. If it passes, ask both docs the same probing question via
     2 cold-clones; contradiction → flag both `status: cross-contested` +
     reciprocal `in_tension_with:` edges (root-relative), `mvm index`, surface.
     🔧 **RUN `dream-pair-test <doc_a> <doc_b>` — the four rules below are MECHANIZED
     (shipped 2026-08-03, selftest 12/12). rc=0 REJECT (do not spend clones) · rc=1 ADMIT
     · rc=2 instrument failure.** It applies rule 4 → rule 2 → rule 1 → rule 3 in that
     order, with morphology folding and the explicit genre/domain token list (both repairs
     below) built in, and it **names the rule that fired** so you never have to attribute a
     rejection by hand. It deliberately STOPS at the family-sibling question — that one is
     judgment — and returns `ADMIT-PENDING-SIBLING-CHECK` with the differing terms printed,
     which is the only call you still make yourself.
     **Why it exists:** this test degenerated FOUR times as hand-applied prose (rule 1 →
     metadata hygiene; rule 3 → domain membership, then filename convention, then rarity),
     plus a fifth opposite-direction defect (exact-token matching zeroed out a 0.9951 pair
     sharing its whole subject). Every one is an ENUMERATION bug, and enumeration is exactly
     what should not cost model tokens — same split as `mvm sweep` (0.5),
     `dream-coverage-triage` (2), `dream-pending-carry` (6). The prose below is now the
     DERIVATION and the regression suite; the tool is the executable. ⚠ If you change a rule
     here, change it in the tool and add the exhibit to `CASES` — a rule that lives only in
     prose is the failure this shipped to end.
     **The pair test — ALL FOUR must hold (no similarity number anywhere), plus rule 2b
     (time-series siblings, added 2026-08-08 — see the repair-8 block below):**
     1. **Same `kind`** (`metadata.kind` in the JSON) — a `recipe` and a `decision`
        cannot contradict; they answer different questions.
     2. **Same declared version, or both version-less.** A `0.4` doc and a `0.5`
        doc disagreeing is correct versioning, not tension — this was the dominant
        historical fire class (see below), so it is now a PRECONDITION, not an
        after-the-fact discriminator.
     3. **≥2 shared significant title terms — OR SATURATION** (ignore stop-words +
        `index`/`notes`). Cheap proxy for "these make claims about the same subject."
        ✅ **COVERAGE-NOT-COUNT repair, shipped 2026-08-03 in `dream-pair-test`
        (selftest 14/14) — this closes the standing false-negative row.** A raw ≥2
        count **penalizes CONCISE titles**, and so suppresses exactly the
        **canonical-vs-versioned** shape where a contradiction is most dangerous
        (canonical is the authority other docs are graded against). Measured:
        `0.4/crafting/essences.md` ↔ `canonical/essences.md` shares **one** term —
        and that term is the ENTIRE subject of both docs. So rule 3 now passes on
        `n_shared ≥ 2` **OR** on **saturation** (the shared set covering **100% of one
        doc's** significant terms): essences → coverage **1.000** ⇒ ADMIT. ⛔ **Do NOT
        instead lower the threshold to 1** — that re-admits the whole template family
        and re-creates the rule-1 degeneration. Saturation does not, **by
        construction**: family siblings differ precisely in the subject term, so they
        cannot saturate (es-body-armour shares 2 of 6 = 0.33; evasion 2 of 4 = 0.50).
        Verified on all **15** intra-family pairs of the 6-member
        `*-profit-recipe-0.5.md` template: **0 saturated**. Both halves are locked
        regression cases in the tool.
     4. **Neither doc is an AGGREGATOR** — ⛔ skip any path under `areas/` matching
        `backlog|research-queue|dashboard`, **any doc whose FILENAME is `index.md`/`INDEX.md`**,
        or any file >40 KB.
        🆕 **The index clause is repair 9 (2026-08-08, `d65f58f`, selftest 41/41) and it was the
        single highest-yield change ever made to this rung.** An index **lists contents and
        asserts no claim**, so no contradiction between two of them is expressible; they are also
        **machine-generated** (`reindex` / `mvm index`), so a "tension" between two would be a
        GENERATOR bug, not a knowledge defect — and cold clones structurally cannot find it.
        Measured: **666 index docs corpus-wide, ZERO previously caught** by the path-pattern or
        size clauses. On a 400-random-pair sample `rule4_aggregator` went **8 → 107** and
        `ADMIT-PENDING-SIBLING-CHECK` went **3 → 0** — and all **3/3** pre-repair admissions were
        verified to be `INDEX.md`↔`INDEX.md` pairs. ⚠ **So the discovery rung's ENTIRE admit
        stream in a random sample was generated-index junk, at 2 cold clones apiece.** Every
        earlier repair here argued about which *real* docs to admit; nobody had checked what the
        admit stream was actually *made of*. **Measure the composition of a gate's output, not
        just its rules.** Found only as the repair-8 residual: the 2 undated members of the
        87-doc daily family were its own `index.md`/`INDEX.md`, leaving 173 pairs open. Grab-bag files pair
        with everything (measured: `research-queue-cold.md` ↔ `backlog.md` = **0.9805**
        cosine, *higher* than a real near-dup pair) and they are the single largest
        source of false fires.
     **Done-test for this rung — apply rule 4 DIRECTLY to the named pair; do NOT wait
     for a rotation to produce it** (repaired 2026-07-27, see below): the pair
     `areas/research-queue-cold.md` + `areas/research-queue.md` must be REJECTED
     (rule 4 — both live, both mirrored, both match the `research-queue` path pattern
     under `areas/`; the PATH-PATTERN clause is what rejects them, NOT the size clause.
     ⚠ an earlier version of this line claiming "both >40 KB" was FALSE, and the 2026-07-27
     repair that caught it cited `research-queue-cold.md` at **342 B** — which has ITSELF gone
     stale: re-measured 2026-07-28 it is **30,328 B** (89× growth in ONE day, from cold-rotation
     doing its job), against `research-queue.md` at **77,426 B**. Both are still under/over the
     40 KB line in a way that leaves the size clause NON-firing for the cold file, so the
     PATH-PATTERN clause remains the one that rejects the pair. **Lesson, now twice-earned: do
     not pin a done-test's rationale to a measured SIZE — sizes drift fast enough to falsify the
     prose within a day. Cite the CLAUSE that fires and re-measure at read-time.** A done-test
     whose stated RATIONALE is wrong still passes for the wrong reason, which is how a rule
     quietly stops meaning what it says), and the pair `poe2/0.5/crafting/jewellery-quality-system.md` +
     `poe2/0.4/mechanics/quality.md` must ALSO be rejected (rule 2 — different
     declared versions). If a rotation reports a fire, record which rule admitted it.
     ⛔ **The original done-test named `areas/backlog.md` and was VACUOUS — it could
     never run (measured 2026-07-27).** `~/areas/backlog.md` has been a **symlink →
     `research-queue.md`** since 2026-07-23 15:33 (the consolidation), and symlinks are
     not mirrored, so `~/mvm/knowledge/areas/backlog.md` **does not exist** and no
     `mvm search` can ever return that path. A done-test whose input the engine cannot
     emit is unfalsifiable — the same **gate #20 unreachable-threshold class this very
     step already documents twice** (`score > 0.7` against a 0.600 ceiling). Two
     independent instances of one class inside one rung is the signal: **when writing a
     done-test, verify the named INPUT is producible before trusting the negative** —
     `ls` the mirrored path, don't assume the live path implies it. Note also that the
     0.9805 cosine cited in rule 4 was measured against the PRE-consolidation
     `backlog.md`; the finding stands (aggregators pair with everything) but the exhibit
     no longer exists as a distinct file, so cite it as history, not as a live check.
     🔴 **RULES 1 AND 2 TREATED *ABSENCE* AS A VALUE — repaired 2026-08-04 in
     `dream-pair-test` (repairs 5/6/7, selftest 24/24). Three coupled defects, one live
     exhibit.** Found by feeding `kalshi/api-field-notes.md` ↔ `kalshi/orderbook-mechanics.md`
     to the tool: it returned REJECT/rule2 as *"version-spanning (None vs 1.00)"* — and the
     `1.00` was scraped out of the literal **`$1.00`** in *"YES + NO must sum to $1.00."*
     - **(5) `declared_version` matched any `\d+\.\d+` in path+title+body[:400].** Measured
       corpus-wide: 725 docs got a "version" and **307 (42.3%) came from free body prose** —
       money (`$1.00`, `8.00`, `12.5`), arXiv ids (`2408.03314`), DOIs (`10.1016`). Extraction
       is now priority-ordered and each tier needs an actual CLAIM: frontmatter `version:` →
       a path segment that IS a version (`/0.4/`) → a cued mention (`PoE2 0.5`, `patch 0.4`) →
       a bare `\d\.\d` guarded against `$`-prefix, `%`-suffix and longer numeric runs. Net
       725 → 635; the 96 dropped are dominated by money/arXiv shapes, and only **6** genuine
       0.4/0.5 docs are collateral — an acceptable trade because a MISSING version is now
       harmless (falls through to rules 3/4) whereas a SPURIOUS one was a hard reject.
     - **(6) Rule 2 rejected `None vs X`, which structurally foreclosed the
       canonical-vs-versioned shape that rule 3's saturation repair (2026-08-03) shipped to
       ADMIT.** Two rules in this rung contradicted each other, and the tool's own DONE-TEST
       (`0.4/crafting/essences.md` ↔ `canonical/essences.md`) passed **only by accident** —
       `canonical/essences.md` happened to scrape `0.4` out of its body under the broken
       extractor. Fixing (5) alone flips that case to REJECT, which is how the coupling
       surfaced. Rule 2 now rejects **only when BOTH docs declare a version and they differ**,
       because *"correct versioning, not tension"* is an EXPLANATION and a version-less doc
       offers no such explanation.
     - **(7) Rule 1 rejected `kind: reference` vs no-frontmatter** — the same disease, and the
       paragraph below already names it (*"metadata hygiene, not semantics"*). Rule 1 now also
       requires both sides to declare. **ABSENCE IS NOT A VALUE**, in either rule.
     With all three in, the exhibit pair is rejected by **rule 3 on the merits** (shares only
     the domain token `kalshi`; coverage 0.33/0.20). ⚠ The verdict was REJECT all along — all
     three *reasons* were wrong. **A gate that reaches the right answer for a wrong reason is
     already broken; the next pair it meets is where you find out.**
     ✅ **Repair 6 immediately paid: it made a REAL contradiction reachable.** Under the old
     rule 2, `0.4/currency-items/vaal-orbs.md` (0.4) ↔ `currency-orbs/vaal-corruption.md`
     (version-less) was auto-rejected. Admitted and probed this cycle, the two returned
     **different Vaal Orb outcome tables**; adjudicated against `poe2wiki.net/wiki/Corrupted`
     (first-party, via `web`), `vaal-corruption.md` was carrying **PoE1 behavior**
     (implicit-reroll, rarity-downgrade "brick") — superseded in-cycle. That contradiction had
     been structurally undiscoverable for the gate's entire life.
     🆕 **RULE 2b — TIME-SERIES SIBLINGS. Shipped 2026-08-08 in `dream-pair-test` (repair 8,
     selftest 36/36, commit `6c542d8`); it is the temporal analogue of rule 2 and lands for
     rule 2's exact reason.** Live fire: `areas/health/daily/2026-05-13.md` ↔
     `2026-05-19.md` returned ADMIT on `shared=['05','daily','rollup']`. Titles are literally
     *"Daily rollup — 2026-05-13"*; the family has **88 members ⇒ 3,828 intra-family pairs,
     every one guaranteed-null** (two different days cannot contradict — no contradiction is
     even *expressible*). **Rule 1's degeneration for the SIXTH time — rule 3 measuring
     filename convention, not shared subject — and the first instance found OUTSIDE the PoE2
     corpus, which is the news: this rung's five prior repairs all read as PoE2-specific, so
     the disease was mis-scoped as a domain quirk when it is a property of any template family.**
     - **(8a) `VERSION_RE`'s `20\d{2}-\d{2}-\d{2}` alternative was DEAD CODE.** `tokenize_title`
       splits on `[^A-Za-z0-9.]+`, so `2026-05-13` is already **three** tokens before VERSION_RE
       is ever consulted. The YEAR dropped (via the `20\d{2}` branch) while **the MONTH and DAY
       fragments survived and were counted as significant subject terms** — so repair (2)'s
       "drop bare years/dates" was only half implemented. Same unreachable-branch class as
       gate #20; the branch **read** as if it handled dates, so nobody re-checked it. ⚠ **A
       regex alternative is not reachable just because it is written — check it against the
       tokenizer that feeds it.**
     - **(8b) `rollup` → `GENRE_TOKENS`** (a doc genre, like summary/overview). ⛔ **`daily` was
       deliberately NOT added** — it makes a cadence claim and can be subject-bearing, and
       dropping `rollup` alone suffices. Every prior rule-3 repair that over-dropped ate a
       subject noun; take the smaller change.
     - **(8c) THE LOAD-BEARING FIX, because 8a+8b ALONE MADE IT WORSE.** With the fragments and
       `rollup` gone, **both titles reduce to the same single term `daily`**, so the SATURATION
       clause admitted the pair at **coverage 1.000** — harder than before. 📐 **Stripping the
       discriminator made the two docs look MORE alike: normalizing away noise can MANUFACTURE
       a false match.** That is the trap under all four prior rule-3 tweaks, stated in one line.
       So rule 2b **names the real discriminator (time)** instead of deleting more tokens: a
       differing **date-of-scope EXPLAINS** a disagreement exactly as a differing version does.
     **Scope is narrow by construction** (mirrors repair 5's *"each tier needs an actual CLAIM"*):
     the date must be a **terminal PATH SEGMENT** (`.../2026-05-13.md`), which claims the doc IS
     about that date. A title parenthetical (*"(poe2wiki, live 2026-06-11)"*) is **PROVENANCE** —
     when it was observed, not what it is about — and is deliberately **not** read, which is what
     keeps the locked `jewel-desecrate-forcing` ↔ `desecrate-reveal-mechanics` exhibit admitted.
     **ABSENCE IS NOT A VALUE** (repairs 6/7): both sides must declare. Measured blast radius:
     **86 path-dated docs corpus-wide, ALL in `areas/health/daily/`** ⇒ 3,655 null pairs rejected,
     no other family touched; 400-random-pair impact sample = **0** rule2b fires, 0 verdict
     regressions. Re-measure in one `find`, don't trust this count second-hand.
     🆕 **RULE 2c — ENUMERATED SIBLINGS (repair 10, 2026-08-08, `5a908a3`, selftest 44/44): the
     GENERAL shape, of which rule 2b's dates are one special case.** Found by hunting repair 8's
     **signature** (`differing terms a=[] b=[]`) across other title-token-identical families
     instead of waiting for a rotation to produce one — the 22-member
     `substrate-authoring-gates-*` family yielded **66 null ADMITs**, because gates 01-10 and
     11-20 are different gates exactly as 05-13 and 05-19 are different days. **Test:** strip
     digits from both basenames; if the remainders are **identical** and the digit sequences
     **differ**, the docs are consecutive members of one enumerated series and their whole
     discriminator is a number ⇒ no contradiction expressible. ⛔ **Deliberately NOT
     *"differing terms empty ⇒ reject"*, which is the tempting one-liner and is REFUTED by this
     rung's own locked done-test:** `0.4/crafting/essences.md` ↔ `canonical/essences.md` also has
     empty differing sets (both reduce to `essence`) yet must be ADMITTED, and it carried a real
     contradiction. It survives because its basenames are identical with **no differing digits** —
     distinguished by PATH (authority scope), not by an index. 📐 **ENUMERATION AND SCOPE LOOK
     IDENTICAL TO A TOKEN COUNTER AND MEAN OPPOSITE THINGS**; both directions are locked guards.
     Measured: gates family 231 pairs → rule2c 34, rule4 165, admits **66 → 32**. ⚠ The 32
     survivors are a different shape (`derivations-NN` vs `index-NN`: same range, different doc
     genre) and are left admitted **on purpose — recorded, not silently capped.**
     📐 **Method note worth more than any single repair: repairs 9 and 10 were both found by
     auditing the RESIDUAL of repair 8 and by hunting its signature, not by waiting for the next
     live fire.** One fire, three defects. **When a gate defect is found, ask what else has that
     shape before closing the cycle** — the rotation surfaces roughly one pair per cycle, so
     waiting for it to find a family is orders of magnitude slower than querying for the family.
     ⚠ **Rule 1 (`same kind`) is a near-no-op and must NOT be trusted as the rejecting
     rule (measured 2026-07-27): 4,703 of 4,812 KB docs — 97.7% — declare NO `kind`.**
     So ~95.5% of random pairs are both-`<NONE>` and PASS rule 1, while the only pairs it
     rejects are the mixed case (one doc happens to carry a `kind:`, the other doesn't)
     — i.e. it discriminates on **metadata hygiene, not semantics**. Both pairs that
     cleared the 0.98 gate on 2026-07-27 were rejected by rule 1 incidentally; both were
     ALSO correctly rejected on the merits by rules 2/3, which are the load-bearing ones.
     **When recording which rule admitted or rejected a pair, name a rule OTHER than 1
     wherever one applies** — attributing a rejection to rule 1 overstates the gate.
     ⚠ **RULE 3 IS AT RISK OF RULE 1's DEGENERATION — corpus-wide boilerplate must NOT
     count as a "significant" shared term (observed 2026-07-27 on a live fire).** The
     pair `0.4/mechanics/sanctified-flag.md` + `0.4/crafting/hinakoras-lock.md` (rank-2
     text 0.9840) shares exactly three title terms: **`PoE2`, `0.4`, `mechanic`**. But
     `PoE2` and `0.4` appear in the title of virtually every doc in this corpus's largest
     domain, so counting them satisfies "≥2 shared significant title terms" for **any**
     same-version PoE2 pair — the rule then measures DOMAIN MEMBERSHIP, not shared
     subject, which is precisely how rule 1 degenerated into a metadata-hygiene check.
     **Excluding the domain name and the version marker, that pair shares ONE term
     (`mechanic`) and rule 3 would REJECT it.** The pair was admitted anyway (correctly —
     the two docs genuinely cover the same fracture/Sanctification interaction, and they
     were probed and DISSOLVED in full agreement), so treat this as a caveat, not a
     repair: **when counting shared title terms, drop the domain token and the version
     token first, and if what remains is <2, say so rather than reporting a rule-3 pass.**
     ⚠ **DOC-FAMILY TEMPLATE TERMS ARE THE THIRD DEGENERATION OF RULE 3 — and unlike the
     domain/version case this one fires COMBINATORIALLY (measured 2026-07-29).** The fire
     paired `0.5/crafting/es-body-armour-profit-recipe-0.5.md` with
     `0.5/crafting/evasion-body-armour-profit-recipe-0.5.md` (rank-2 text **0.984**). All four
     rules PASS honestly: rule 1 (**both `kind: canonical`** — a real same-kind pass, not the
     usual both-`<NONE>` no-op), rule 2 (both 0.5), rule 3 (after dropping `poe2` + `0.5` the
     titles still share `body`/`armour`/`profit`/`recipe`), rule 4 (5,780 B and 7,743 B, not
     aggregators). Probed with 2 cold clones, it **DISSOLVED completely**: the ES doc is a Vile
     Robe magic base finished with Greater Essence of Enchantment, the evasion doc an ilvl≥82
     exceptional base finished with Fracture + chaos-spam + Greater Exalt. **Different subjects,
     therefore different methods — no contradiction is even expressible.**
     The structural problem: `*-profit-recipe-0.5.md` is a **naming TEMPLATE with 6 members**
     (`breach-tablet-rolling`, `es-body-armour`, `es-helmet-caster-tiara`, `evasion-body-armour`,
     `ms-boots`, `wand-caster` — measured by `find`). Every member shares `profit`+`recipe`+
     version, so **all 15 intra-family pairs pass rule 3**, and every one of them must dissolve
     **by construction**, because a recipe family is differentiated precisely by the term the
     pair does NOT share. That is 15 guaranteed-null probes (2 clones each) available to the
     rotation — the rule stops measuring shared subject and starts measuring **shared filename
     convention**, exactly as rule 1 came to measure metadata hygiene.
     **Discriminator to apply before probing — it is rule 1's own logic, one level down:**
     rule 1 rejects a `recipe` vs a `decision` because *they answer different questions*. Two
     `canonical` recipes **for different items answer different questions too.** So: after
     dropping domain, version, AND any term that is corpus boilerplate (**threshold: a term in
     ≥0.2% of titled docs — re-measure it, do NOT reuse a raw count**; see the calibration note
     below), ask **what the DIFFERING title terms name**. If they
     name the subject/base/item being crafted (`es` vs `evasion`, `boots` vs `wand`), the pair
     is a **FAMILY SIBLING → reject, do not spend clones.** Only probe when the differing terms
     are incidental and the SUBJECT genuinely coincides.
     ✅ **The discriminator GENERALIZES beyond the family it was derived from — re-confirmed on a live
     fire 2026-08-02.** That fire was `0.5/crafting/breach-tablet-rolling-profit-recipe-0.5.md` ↔
     `0.5/crafting/irradiated-tablet-farming-and-rolling-recipe-0.5.md` (rank-2 text **0.9839**), which
     sits in the WIDER `*-recipe-0.5.md` template family (**12 members**, so **66** intra-family pairs),
     not the narrower `*-profit-recipe-0.5.md` family (**6** members / 15 pairs) the rule was written
     from. Rules 1–4 all pass honestly — rule 1 a REAL same-kind pass (both `kind: canonical`), rule 2
     both 0.5, rule 3 two surviving shared terms (`tablet`, `rolling`) after dropping `poe2`/`0.5` and
     the ≥3-title boilerplate `recipe` (46 files), rule 4 neither an aggregator (7.5 KB / 11.6 KB) — so
     **the family-sibling discriminator is the ONLY thing that rejects it.** Differing terms `breach` vs
     `irradiated` name the tablet TYPE, i.e. the base being crafted ⇒ different questions ⇒ reject.
     🔧 **CALIBRATION — the ≥3-title boilerplate cutoff was itself a degeneration, repaired 2026-08-02
     (measured).** Written as a RAW COUNT with no corpus denominator, it was derived from `recipe` (34
     titles) and then applied to everything. Measured over all **4,863 titled docs / 3,970 unique title
     terms**: dropping every term in **≥3** titles removes **83.3% of all title-term occurrence mass**,
     so rule 3's "≥2 shared significant terms" was being evaluated on the near-hapax 16.7% tail — two
     docs sharing two terms that each appear in ≤2 titles corpus-wide is essentially a filename
     near-duplicate. **That is rule 1's degeneration one more time: a clause that stops measuring shared
     subject and starts measuring rarity.** Caught by a live pair it wrongly rejected —
     `0.4/endgame/atlas-tower-system.md` ↔ `0.4/endgame/map-juicing.md` (rank-2 text **0.9955**), where
     the dropped terms were `doctrine` (14 titles, genuine boilerplate) and **`juicing` (3 titles —
     0.06% of the corpus, i.e. the single most subject-bearing word in both titles)**. Measured
     separation at **≥10 titles (0.206%)**: DROPS `crafting` 107 · `tablet` 58 · `recipe` 34 · `profit`
     15 · `doctrine` 14 · `system` 10; KEEPS `tower` 6 · `juicing` 3 — clean on both sides, and it
     removes 59.2% of mass instead of 83.3%. **Use the FRACTION (≥0.2% of titled docs), not the integer**,
     because the integer silently retightens as the corpus grows — which is exactly how this clause
     broke. ⚠ Under the repaired threshold the 2026-08-02 `breach-tablet` fire above loses `tablet`
     (58 titles ⇒ boilerplate) and keeps only `rolling` ⇒ **rule 3 now rejects it outright**, one rung
     cheaper than the family-sibling discriminator did. That paragraph's verdict stands; its stated
     "two surviving shared terms" reasoning is superseded by this note — recorded rather than silently
     overwritten, because the verdict was right for a reason that no longer applies.
     ✅ **The wrongly-rejected pair was then probed on the merits and DISSOLVED in full agreement**
     (2 cold clones, 2026-08-02): both docs make TABLETS the primary juicing lever with towers merely
     unlocking slots, and both independently state the same 3×3=9 geometry and the same tiered unlock
     (0–2 mods → 1 slot, 3–5 → 2, 6 → 3). So the corpus is consistent here — the finding is about the
     GATE, not the docs. **A rule that rejects for the wrong reason still needs fixing even when its
     verdict happens to match**, because the next pair it mis-rejects will be a real contradiction.
     🔴 **THE ≥0.2% FRACTION EATS THE DISCRIMINATOR THAT FOLLOWS IT — measured 2026-08-03, and this is
     an INTERNAL CONTRADICTION, not a tuning nit.** Re-measured over **4,859 titled docs** (threshold =
     **9.7 titles**), the fraction classifies as "boilerplate" **17 of 20 canonical PoE2 subject nouns**:
     `omen` 136 · `atlas` 100 · `map` 97 · `essence` 68 · `tablet` 59 · `breach` 52 · `amulet` 40 ·
     `shield` 40 · `ring` 38 · `jewel` 33 · `energy` 27 · `es` 20 · `desecrate` 19 · `gloves` 19 ·
     `rune` 17 · `boots` 16 · `catalyst` 16 · `wand` 15 · `armour` 14 · `evasion` 14. Only `sceptre` 5 ·
     `quarterstaff` 4 · `flask` 3 survive — **and they survive purely by being rare.** The decisive
     consequence: the family-sibling discriminator directly above instructs you to drop boilerplate and
     then ask what the **DIFFERING** terms name, citing `es` vs `evasion` and `boots` vs `wand` — **all
     four of its own worked-example terms are dropped before it runs (20/14/16/15, every one ≥9.7), so
     the discriminator cannot fire on a single one of its own examples.** A rule whose exhibits its own
     preprocessing destroys is not calibrated; it is inoperative. This is rule 1's degeneration for the
     **fourth** time — the clause has stopped measuring shared subject and now measures rarity, which is
     the exact failure the 2026-08-02 repair was written to end (it moved the cut ≥3 → ≥10 and thereby
     traded under-dropping for over-dropping; **both directions are the same disease**).
     🔴 **SECOND, INDEPENDENT DEFECT — RULE 3 COUNTS EXACT TOKENS, SO MORPHOLOGY ALONE CAN ZERO OUT A
     PAIR THAT SHARES ITS ENTIRE SUBJECT (measured 2026-08-03, live fire).** The fire was
     `0.5/crafting/jewel-desecrate-forcing.md` ↔ `0.5/crafting/desecrate-reveal-mechanics.md` (rank-2
     text **0.9951**). Titles: *"Jewel desecration & mod-forcing (PoE2 0.5)"* vs *"Desecrated modifier —
     reveal mechanics (poe2wiki, live 2026-06-11)"*. Shared **exact** significant tokens: **ZERO** —
     because `desecration` ≠ `desecrated` and `mod` ≠ `modifier`. Both are `kind: canonical`, both 0.5,
     neither an aggregator (6,132 B / 4,612 B), and they are unmistakably about the same subject. **So
     rule 3 rejects at 0 shared terms while the retriever scores them 0.9951 — the under-admitting
     direction, which none of the three prior degenerations covered (all three were over-admitting).**
     Note this defect is *upstream* of the threshold: no frequency cut can repair it, since the tokens
     never match in the first place.
     🔧 **REPAIR — apply BOTH, they address opposite failure directions:**
     (1) **Fold morphology before comparing.** Lowercase and strip common inflections (`-s`, `-ed`,
     `-ing`, `-ion`/`-ions`, `-tion`) and treat a token that is a prefix of another surviving token
     (≥5 chars) as the same term, so `desecrate`/`desecration`/`desecrated` and `mod`/`modifier` count
     ONCE. Under this, the pair above shares `desecrat*` + `mod*` = **2** ⇒ rule 3 correctly ADMITS.
     (2) **Replace the frequency cut with an EXPLICIT GENRE/DOMAIN TOKEN LIST** — the domain name
     (`poe2`, `poe`), the version marker (`0.4`, `0.5`, bare years/dates), and doc-genre words
     (`crafting`, `recipe`, `doctrine`, `system`, `mechanic`, `mechanics`, `notes`, `index`, `overview`,
     `summary`, `guide`, `spec`, `profit`). **A closed list is falsifiable, stays stable as the corpus
     grows, and by construction never eats a subject noun** — whereas any fraction silently re-tightens
     or re-loosens with corpus composition, which is how this clause has now broken twice in four days.
     Regression-checked against every exhibit in this rung: `atlas-tower-system` ↔ `map-juicing` still
     yields **1** shared term (`juicing`) ⇒ still rejected by rule 3, unchanged verdict; `es-body-armour`
     ↔ `evasion-body-armour` keeps `body`/`armour` ⇒ admitted by rule 3 and then correctly rejected by
     the family-sibling discriminator **which can now actually see `es` vs `evasion`**; `breach-tablet`
     ↔ `irradiated-tablet` keeps `tablet`/`rolling` ⇒ admitted, then rejected on `breach` vs
     `irradiated` naming the tablet TYPE. **That restores the family-sibling discriminator to being the
     load-bearing rejecter — which is what this rung says it should be — instead of letting a rarity
     proxy do that job invisibly and for the wrong reason.**
     ✅ **The 0.9951 pair was probed on the merits anyway (2 cold clones, 2026-08-03) and DISSOLVED in
     full agreement:** both docs state **3 offered modifiers, player chooses 1**, and both give Abyssal
     Echoes as a **single** reroll of those 3. The apparent divergence — the reveal doc lists the Lich
     omens (Blackblooded→Kurgal, Liege→Amanamu, Sovereign→Ulaman) while the forcing doc gives
     Sinistral/Dextral Necromancy side-locking — is **complementary scope, not contradiction**, because
     the jewel doc states outright that jewel desecrations *cannot* use Lich omens (incompatible pool).
     Corpus consistent; the finding is about the GATE.
     ⚠ **Verify the glob before "correcting" either count.** This cycle first read the family as 12 via a
     loose `grep -c 'recipe-0.5.md'` and nearly rewrote the accurate "6 members" line; `ls
     *-profit-recipe-0.5.md` is still exactly 6. **Two different families, two correct numbers** — the
     stale-measurement reflex this rung warns about cuts both ways, and a "fix" applied to a correct
     line is a regression. Re-measure with the SAME glob the prose names.
     ⛔ **DERIVATIONAL MORPHOLOGY IS A KNOWN, MEASURED, AND DELIBERATELY-UNREPAIRED GAP IN
     `_fold` — the obvious fix is REFUTED BY THE CORPUS, so do NOT ship it (2026-08-07, n=4,920
     titled docs / 3,660 folded terms).** Repair (1) folds *inflectional* suffixes only
     (`SUFFIXES = ations|ation|ions|ion|tion|ing|ed|es|s`) plus prefix-containment; it therefore
     **cannot** merge a *derivational* pair whose stems diverge mid-word. Live exhibit found this
     cycle: `resources/finances.md` ("Financial Profile") ↔ `areas/mike-finance-tooling.md`
     ("Mike's Finance Tooling Inventory") reports **`0 shared significant terms: []`**, because
     `financial` and `finance` neither fold (no `-ial` rule) nor prefix-contain (`finance` is not a
     prefix of `financial` — they split at char 7).
     **The tempting repair — "merge tokens sharing a ≥6-char prefix" — was measured and REJECTED.**
     Over the corpus it would newly merge **159** term pairs, and the head of that list is
     poisonous: **`currency`/`current`** (56 vs 7 titles — `currency` is one of the most
     subject-bearing nouns in the PoE2 domain), `general`/`generator`, `general`/`generative`,
     `identity`/`identical`, `identity`/`identify`, `convergence`/`convers*`, `sinister`/`sinistral`.
     It buys true folds (`economic`/`economy`, `skeletal`/`skeleton`, `defense`/`defensive`,
     `alchemist`/`alchemy`) at the price of collapsing distinct subjects — **which is rule 1's
     degeneration for the fifth time, in the over-admitting direction.** A closed explicit list
     (repair 2's doctrine) stays the only safe shape here, and nobody has yet paid for one.
     ✅ **Scope the gap honestly: no verdict is known to be wrong because of it.** On the exhibit
     pair, folding `financial`≈`finance` yields n_shared=1, coverage 0.50/0.25 ⇒ **still REJECT**.
     So this is an under-stated *reason*, not a proven bad *verdict* — logged as a gap, not a
     repair, precisely because this rung's own doctrine ("a gate that reaches the right answer for
     a wrong reason is already broken") has to be weighed against its other doctrine (four prior
     rule-3 tweaks each broke something). **Re-measure before ever revisiting: the 159-pair list is
     regenerable in ~30 s from `read_doc`+`tokenize_title`; do not trust this count second-hand.**

     ⚠ **VERSION-SPANNING PAIRS ARE THE DOMINANT FIRE CLASS AND ARE NOT TENSIONS**
     (measured on the gate's first live rotation, 2026-07-26). The very first fire
     paired `poe2/0.5/crafting/jewellery-quality-system.md` with
     `poe2/0.4/mechanics/quality.md`; the clones returned **different numbers**
     (0.4: max amulet quality 30% · 0.5: 20% base + 20 Essence + 10 Vaal Infuser
     = 50%) and it **dissolved**, because both docs are version-scoped in their
     path AND self-declare their patch in the answer. **Discriminator before
     flagging anything: do the two docs cover DIFFERENT VERSIONS and each say so?
     Then it is correct versioning — no `cross-contested`, no edge.** Only a
     disagreement *within the same declared version* is a real tension. The live
     hazard from such a pair is retrieval-side, not doc-side (a version-less query
     can land on the older doc), so it belongs in `gaps_surfaced`, never in
     `contested_resolved`.
     ⚠ **Do NOT gate on `score` (2026-07-26 — the previous rule was UNREACHABLE).**
     The old trigger read "top-2 similarity > 0.7" against `score`, but `score` =
     `0.6*text + 0.25*graph + 0.15*hier` where `text` is **min-max normalized so
     rank-1 is ALWAYS exactly 1.0**, and graph/hier are **always 0 unless `--near`
     is passed** (step 5b never passes it). So the ceiling for ANY query here is
     **0.600** — measured identical across 6 unrelated queries spanning poe2 /
     kalshi / french / finances. `>0.7` could never fire, so tension-DISCOVERY was
     structurally inert for its entire life and every "no new tensions" it reported
     was a **vacuous negative**, not evidence of a clean corpus. Gate on the
     normalized `text` component of rank-2 instead: it measures what the rule
     actually wants (is #2 nearly as good a match as #1 ⇒ near-duplicate pair worth
     probing for contradiction) and is reachable — observed rank-2 `text` spread on
     that same 6-query sample was 0.947–0.995, so 0.98 selects the genuinely tight
     pairs. **Lesson (gate #20 class): a threshold is only a test if some real input
     can cross it — check reachability against the metric's actual range before
     trusting a negative.**
     🔴 **THE ≥0.98 GATE IS ITSELF NOW RETIRED — DELETED, NOT RE-TUNED (2026-08-04, measured
     n=18). Take rank-1/rank-2 from each query and feed the pair STRAIGHT to `dream-pair-test`;
     that tool is the gate. Do NOT apply any similarity threshold first.** The number was
     re-measured against the structural tool over 18 queries spanning poe2 / kalshi / finance /
     french, and it is **worse than no filter on both axes**:
     - **Precision 2/7 (28.6%)** — of the 7 pairs that cleared ≥0.98, `dream-pair-test` rejected
       5 outright (rules 2/3/4). The gate's "genuinely tight pairs" are mostly junk.
     - **Recall 2/5 (40%)** — it **discarded 3 of the 5 structurally-admissible pairs**, i.e. the
       gate threw away 60% of the real candidates.
     What it discarded is the indictment: `canonical/essences.md` ↔ `0.4/crafting/essences.md`
     at **0.9735** — *the exact canonical-vs-versioned pair the saturation repair shipped hours
     earlier (2026-08-03) to admit.* So the repair was **unreachable through discovery**: rule 3
     was fixed to admit a pair the upstream number never emitted. **And that pair carried a REAL
     same-version contradiction** — the 0.4 doc asserted a *"Perfect/Corrupted Essence of the
     Abyss"* while canonical lists Abyss as **Corrupted-only**; probed and repaired this cycle
     against poe2db (tier-1: `Essence of the Body` renders Lesser/Greater/Perfect forms,
     `the Abyss`/`Hysteria`/`Horror` render bare-name only). **The gate was not merely filtering
     candidates — it was hiding a live defect for as long as it existed.**
     **Why deletion is free, and why this is not a fourth number:** `dream-pair-test` is
     deterministic and costs **zero model tokens**, so it can absorb every pair. Measured on the
     same 18: it rejects **13/18 (72%)** mechanically and surfaces 5 for judgment. A numeric
     pre-filter can therefore only ever *subtract* recall — it buys nothing the free tool
     wasn't already going to do. This is the doctrine below carried to its conclusion: all three
     similarity proxies are dead, and the honest successor to a dead number is **no number**,
     not a fourth one. ⚠ Rank-2 `text` remains fine as a *display* value in the cycle log; it is
     simply no longer permitted to reject anything.
     ⚠ **ALL THREE SIMILARITY PROXIES ARE NOW DEAD — do NOT propose a fourth number
     (2026-07-26 reflect, measured).** (1) `score > 0.7` was **unreachable** (ceiling
     0.600). (2) rank-2 `components.text ≥ 0.98` is **content-blind**: RRF is
     rank-based then max-normalized, so the value is a ratio of rank-reciprocals —
     measured over 11 queries, the value **0.9841 occurs on three of them**
     (`catalyst quality amulet`, `budget monarch categories`, `french lesson
     subjunctive`), reported as IDENTICAL while their absolute cosines spread
     0.7820 / 0.6425 / 0.6855. (3) **doc↔doc absolute cosine** (`mvm doc-sim`, built
     and graded this cycle) is **INVERTED on the falsifier**: the FALSE pair scores
     **0.9805** against the TRUE near-dup pair's **0.9367**, versus a random-pair
     null of p50 0.642 / max 0.810 (n=210) — so no threshold separates them.
     **Root cause shared by all three: TENSION IS A RELATION BETWEEN CLAIMS, and no
     aggregate over a whole document carries claim structure.** A whole-doc embedding
     of a grab-bag file encodes genre and voice, not subject — *a document about one
     thing embeds its subject; a document about forty things embeds its author.*
     That is why the structural test above uses zero similarity scores.

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
   **PENDING CARRY CONTRACT (2026-07-17, auditor #88 F1): every `:PENDING`
   from the PREVIOUS pass must be CLOSED or EXPLICITLY CARRIED by this pass —
   step 0 of every cycle runs **`dream-pending-carry`** (rc=0 nothing owed ·
   rc=1 REAL/AMBIGUOUS markers owe a disposition · rc=2 instrument failure),
   and each REAL hit ends this cycle in exactly one of two states:
   (a) CLOSED — verdict recorded via `dream-log-amend` on the ORIGINAL entry,
   or (b) CARRIED — named in THIS cycle's entry with an explicit reason why
   the verify is still outstanding. Silent outcomes are the failure class this
   rule kills: a promised amend that never lands, or a doc silently
   SUBSTITUTED without a verdict/drop-reason on the original (the 7/16 12:39
   breach-tablet swap — 4 of 5 PENDINGs closed, the 5th replaced with no
   disposition). PENDINGs rot invisibly because nothing re-surfaces them; this
   contract makes pass N+1 the re-surfacer.**
   ⚠ **Do NOT hand-grep for `PENDING` — the prose remedy is SELF-WORSENING and
   that is exactly why the tool exists (2026-07-25).** The moment a cycle writes
   its verdict ("PENDING-carry: 0 real action-level markers") into `meta_audit`,
   that sentence BECOMES the next cycle's grep hit. Measured: all 6 prior entries
   carry 2–4 raw hits and the last 3 cycles each burned model tokens re-deriving
   the identical negative. `dream-pending-carry` classifies REAL (item-attached
   marker, or any attached marker in `actions.*`) vs META (carry-contract
   discourse) vs AMBIGUOUS (fail-closed → rc=1, surfaced for judgment), so the
   enumeration is deterministic and only genuine dispositions cost tokens.
   Selftest 9/9 incl. verbatim repros of the 0300 + 0647 entries. Enumeration is
   the tool's job; judgment is yours.
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
   (`duration_ms:0` is a PLACEHOLDER — `dream-log-append` overwrites it with the
   real wall-clock ms diffed from the Step-0 `dream-cycle-begin` marker, then
   consumes the marker; auditor #48. Keep emitting `0`, don't hand-compute it.)

   ⚠ **`escalations[].severity` encodes HOW URGENTLY ANOTHER DREAM CYCLE IS OWED —
   NOT how important the item is in the world (2026-07-27, measured).** Each entry
   must be a **dict with a `severity` key** (a bare string is refused: it crashes
   `dream-recent-clean`), and `dream-recent-clean` treats `high`/`critical` in the
   LATEST entry as *"a dirty lane demands the follow-up cycle"* — it blocks the
   redundant-cluster skip. So a **high** severity is a claim that *more dream work
   is owed and a follow-up cycle can do it*. An item **blocked on an external
   party** (a Mike-actionable ask, an upstream fix, a scheduled delivery window)
   fails that test by construction: no follow-up cycle can clear it, so scoring it
   `high` pins the lane dirty until the outsider acts and **turns a health
   indicator into a constant**. Score such items `medium` (or lower) and add
   `blocked_on` + `surface` keys naming who owns it and where it will be
   delivered; their real urgency rides the delivery queue (telegram, morning
   brief), which is the correct surface for an external ask. World-importance and
   cycle-urgency are different axes — this field is the second one.
   *Found by tripping it: this cycle filed the WSL `memory=12GB` ask as `high`,
   read back `recent but dirty`, and would have re-fired the lane every cycle
   until Mike edited a Windows file.*
   S4 path pin: the s4-initiatives ledger is
   `~/.local/state/alfred/areas/s4-initiatives/` (NOT `~/areas/` — stubs only).
   Approximations go in string fields (`"count_7d_approx":"~11"`), never a
   tilde in a numeric slot.

   **Queue-reconciliation gate (2026-07-02):** any ship this cycle
   (`substrate_fixes`, hook flip, ingest) that satisfies — fully or partially —
   a standing `~/areas/research-queue.md` item or a journal-head
   LARGE-TASK/Next-action premise → reconcile that row IN THIS CYCLE: mark
   `[x] CLOSED` (or annotate partial progress) with a one-line pointer to this
   dream session_id. Why load-bearing: the 6/27 enforce-flip of the
   curated-web-recall gate shipped out-of-band and left research-queue L12 +
   three downstream ORIENTs planning against a dead observe-mode premise
   (discovered 7/2 wake #8, a full LARGE-TASK slot spent re-deriving it). An
   out-of-band ship without queue reconciliation converts every downstream
   ORIENT into stale-premise planning.

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
