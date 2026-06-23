---
name: ingest
description: "Learn something new into my permanent memory. Reads a source (URL/file), writes locked test cases, verifies the knowledge survives a cold-clone check, and commits it to my knowledge base ONLY if it passes — so what I remember is tested, not just stored. Use whenever I learn a durable fact/mechanic/decision worth keeping across sessions, or Mike says 'remember this' / 'capture this'. Triggers: '/ingest', 'ingest <source>', 'remember this from <url>', 'capture <source>', 'add to KB'."
---

# /ingest

Knowledge root: `~/mvm/knowledge/`. History & rationale for every gate here:
`~/resources/mvm-incident-history.md`.

## Layout (bootstrap if missing)

PARA-style: `_meta/` (user-model, domains, INDEX) · `projects/` (time-bound) ·
`areas/` (curated domains — KB-canonical scope) · `resources/` (reference) ·
`archive/` (superseded). Curated domain → `areas/<domain>/<topic>.md`;
reference → `resources/<topic>.md`. Mirror existing structure.

**Write surface — author DIRECTLY under `~/mvm/knowledge/...`, never `~/resources/...`
(learned 2026-06-14, costs 2× when violated).** `~/mvm/knowledge/` is this skill's
native surface; the `mvm-mirror` cron (`*/5`) carries the canonical `.md` BACK to
`~/resources/`. Authoring on the `~/resources` side breaks the loop two ways:
(1) **`mvm-mirror` syncs only `*.md`, never `*.tests.yaml`** → the locked tests never
reach the mvm side, so injected-verify/cascade run against a missing file (you'd have
to `cp -p` the tests by hand); (2) the mirror **preserves original mtime** via `cp -p`,
which can make a CHANGED doc's mtime collide with the recorded value → the incremental
index's `mtime != recorded` change-detector compares EQUAL and **silently SKIPS the
update** (the doc indexes stale; defeated by `touch`-before-index, see step 8). Author
both `.md` and `.tests.yaml` under `~/mvm/knowledge/...`, then index (step 8) — the
mirror handles `~/resources/`.

## Steps

1. **Read source** from `$ARGUMENTS` (WebFetch for URLs, Read for paths).

