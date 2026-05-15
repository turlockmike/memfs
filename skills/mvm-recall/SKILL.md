---
name: mvm-recall
description: "MVM unified question-answering. Spawns 3 parallel haiku probes (naked / KB / web), reconciles via trust hierarchy KB>web>weights, auto-spawns /mvm-ingest on web hits, logs every recall to ~/mvm/state/recall-log.jsonl. Triggers: '/mvm-recall', 'recall <question>', any factual question routed to mvm."
---

# /mvm-recall

## Steps

1. **Spawn 3 parallel Agent calls (`model: haiku`, all in one batch). Tool restriction is STRUCTURAL via dedicated subagent_type — not prompt-level.** Use `subagent_type: mvm-naked-clone` (tools: []), `subagent_type: mvm-kb-clone` (tools: [Bash, Read, Grep]), `subagent_type: mvm-web-clone` (tools: [WebSearch, WebFetch]). Each agent's frontmatter enforces its tool envelope at the harness level — they can't break it even if the prompt is buggy. Prompt-level CRITICAL CONSTRAINTS blocks are still useful as a second layer (and to forbid prior-knowledge use, which isn't a tool), but tool gating is structural now. The orchestrator still validates tool-use counts as a sanity assert; structural enforcement should make that always pass. (Switched from `general-purpose` 2026-05-09 telegram-1851 — recall-log entry #2 'contaminated probe — did web/KB' was the smoking gun for prompt-only restriction failure.)

   **Naked probe prompt:**
   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a CALIBRATION INSTRUMENT measuring weight-prior alone.
   You MUST NOT use ANY tool. Forbidden: Bash, Read, Grep, Glob, WebSearch, WebFetch,
   mvm search, mvm verify, Skill, Agent, ToolSearch — and any other tool.
   Tool use INVALIDATES the measurement and breaks the system.
   If you would normally reach for a tool, DO NOT. Note in RATIONALE that you would
   have used <X> but didn't, and answer from prior alone — even if uncertain.

   QUESTION: <q>

   Output exactly:
   ANSWER: <your best answer from prior knowledge alone>
   RATIONALE: <one sentence; if you wanted a tool, name it here>
   ```

   **KB probe prompt:**
   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a KB RETRIEVAL INSTRUMENT.
   ALLOWED tools: ONLY Bash for `mvm search` (no other Bash commands), Read on files
   inside ~/mvm/knowledge/, Grep on files inside ~/mvm/knowledge/.
   FORBIDDEN: any other Bash; WebSearch; WebFetch; any web tool; Skill; Agent; ToolSearch;
   prior knowledge.
   HARD CAP: 3 tool calls total. Stop after 3 even if you haven't found the answer.
   If KB doesn't contain the answer, output a refusal with that explicit signal.

   Procedure:
   1. ONE `mvm search "<query>"` to find candidates.
   2. Read top 2 candidates if needed.
   3. If candidates conflict on this question, list each one's claim in RATIONALE.

   QUESTION: <q>

   Output exactly:
   ANSWER: <best answer drawn from KB, or refusal>
   RATIONALE: <cite file path(s) and passage; note conflicts>
   ```

   **Web probe prompt:**
   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a WEB RETRIEVAL INSTRUMENT.
   ALLOWED tools: ONLY WebSearch and WebFetch.
   FORBIDDEN: Bash; Read; Grep; Glob; mvm search; any KB access; Skill; Agent;
   ToolSearch; prior knowledge.
   HARD CAP: 3 tool calls total. Typically: 1 WebSearch + 1 WebFetch (top result).
   If inconclusive, ONE more WebFetch on the second result. Then answer or refuse.

   QUESTION: <q>

   Output exactly:
   ANSWER: <best answer from fetched source>
   RATIONALE: <one sentence including the source URL>
   ```

   **Orchestrator validation:** when reading each probe's response, check the reported tool-use count (visible in the Agent task notification). If naked > 0 tool calls or any probe exceeds 3, treat that probe's result as invalid and re-spawn with even stricter wording, OR mark the probe failed and note in reconciliation.

2. **Reconcile** by reading all three results. Trust hierarchy: **KB > web > weights**. The rationale is the diagnostic — it tells you whether each probe actually had grounding or was guessing.

   - **KB rationale cites a file/passage** → KB grounded; trust it (default).
   - **KB rationale says "no relevant doc found" or similar** → KB silent; fall through to web.
   - **Web rationale cites a URL with fresher-source markers (date, "as of", "updated") that contradicts KB** → web wins; spawn `/mvm-ingest` to supersede.
   - **All three rationales say "guess" or "no information"** → hard miss; refuse with gap report.
   - **Three different high-confidence answers each with seemingly grounded rationales** → contested; surface all three; log to `contested.jsonl`; don't pick.

   Curated-domain override: if the question is in a declared curated domain, KB always wins over web/weights as long as it has grounded rationale.

3. **Auto-spawn `/mvm-ingest`** in background (`run_in_background: true`) when web answered but KB didn't, OR when web supersedes stale KB. Pass `q`, `a`, source URL as a seed.

4. **Append to `~/mvm/state/recall-log.jsonl`** (single-line JSON per recall):
   ```json
   {"ts":"...", "session_id":"...", "question":"...", "topic_hint":"...",
    "probes":{"naked":{"ans":"...","rationale":"..."},
              "kb":{"ans":"...","rationale":"...","top_score":N},
              "web":{"ans":"...","rationale":"...","url":"..."}},
    "reconciliation_pattern":"...", "decided_source":"kb|web|weights|none",
    "decided_answer":"...", "ingested":bool, "ingested_path":"...",
    "duration_ms":N}
   ```

5. **Output to user:**
   ```
   <answer>

   — from <kb-path | url | weights>  reconciliation: <pattern>
   [substrate updated: <what changed>]
   ```

6. **Substrate-confusion signal — handle walkback as a substrate-update event.**
   If the user pushes back on a confident claim during this turn ("wait, who said that?", "are you sure?", "where did that come from?") and you have to **revise the answer** after re-checking, that revision is NOT just a conversational correction — it's evidence the substrate enabled the conflation by commingling things that should be visually separable (e.g., GGG-direct quote vs creator-speculation; old version vs new; observation vs prediction).

   Before exit:
   1. Identify the offending source — the canonical doc that contained the confusable content.
   2. Identify the conflation — name the two things that got mixed.
   3. Spawn `/mvm-ingest` in rewrite mode on that source doc. Goal: restructure the canonical so the distinction is structural (separate sections with clear epistemic-status labels like `## GGG-direct (Tier-1 confirmed)` vs `## Creator predictions (uncorroborated)`).
   4. Author a NEW locked test case that specifically probes the conflation, with expected answer naming the distinction. This prevents silent recurrence.
   5. Run injected verify on the new test plus existing tests (doc was edited; cascade verifies everything still passes).
   6. In the recall-log entry for this turn, set `substrate_confusion: {original_answer, revised_answer, source_path}`.

   FEP-clean handling: prediction error during recall must drive a substrate update OR dissolve via cross-check. Don't apologize and move on — close the loop.
