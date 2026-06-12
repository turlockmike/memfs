---
name: mvm-ingest
description: "MVM closed-loop write. Reads a source (URL/file), authors locked test cases, runs naked baseline + injected verify (Agent-tool cold-clones, model: haiku), commits canonical only if all tests pass. Triggers: '/mvm-ingest', 'ingest <source>', 'add to mvm KB', 'remember this from <url>', 'capture <source>'."
---

# /mvm-ingest

Knowledge root: `~/mvm/knowledge/`. History & rationale for every gate here:
`~/resources/mvm-incident-history.md`.

## Layout (bootstrap if missing)

PARA-style: `_meta/` (user-model, domains, INDEX) · `projects/` (time-bound) ·
`areas/` (curated domains — KB-canonical scope) · `resources/` (reference) ·
`archive/` (superseded). Curated domain → `areas/<domain>/<topic>.md`;
reference → `resources/<topic>.md`. Mirror existing structure.

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
   seconds total — the old two-phase --no-embed/--embed-only dance is obsolete):
   ```bash
   mvm index --quiet      # delta-only: FTS + embedding for the new doc, ~5s
   ```
   Report: `Naked pass: N/total · Injected pass: N/total · KB lift: +N`.

9. **Cascade check:** `mvm backlinks <topic-relpath>` → for each linker, run
   one random test from `<linker>.tests.yaml` in injected mode (haiku
   cold-clone). FAIL → surface to user: "ingest of <topic> broke <linker> test
   <id> — review or rewrite <linker>." No automatic rewrite; user decides.
