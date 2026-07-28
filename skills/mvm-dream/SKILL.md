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
     **The pair test — ALL FOUR must hold (no similarity number anywhere):**
     1. **Same `kind`** (`metadata.kind` in the JSON) — a `recipe` and a `decision`
        cannot contradict; they answer different questions.
     2. **Same declared version, or both version-less.** A `0.4` doc and a `0.5`
        doc disagreeing is correct versioning, not tension — this was the dominant
        historical fire class (see below), so it is now a PRECONDITION, not an
        after-the-fact discriminator.
     3. **≥2 shared significant title terms** (ignore stop-words + `index`/`notes`).
        Cheap proxy for "these make claims about the same subject."
     4. **Neither doc is an AGGREGATOR** — ⛔ skip any path under `areas/` matching
        `backlog|research-queue|dashboard`, or any file >40 KB. Grab-bag files pair
        with everything (measured: `research-queue-cold.md` ↔ `backlog.md` = **0.9805**
        cosine, *higher* than a real near-dup pair) and they are the single largest
        source of false fires.
     **Done-test for this rung — apply rule 4 DIRECTLY to the named pair; do NOT wait
     for a rotation to produce it** (repaired 2026-07-27, see below): the pair
     `areas/research-queue-cold.md` + `areas/research-queue.md` must be REJECTED
     (rule 4 — both live, both mirrored, both match the `research-queue` path pattern
     under `areas/`; the PATH-PATTERN clause is what rejects them, NOT the size clause.
     ⚠ measured 2026-07-27: `research-queue-cold.md` is **342 B**, so an earlier version
     of this line claiming "both >40 KB" was FALSE — `research-queue.md` is 74,425 B but
     its partner is tiny. A done-test whose stated RATIONALE is wrong still passes for
     the wrong reason, which is how a rule quietly stops meaning what it says; cite the
     clause that actually fires), and the pair `poe2/0.5/crafting/jewellery-quality-system.md` +
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