2. **Author 3–8 locked test cases** to `<topic>.tests.yaml`:
   ```yaml
   - id: 1
     q: "<question>"
     a: "<concise expected answer>"
   ```
   Tests are immutable post-authoring. Every test has a real expected answer
   (no DONT-KNOW expecteds). Hallucination = high confidence + wrong answer.
   **Question-scope gate (auditor-mandated, dream-20260610):** the expected
   `a` must answer the question and NOTHING MORE — facts beyond the
   question's scope (extra times, adjacent events, provenance asides) belong
   in the canonical doc, not the expected, because out-of-scope EXPECTED
   facts produce grader false-FAILs (8/11 in the 06-10 cycle were this
   artifact, class: grader-question-blindness).
   **Naked-unanswerable gate (learned G5/Phase-F, 2026-06-14):** phrase each
   `q` so the answer CANNOT be reconstructed from the stem — never hand
   partial computation, intermediate steps, or the arithmetic that leads to
   the answer inside the question, because a stem that pre-computes makes the
   naked baseline pass on the STEM, not the KB, so the test stops measuring
   KB lift. Target a counterintuitive game-/domain-truth whose naked baseline
   is low; a HIGH naked baseline means the test (not the KB) is doing the
   work → re-tighten the stem ANSWER-identically (HC4) and re-run the
   baseline before locking. The 2026-06-14 Phase-F cycle caught exactly this
   in flight: Q1 leaked "+3 at 50%=+4, +4 at 40%=+5" in the stem, naked
   reconstructed it; tightened to stop handing the math, baseline re-ran clean.
   **The leak is CONCEPTUAL too, not just numeric (learned J6, 2026-06-14):** a
   parenthetical that NAMES the counterintuitive twist hands the answer just as
   surely as pre-computed arithmetic. J6's stem said "the side it occupies, not
   the side it buffs" — that aside telegraphed the whole inversion, so naked
   scored a FALSE 5/5 (passing on the trap-naming, not the KB). A clean-stem
   re-probe gave the true DON'T-KNOW → real +5 lift. Rule: a `q` testing a
   counterintuitive truth must NEVER name, hint, or frame the twist itself —
   ask the plain question and let the naked clone fall into the trap. When a
   naked baseline scores SURPRISINGLY HIGH on a fact you believe is obscure,
   suspect a stem leak (numeric OR conceptual) before trusting the number; a
   leaked baseline silently zeroes out the measured lift.
   **Generalization gate (Mike 2026-06-14 — the core upgrade): ≥1 test MUST be
   a TRANSFER item** — a question whose answer is stated NOWHERE in the doc
   verbatim, but is DERIVABLE by applying the doc's principle to a NEW case.
   Stronger than naked-unanswerable (which only bars stem-leak): it proves the
   doc encodes a generalizing PRINCIPLE, not a memorized string. The injected
   clone must PASS the transfer item from the doc alone; if it can't, the doc
   stores facts but not the rule → rewrite to state the underlying principle
   explicitly (not more examples). If you CANNOT author a transfer question, the
   knowledge is either (a) atomic reference — a date/name/id → `kind: reference`,
   exempt — or (b) a recipe at too-low altitude → the altitude gate (2.5) fires.
   Learning is generalization, not recall (Mike: "learning isn't just write a
   document and see if you can recall it").

2.5 **Altitude gate — fundamental vs composite (Mike 2026-06-14).** Before
   writing the canonical, classify the target knowledge. Smell test: *does it
   rest on ≥2 other mechanics that, if known, would let me re-derive it?*
   - **Fundamental** — an atomic mechanic/rule/relationship that other facts
     derive FROM (e.g. "quality display truncates: floor(base×(1+q))"; "removing
     a 'can have additional modifier' mod does not strip the existing extra
     mod"). Store directly; proceed to step 3 (transfer gate already enforces
     generalization).
   - **Composite / recipe** — a multi-step METHOD reaching a specific named
     outcome that rests on ≥2 fundamentals (e.g. "+6 melee amulet", "5-mod jewel
     lock-trick"). Storing it as a doc and recall-testing it is the CHEAT (a
     lookup entry that grades its own recall). Route to the sub-protocol:
     a. **Decompose** into the fundamentals it rests on; list them. Track in
        `~/projects/<name>/program.md` — composites are explicitly MULTI-SESSION.
     b. For each fundamental not already a passing KB doc, recursively
        /mvm-ingest it AS A FUNDAMENTAL (this loop, each its own locked tests).
     c. **Cheat-prevention invariant:** no recipe/method doc on the recall
        surface (`~/mvm/knowledge/`, `~/resources/`) until the exam passes.
        Strip any recipe doc to background facts; archive the full recipe as the
        ground-truth ANCHOR (off the mvm-kb-clone surface).
     d. **Held-out derivation exam:** spawn `mvm-kb-clone` (KB-only, cold) with
        the OUTCOME question and the recipe doc ABSENT. PASS = it reconstructs
        the method citing only fundamentals docs. (This is a held-out
        generalization test — the real proof, vs single-doc recall.)
     e. On PASS: optionally add an APPLIED doc as a worked, cross-referencing
        application — NOT a standalone lookup, and the exam must not depend on it.
     f. On FAIL: the missed step IS the next fundamental gap → ingest it,
        re-exam. No hints in the prompt, no partial credit.
   Worked templates: `~/archive/poe2-kb-fundamentals/program.md` (+6 amulet,
   COMPLETE) · `~/projects/poe2-jewel-lock-trick/program.md` (active).

3. **Naked baseline** — ONE Agent call, `subagent_type: mvm-naked-clone`
   (tools: [] structural), `model: haiku`, ALL tests in one prompt:
   ```
   prompt: |
     [CRITICAL CONSTRAINTS — READ FIRST]
     You are a CALIBRATION INSTRUMENT measuring weight-prior alone.
     You MUST NOT use ANY tool. Tool use INVALIDATES the measurement.
     If you would normally reach for a tool, DO NOT. Answer from prior alone.

     For EACH question below, output:
       Q<id> ANSWER: <your answer>
       Q<id> RATIONALE: <one sentence; if you wanted a tool, name it here>

     QUESTIONS:
       Q1: <test1.q>
       ...
   ```
   Validation: task notification `tool_uses` must be 0, else re-spawn stricter
   or mark the measurement failed.

4. **Write canonical** (`<topic>.md`) with frontmatter:
   ```yaml
   source: <URL or path>
   kind: canonical | log | opinion | reference | synthesis
   summary: "<one-line, 100-200 char description>"
   ingested_at: YYYY-MM-DD
   ```
   Phrase facts directly so they're retrievable ("X publicly stated 'Y'", not
   "X posted on Z"). `summary:` is required (`mvm index` builds INDEX.md from
   it; never hand-edit INDEX.md — derived view).

