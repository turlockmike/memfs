---
name: recall
description: "Recall what I already know BEFORE answering. Searches my own knowledge base first, then the web, reconciles them (my KB beats web beats raw memory), and logs the result. USE THIS FIRST for any factual question — and it is MANDATORY, not a judgment call, for curated domains: PoE2 (builds/items/crafting/mechanics/currency), finance/budget/Monarch, Kalshi, home-device state. Whenever I'm about to answer a fact from memory alone, OR reach for the web/a tool to 'discover' something I might already know — recall first instead. The cheapest, highest-trust source is my own memory. Triggers: '/recall', 'recall <question>', any factual lookup (esp. curated-domain)."
---

# /recall

Architecture: KB and web retrieval run **inline** (you search); only the naked
probe is a subagent (weight-prior needs an uncontaminated context). History &
rationale for every rule here: `~/resources/mvm-incident-history.md`.

## Steps

> **Timing capture (F3a, mandatory):** the ledger profiles recall latency
> (713s mean as of 2026-06-12 — unfixable until rows say WHERE it goes), so
> stamp phase boundaries with `date +%s%3N` (epoch-ms, ~0 cost): chain it
> onto an adjacent Bash call where one exists (`date +%s%3N; <cmd>; date
> +%s%3N`), bare around non-Bash steps (web tools). Wall-clock per phase is
> the target — it INCLUDES orchestrator think-time, which may be the real
> sink. Deltas land in the `timings` object at step 6.

0. **Memo-cache.** `date +%s%3N; recall-log cache-lookup "<question>"; date
   +%s%3N` (stamps = `t0` start-of-recall + cache_ms). On a hit with
   `sim ≥ 0.7` whose answer you judge still fresh (check `age_days` against
   domain velocity — PoE2 patch facts go stale in days; biography doesn't):
   serve it, log with `decided_source:"cache"` + `source_detail` naming the
   original ts, output `— from cache (age Nd)`, done. Stale-suspect or
   low-sim → full pipeline below.

1. **Naked probe — the only subagent.** Spawn in the SAME message as your
   first inline search so it runs concurrently. `subagent_type:
   mvm-naked-clone` (tools: [], structural), `model: haiku`. Validation: task
   notification must show `tool_uses: 0`, else the measurement is invalid —
   re-spawn stricter or mark the probe failed. Timing: `probe_ms` = stamp in
   the FIRST Bash call after the probe result is in hand, minus the step-0
   end stamp (spawn-to-result wall, concurrency included).

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
   Timing: prepend `date +%s%3N;` to the first `mvm search` call and stamp
   again when KB retrieval concludes → `kb_search_ms`.

3. **Web retrieval — inline, by default.** ~1 WebSearch + 1 WebFetch on the
   top result (one extra if inconclusive); capture `web.ans` + `web.rationale`
   + URL. Skip ONLY when KB returned a grounded hit in a declared curated
   domain AND you have no staleness concern — web is how supersession is
   detected. Discount SEO/gold-seller domains (aoeah, u4gm, mmoexp, …).
   Timing: WebSearch/WebFetch are not Bash, so bare-stamp before the first
   and after the last web call → `web_ms`; skipped web → `"web_ms": null`.

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
   **Every web-decided ledger entry MUST carry `"ingested":false`** (step 6) —
   the spawn flips it via `recall-log mark-ingested` on commit. Without the
   field a dropped spawn is invisible (verified 2026-06-12: 6/11 desecrate web
   hit, no ingest, no detection — the field was absent so nothing could alarm).
   Skipping the spawn deliberately (hit doesn't qualify) → still write
   `"ingested":false` + the reason in `source_detail`.

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
    "decided_answer":"...","duration_ms":N,
    "timings":{"cache_ms":N,"kb_search_ms":N,"probe_ms":N,"web_ms":N,
               "reconcile_ms":N},
    "ingested":false}' | recall-log add   # "ingested" REQUIRED iff decided_source=web
   ```
   `decided_source` is a hard enum (CLI rejects anything else; prose goes in
   `source_detail`). `evidence_paths` = KB docs that grounded the answer
   (feeds retrieval-heat test coverage). ts/session_id/kind auto-fill.
   `timings` deltas come from the stamps above (null = step skipped; CLI
   rejects non-numeric/negative values); prepend `date +%s%3N;` to this add
   call — that final stamp gives `reconcile_ms` (end-of-retrieval → log) and
   `duration_ms` (t0 → log). Cache-served recalls log
   `{"cache_ms":N}` alone — the other steps never ran.

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
