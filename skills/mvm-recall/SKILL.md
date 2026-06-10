---
name: mvm-recall
description: "MVM unified question-answering. Step 0 memo-cache, then KB + web retrieval INLINE (orchestrator searches itself) plus ONE naked-baseline haiku probe for prior-contamination signal; reconciles via trust hierarchy KB>web>weights, auto-spawns /mvm-ingest on web hits, logs every recall via `recall-log add`. Triggers: '/mvm-recall', 'recall <question>', any factual question routed to mvm."
---

# /mvm-recall

Architecture: KB and web retrieval run **inline** (you search); only the naked
probe is a subagent (weight-prior needs an uncontaminated context). History &
rationale for every rule here: `~/resources/mvm-incident-history.md`.

## Steps

0. **Memo-cache.** `recall-log cache-lookup "<question>"`. On a hit with
   `sim ≥ 0.7` whose answer you judge still fresh (check `age_days` against
   domain velocity — PoE2 patch facts go stale in days; biography doesn't):
   serve it, log with `decided_source:"cache"` + `source_detail` naming the
   original ts, output `— from cache (age Nd)`, done. Stale-suspect or
   low-sim → full pipeline below.

1. **Naked probe — the only subagent.** Spawn in the SAME message as your
   first inline search so it runs concurrently. `subagent_type:
   mvm-naked-clone` (tools: [], structural), `model: haiku`. Validation: task
   notification must show `tool_uses: 0`, else the measurement is invalid —
   re-spawn stricter or mark the probe failed.

   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a CALIBRATION INSTRUMENT measuring weight-prior alone.
   You MUST NOT use ANY tool. Forbidden: Bash, Read, Grep, Glob, WebSearch,
   WebFetch, mvm search, mvm verify, Skill, Agent, ToolSearch — and any other
   tool. Tool use INVALIDATES the measurement and breaks the system.
   If you would normally reach for a tool, DO NOT. Note in RATIONALE that you
   would have used <X> but didn't, and answer from prior alone — even if
   uncertain.

   QUESTION: <q>

   Output exactly:
   ANSWER: <your best answer from prior knowledge alone>
   RATIONALE: <one sentence; if you wanted a tool, name it here>
   ```

2. **KB retrieval — inline.** Surface: `~/mvm/knowledge/{resources,areas,topics,topics-alfred-state}/`.
   `knowledge/projects/` is NOT a surface — project deliverables are direct-Read
   from `~/projects/` (a hit there = KB-miss).

   Search doctrine (eval-backed, exp5 2026-06-09): the engine is vector-first
   with FTS fallback. Issue `mvm search "<question verbatim>"` AND up to 2
   tight key-term queries (proper nouns / distinctive phrases), **merge the
   results** — each style uniquely recovers docs the other misses (union
   17/27 vs 13/27 either alone). Read the best candidate if snippets are
   insufficient. Conclude KB-miss only after both styles come up empty.
   Capture `kb.ans` + `kb.rationale` citing the path. Keep it ≤4 retrieval
   calls. If candidates conflict, record each claim in the rationale.

3. **Web retrieval — inline, by default.** ~1 WebSearch + 1 WebFetch on the
   top result (one extra if inconclusive); capture `web.ans` + `web.rationale`
   + URL. Skip ONLY when KB returned a grounded hit in a declared curated
   domain AND you have no staleness concern — web is how supersession is
   detected. Discount SEO/gold-seller domains (aoeah, u4gm, mmoexp, …).

4. **Reconcile.** Trust hierarchy **KB > web > weights**; the rationale is the
   diagnostic (grounded vs guessing).
   - KB rationale cites a file/passage → KB grounded; trust it (default).
   - KB silent → fall through to web.
   - Web cites a fresher source (date markers) contradicting KB → web wins;
     spawn `/mvm-ingest` to supersede.
   - All three guessing → hard miss; refuse with gap report,
     `decided_source:"none"`.
   - Three different confidently-grounded answers → contested; surface all,
     `decided_source:"contested"`, don't pick.
   - Curated-domain override: KB with grounded rationale always beats
     web/weights.

5. **Auto-spawn `/mvm-ingest`** in background (`run_in_background: true`) when
   web answered and KB didn't, or web superseded stale KB. Pass q, a, URL.

6. **Log — mandatory, via the CLI** (the fail-closed curated-write gate and
   the Stop hook key off this ledger):
   ```bash
   echo '{"question":"...","topic_hint":"...",
    "probes":{"naked":{"ans":"...","rationale":"..."},
              "kb":{"ans":"...","rationale":"...","top_score":N},
              "web":{"ans":"...","rationale":"...","url":"..."}},
    "reconciliation_pattern":"...",
    "decided_source":"kb|web|weights|engine|contested|cache|mixed|none",
    "source_detail":"<free-text provenance if compound>",
    "evidence_paths":["resources/..."],
    "decided_answer":"...","duration_ms":N}' | recall-log add
   ```
   `decided_source` is a hard enum (CLI rejects anything else; prose goes in
   `source_detail`). `evidence_paths` = KB docs that grounded the answer
   (feeds retrieval-heat test coverage). ts/session_id/kind auto-fill.

7. **Output:**
   ```
   <answer>

   — from <kb-path | url | cache | weights>  reconciliation: <pattern>
   [substrate updated: <what changed>]
   ```

8. **Substrate-confusion signal.** If the user's pushback this turn forces a
   revision of a confident claim, the substrate enabled a conflation — close
   the loop before exit: (a) identify the offending canonical and the two
   things it commingles; (b) spawn `/mvm-ingest` rewrite-mode to make the
   distinction structural (separate sections with epistemic-status labels);
   (c) author a NEW locked test probing exactly that conflation; (d) run
   injected verify on new + existing tests; (e) set
   `substrate_confusion: {original_answer, revised_answer, source_path}` in
   the recall-log entry. Prediction error drives a substrate update or
   dissolves via cross-check — never apologize-and-move-on.
