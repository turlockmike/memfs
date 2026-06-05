---
name: mvm-recall
description: "MVM unified question-answering. Runs KB + web retrieval INLINE (orchestrator does the searches itself) plus ONE naked-baseline haiku probe for prior-contamination signal; reconciles via trust hierarchy KB>web>weights, auto-spawns /mvm-ingest on web hits, logs every recall to ~/mvm/state/recall-log.jsonl. Triggers: '/mvm-recall', 'recall <question>', any factual question routed to mvm."
---

# /mvm-recall

## Steps

**Architecture (changed 2026-06-04 S753, Mike-requested):** KB and web retrieval are run **INLINE by the orchestrator** (you do the `mvm search` / `WebSearch` calls yourself in this turn). Only the **naked baseline stays a subagent** — measuring weight-prior requires an isolated, uncontaminated context, and your own context is contaminated the moment you do the inline KB/web searches. So: one subagent (naked), two inline retrieval passes (KB, web). The trust hierarchy, reconciliation, ingest-on-supersession, and logging are unchanged.

1. **Spawn the naked probe FIRST — the only subagent.** Issue it in the SAME message as your first inline KB `mvm search` so it runs concurrently. `subagent_type: mvm-naked-clone` (tools: [], structural), `model: haiku`. Its job is the contamination signal: it tells you whether your weight-prior alone would have answered (and whether that prior is wrong/PoE1-contaminated/etc.). **Validation:** its task notification must show `tool_uses: 0`; if > 0, the measurement is invalid — re-spawn with stricter wording or mark the probe failed in reconciliation.

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

2. **KB retrieval — INLINE (you run it; no subagent).** Apply this procedure yourself with your own Bash/Read/Grep, capturing a `kb.ans` + `kb.rationale` (with cited path) for the log.

   **KB SURFACE SCOPE** (decided 2026-05-15, sync-gap-doctrine.md): the recall KB surface is `~/mvm/knowledge/resources/` (canonical mechanics — mirrored & current via mvm-mirror) plus `~/mvm/knowledge/{areas,topics,topics-alfred-state}/`. `~/mvm/knowledge/projects/` is NOT a recall surface and no longer exists: project deliverables are retrieved by direct Read of `~/projects/<path>`, never recalled. If the question targets a project deliverable / recommendation doc, treat KB as a miss and Read the `~/projects/` path directly — never treat a stale project copy as a KB hit.

   Procedure:
   1. `mvm search` is a TEXT-MATCH index, NOT semantic — a verbose multi-term query (e.g. "PoE2 0.5 league name expansion patch version") scores near-zero and produces a FALSE KB-miss even when the fact is in substrate. FIRST extract 1–3 TIGHT key terms — proper nouns, distinctive multi-word phrases, canonical entity names (e.g. "Runes of Aldur", "Ice Shot Deadeye") — and issue ONE `mvm search "<tight term>"` per term (up to 3). Tight key-term queries hit text=1.000; verbose strings do not. Only if no tight term exists, fall back to one trimmed noun-phrase query.
   2. Read the single best candidate if the snippet is insufficient. Ignore any candidate under a `knowledge/projects/` path (stale-snapshot artifact) — treat as KB-miss.
   3. Conclude a KB-miss ONLY after tight key-term queries also came up empty — a verbose-query zero-hit is NOT sufficient evidence of a real gap.
   4. If candidates conflict, record each one's claim in the rationale.

   Keep it tight — aim for ≤4 retrieval calls (the old subagent cap, now a self-discipline, not a hard wall since you control the budget).

3. **Web retrieval — INLINE (you run it; no subagent).** Run web **by default** with your own WebSearch/WebFetch (~1 WebSearch + 1 WebFetch on the top result; one extra WebFetch if inconclusive), capturing `web.ans` + `web.rationale` (with URL). **You MAY skip web** only when KB returns a grounded hit in a declared curated domain AND you have no staleness concern (KB wins there regardless, per the curated-domain override) — but still run web whenever you suspect the KB doc may be stale, since web is how supersession (step 4) is detected. Note: low-trust SEO/gold-seller domains (aoeah, u4gm, mmoexp, etc.) are weak sources — discount them, especially against a grounded KB hit.

