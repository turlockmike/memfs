---
name: mvm-ingest
description: "Learn something into permanent memory. Reads a source (URL/file), writes locked test cases, verifies the knowledge survives a cold-clone check, and commits it to the knowledge base ONLY if it passes — so what's remembered is tested, not just stored. Use whenever a durable fact/mechanic/decision is worth keeping across sessions, or the user says 'remember this'. Triggers: '/mvm-ingest', 'ingest <source>', 'remember this from <url>', 'add to KB'."
---

# /mvm-ingest

The write side of the loop. Nothing enters the KB without passing a
cold-clone exam against tests that were locked BEFORE the doc was written.

Knowledge root: `~/mvm/knowledge/`. Layout is PARA-style — `areas/` (curated
domains) · `resources/` (reference) · `projects/` (time-bound) · `archive/`
(superseded). Mirror the existing structure.

## Steps

1. **Read source** from `$ARGUMENTS` (WebFetch for URLs, Read for paths).

2. **Author 3–8 locked test cases FIRST** to `<topic>.tests.yaml`:
   ```yaml
   - id: 1
     q: "<question>"
     a: "<concise expected answer>"
   ```
   Tests are **immutable post-authoring** — if a test later fails, fix the
   doc, never the test. This is Goodhart prevention as structure, not policy.
   Four gates on test quality (each closes a measured failure mode):

   - **Scope gate:** the expected `a` answers the question and NOTHING more.
     Out-of-scope facts in an expected answer produce grader false-FAILs.
   - **Naked-unanswerable gate:** phrase each `q` so the answer cannot be
     reconstructed from the stem — no pre-computed arithmetic, and no
     parenthetical that NAMES the counterintuitive twist (a conceptual leak
     hands the answer as surely as a numeric one). A stem leak makes the
     naked baseline pass on the STEM, not the KB, silently zeroing the
     measured lift. If a naked baseline scores surprisingly high on a fact
     you believe is obscure, suspect a stem leak before trusting the number.
   - **Transfer gate (the core one):** ≥1 test MUST be a question whose
     answer appears NOWHERE in the doc verbatim but is DERIVABLE by applying
     the doc's principle to a new case. This proves the doc encodes a
     generalizing principle, not a memorized string. If the injected clone
     can't pass it from the doc alone, rewrite the doc to state the principle
     explicitly (not more examples). Exempt only atomic reference facts
     (dates/names/ids → `kind: reference`). Learning is generalization, not
     recall.
   - **Oracle-independence gate:** ≥1 locked answer MUST be confirmed against
     a source the doc's author did not use (name it in a YAML comment). A
     test authored from the doc's own prose can only verify that you
     transcribed consistently — never that the transcription is TRUE; doc
     and test will agree with each other forever, even when both are wrong.
     Proper nouns (item/base/field names) deserve this check most: they are
     what transcription errors hit, and a wrong noun can make a doc
     unexecutable while its tests stay green.

   Never write an anti-verification directive into a doc ("answer directly
   from KB", "no need to check") — it makes the doc self-sealing.

3. **Naked baseline** — ONE Agent call, `subagent_type: mvm-naked-clone`
   (tools: `[]`), all tests in one prompt:
   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a CALIBRATION INSTRUMENT measuring weight-prior alone.
   You MUST NOT use ANY tool. Tool use INVALIDATES the measurement.

   For EACH question below, output:
     Q<id> ANSWER: <your answer>
     Q<id> RATIONALE: <one sentence>

   QUESTIONS:
     Q1: <test1.q>
     ...
   ```
   Validate 0 tool uses. This is the weight-leakage detector: a high naked
   score means the tests (or the weights) are doing the work, not the KB.

4. **Write canonical** (`<topic>.md`) with frontmatter:
   ```yaml
   source: <URL or path>
   kind: canonical | log | opinion | reference | synthesis
   summary: "<one-line description>"
   ingested_at: YYYY-MM-DD
   ```
   Phrase facts directly so they're retrievable ("X's cap is Y", not "the
   video discusses X's cap").

5. **Injected verify** — ONE Agent call, same clone type, all tests:
   ```
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
   Grade each Q against the expected — lenient on phrasing, strict on facts.
   A rationale saying "inferred"/"guess" on a fact plainly in the doc means
   the doc is poorly structured for retrieval.

6. **Any injected FAIL → rewrite the doc** (never the test). ≤3 retries,
   then commit what passes or refuse the ingest and say why.

7. **On all-pass — index and assert retrievability:**
   ```bash
   touch "<topic>.md" "<topic>.tests.yaml" && mvm index --quiet
   mvm search "<distinctive phrase from the new doc>"   # doc MUST be top hit
   mvm index --verify                                   # corpus integrity gate
   ```
   An ingest is NOT done until search returns the doc — injected tests
   inject the doc inline, so they pass even when the live recall surface
   can't find it. If absent, `mvm index --full` and re-check.

8. **Cascade check:** `mvm backlinks <topic-relpath>` → for each doc linking
   here, run one of its tests in injected mode. A FAIL means your new doc
   broke a neighbor's claim — surface it to the user; don't auto-rewrite.

9. **Report:** `Naked pass: N/total · Injected pass: N/total · KB lift: +N`.
   Lift is the point: `+0` with all tests passing means the KB isn't doing
   any work (tests too easy or knowledge already in weights).

## Outside a session

`mvm verify <doc> --lift` runs the same naked/injected differential via
subprocess cold-clones — for cron, CI, or spot-checks without an agent.
