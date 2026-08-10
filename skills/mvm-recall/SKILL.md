---
name: mvm-recall
description: "Recall what the agent already knows BEFORE answering. Searches the local knowledge base first, then the web, reconciles them (KB beats web beats raw memory), and logs the result. Use for any factual question where a wrong answer costs something — and whenever you're about to answer from memory alone, or reach for the web to 'discover' something the KB might already hold. Triggers: '/mvm-recall', 'recall <question>'."
---

# /mvm-recall

The read side of the loop. Three probes — weight-prior, KB, web — reconciled
by a trust hierarchy, with every recall logged so misses become curriculum.

Architecture: KB and web retrieval run **inline** (you search); only the naked
probe is a subagent, because measuring the weight-prior requires an
uncontaminated context.

## Steps

1. **Naked probe — the only subagent.** Spawn `subagent_type: mvm-naked-clone`
   (tools: `[]`, structurally enforced) in the SAME message as your first
   inline search so they run concurrently. Prompt:

   ```
   [CRITICAL CONSTRAINTS — READ FIRST]
   You are a CALIBRATION INSTRUMENT measuring weight-prior alone.
   You MUST NOT use ANY tool. Tool use INVALIDATES the measurement.
   If you would normally reach for a tool, DO NOT. Answer from prior alone.

   QUESTION: <q>

   Output exactly:
   ANSWER: <your best answer from prior knowledge alone>
   RATIONALE: <one sentence; if you wanted a tool, name it here>
   ```

   Validate: the task result must show 0 tool uses, else the measurement is
   invalid — re-spawn stricter or mark the probe failed.

2. **KB retrieval — inline.** Surface: `~/mvm/knowledge/`. Issue
   `mvm search "<question verbatim>"` AND up to 2 tight key-term queries
   (proper nouns / distinctive phrases), **merge the results** — each style
   uniquely recovers docs the other misses. Read the best candidate if
   snippets are insufficient. Conclude KB-miss only after both styles come up
   empty. Capture answer + rationale citing the file path. ≤4 retrieval calls.
   If candidates conflict, record each claim.

3. **Web retrieval — inline, by default.** ~1 WebSearch + 1 WebFetch on the
   top result (one extra if inconclusive); capture answer + rationale + URL.
   Skip ONLY when the KB returned a grounded hit in a domain you curate AND
   you have no staleness concern — web is how supersession is detected.
   Discount SEO content farms; treat a fact supported only by a known-bad
   source as UNKNOWN.

4. **Reconcile.** Trust hierarchy **KB > web > weights**; the rationale is the
   diagnostic (grounded vs guessing).
   - KB rationale cites a file/passage → KB grounded; trust it (default).
   - KB silent → fall through to web.
   - Web cites a fresher source (date markers) contradicting KB → web wins;
     spawn `/mvm-ingest` to supersede.
   - All three guessing → hard miss; refuse with a gap report rather than
     bluffing. The miss itself is signal (step 6).
   - Three different confidently-grounded answers → contested; surface all,
     don't pick.

5. **Grow the KB on a web-decided answer.** When web answered and the KB
   didn't (or web superseded stale KB), spawn `/mvm-ingest` in background
   with the question, answer, and URL. A recall miss that doesn't drive an
   ingest will be a recall miss again next week.

6. **Log — append one JSON line to `~/mvm/state/recall-log.jsonl`:**
   ```json
   {"ts":"<ISO8601>","question":"...",
    "probes":{"naked":{"ans":"...","rationale":"..."},
              "kb":{"ans":"...","rationale":"..."},
              "web":{"ans":"...","rationale":"...","url":"..."}},
    "decided_source":"kb|web|weights|contested|none",
    "evidence_paths":["<kb docs that grounded the answer>"],
    "decided_answer":"...","ingested":false}
   ```
   The log is what makes the system self-improving: `/mvm-dream` reads it to
   find coverage gaps (repeated web-fallbacks = missing KB docs) and to route
   verification effort toward the docs that actually ground answers.
   `mvm stats` and `mvm sweep` consume it too. `"ingested"` is required on
   web-decided entries — a dropped ingest spawn is invisible without it.

7. **Output:**
   ```
   <answer>

   — from <kb-path | url | weights>  reconciliation: <pattern>
   ```