5. **PoE2 patch-state gate — HARD GATE** when source/canonical references PoE2
   mechanics or path contains `poe2`:
   ```bash
   poe-doctrine-patch-check <canonical-path>
   ```
   Exit 0 → proceed. Exit 1 → do NOT commit; rewrite per step 7 (≤3 retries):
   fix stale hits via **cite-don't-restate** — reference the changelog entry
   (`~/resources/poe2/<patch>/patches/<changelog>:L<N>`) with a 🚨
   STALE-MECHANIC banner instead of restating dead-mechanic prose (banner'd
   hits auto-suppress within the gate's 3-line ack window). Persistent fails →
   REFUSE the ingest, surface the gate output verbatim. False positives → fix
   the kill-list at the script source, never work around the gate.

6. **Injected verify** — ONE Agent call, same `mvm-naked-clone` + haiku, ALL
   tests in one prompt:
   ```
   prompt: |
     Answer from the document below only. No tools, no prior knowledge.

     DOCUMENT:
     ---
     <full canonical content>
     ---

     For EACH question below, output:
       Q<id> ANSWER: <answer drawn from the document>
       Q<id> RATIONALE: <one sentence on the supporting passage>

     QUESTIONS: ...
   ```
   Grade each Q against expected — lenient on phrasing, strict on facts. A
   RATIONALE saying "inferred"/"guess" on a fact plainly in the doc = the doc
   is poorly structured for retrieval.

7. **Any injected FAIL → rewrite the doc** (never the test). ≤3 retries, then
   commit or refuse.

8. **On all-pass — single incremental index** (since 2026-06-10 `mvm index`
   is incremental by default: only the new/changed doc is parsed + embedded,
   seconds total — the old two-phase --no-embed/--embed-only dance is obsolete).
   **`touch` the doc + its tests FIRST, then index** — this is the happy path, not
   a recovery step. The incremental indexer change-detects via `_doc_mtime(p) !=
   recorded_mtime` (max of `.md` and `.tests.yaml`); `mvm-mirror`'s `cp -p`
   preserves source mtime, so a same-second/reset-mtime *collision* makes a changed
   doc compare EQUAL → silently SKIPPED (verified 2026-06-15: changed content +
   mtime reset to the recorded value → update missed). `touch` forces mtime strictly
   past any recorded value, so the change is always detected:
   ```bash
   touch "<doc>.md" "<doc>.tests.yaml" && mvm index --quiet   # delta-only, ~5s
   ```
   (Note: the old "watermark skips NEW docs" model is FALSE for the current indexer —
   genuinely new docs are added by set-membership regardless of mtime. The real
   residual race is the mtime-COLLISION on CHANGED docs, which `touch` defeats.)
   **Then ASSERT retrievability (the warning above is not a catch — this is):**
   ```bash
   mvm search "<distinctive phrase from the new doc>"   # the doc MUST be the top hit, sem≈1.0
   ```
   If the doc is absent / not top hit, run `mvm index --full` (full rebuild ignores
   mtime entirely). An ingest is NOT done until search returns the doc — injected
   tests inject the doc inline, so they pass even when the live recall surface can't
   find it.
   Report: `Naked pass: N/total · Injected pass: N/total · KB lift: +N`.

9. **Cascade check:** `mvm backlinks <topic-relpath>` → for each linker, run
   one random test from `<linker>.tests.yaml` in injected mode (haiku
   cold-clone). FAIL → surface to user: "ingest of <topic> broke <linker> test
   <id> — review or rewrite <linker>." No automatic rewrite; user decides.