4. **Reconcile** across the naked probe result and your inline KB + web findings. Trust hierarchy: **KB > web > weights**. The rationale is the diagnostic — it tells you whether each source actually had grounding or was guessing.

   - **KB rationale cites a file/passage** → KB grounded; trust it (default).
   - **KB rationale says "no relevant doc found" or similar** → KB silent; fall through to web.
   - **Web rationale cites a URL with fresher-source markers (date, "as of", "updated") that contradicts KB** → web wins; spawn `/mvm-ingest` to supersede.
   - **All three rationales say "guess" or "no information"** → hard miss; refuse with gap report.
   - **Three different high-confidence answers each with seemingly grounded rationales** → contested; surface all three; log to `contested.jsonl`; don't pick.

   Curated-domain override: if the question is in a declared curated domain, KB always wins over web/weights as long as it has grounded rationale.

5. **Auto-spawn `/mvm-ingest`** in background (`run_in_background: true`) when web answered but KB didn't, OR when web supersedes stale KB. Pass `q`, `a`, source URL as a seed.

6. **Log via the `recall-log` CLI — NOT a hand-built file append.** This step is
   mandatory and non-skippable: the fail-closed `pretool-curated-write-gate`
   uses this ledger as its ONLY evidence source, and a stop-hook
   (`stop-recall-log-enforce.sh`) will BLOCK session end if a recall ran without
   a logged entry. Pipe the reconciled entry as one JSON object on stdin:
   ```bash
   echo '{"question":"...", "topic_hint":"...",
    "probes":{"naked":{"ans":"...","rationale":"..."},
              "kb":{"ans":"...","rationale":"...","top_score":N},
              "web":{"ans":"...","rationale":"...","url":"..."}},
    "reconciliation_pattern":"...", "decided_source":"kb|web|weights|none",
    "decided_answer":"...", "ingested":false, "ingested_path":null,
    "duration_ms":N}' | recall-log add
   ```
   The CLI auto-fills `ts` (local-tz now) and `session_id`, validates that
   `question`+`decided_answer` are present, and appends atomically (flock).
   Required keys: `question`, `decided_answer`. Everything else is optional but
   include `topic_hint` (the gate matches its curated subdomain token against it).
   Why a CLI and not a prose append: the prose step was silently dropped across
   sessions (ledger frozen 2026-05-25..27, ≥2 confirmed unlogged recalls →
   auditor cycle #172). A single command can't be malformed and is enforced.

7. **Output to user:**
   ```
   <answer>

   — from <kb-path | url | weights>  reconciliation: <pattern>
   [substrate updated: <what changed>]
   ```

8. **Substrate-confusion signal — handle walkback as a substrate-update event.**
   If the user pushes back on a confident claim during this turn ("wait, who said that?", "are you sure?", "where did that come from?") and you have to **revise the answer** after re-checking, that revision is NOT just a conversational correction — it's evidence the substrate enabled the conflation by commingling things that should be visually separable (e.g., GGG-direct quote vs creator-speculation; old version vs new; observation vs prediction).

   Before exit:
   1. Identify the offending source — the canonical doc that contained the confusable content.
   2. Identify the conflation — name the two things that got mixed.
   3. Spawn `/mvm-ingest` in rewrite mode on that source doc. Goal: restructure the canonical so the distinction is structural (separate sections with clear epistemic-status labels like `## GGG-direct (Tier-1 confirmed)` vs `## Creator predictions (uncorroborated)`).
   4. Author a NEW locked test case that specifically probes the conflation, with expected answer naming the distinction. This prevents silent recurrence.
   5. Run injected verify on the new test plus existing tests (doc was edited; cascade verifies everything still passes).
   6. In the recall-log entry for this turn, set `substrate_confusion: {original_answer, revised_answer, source_path}`.

   FEP-clean handling: prediction error during recall must drive a substrate update OR dissolve via cross-check. Don't apologize and move on — close the loop.
