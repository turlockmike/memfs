# Neuroscience Memory Principles — Applied Review for `mem` CLI

*Researched: 2026-04-12*

## Summary

The `mem` plan borrows the vocabulary of neuroscience — Hebbian learning, connection strength, decay — but the borrowed math is wrong in ways that matter. The exponential decay formula `strength * 0.97^days` is biologically implausible and practically worse than a power-law alternative. More importantly, five neuroscience principles have direct computational analogs that would improve retrieval quality, index freshness, and clustering — without adding biological constraints that don't translate. Two principles (RIF and multi-phase consolidation) suggest concrete new commands. Two others (schema acceleration, engram pattern completion) suggest changes to existing scoring formulas. One (hippocampal index staleness) identifies a bug in the current architecture.

This review does not advocate simulating the brain. It asks: which solutions did evolution discover that are also good solutions to the information-organization problem `mem` is solving?

---

## Key Findings

1. **The decay formula should be power-law, not exponential.** The empirical forgetting curve is `P(t) = m(1 + ht)^-f`, not `P(t) = m * e^(-kt)`. Exponential decay over-predicts forgetting at short intervals and under-predicts it at long ones. The current `0.97^days` formula will prune legitimate long-dormant but genuinely important edges too aggressively within 100 days. A power law decays fast early and slow late — which matches how important-but-infrequently-accessed connections actually behave.

2. **Decay should be two-phase, not single-phase.** Memory consolidation research (the Memory Chain Model) shows a measurable uptick in retention around the 24-hour mark attributable to sleep replay. A two-component decay (fast initial + slow residual) better captures real memory dynamics. For `mem`, this means recently-accessed edges should decay faster in hours 0-24 and slower thereafter — which is a better signal about which connections are genuinely durable.

3. **The hippocampal index goes stale when files change content but not path.** The brain handles this via reconsolidation: memory is destabilized on retrieval and restabilized incorporating new content. `mem` currently only checks `content_hash` on `rebuild`. On `cat`, it should check whether `content_hash` has changed since last embedding and re-index inline. Stale FTS entries and stale embeddings are the `mem` equivalent of silent engrams — pointers that exist but can't retrieve the right content.

4. **Pattern completion means `mem find` should traverse the edge graph, not just rank isolated nodes.** Engram research shows that activating a small part of the network pulls in the rest. For `mem`, after initial FTS5/vector ranking, the top results should expand via their strongest edges to surface related nodes the query didn't directly match. This is the file-system equivalent of pattern completion — retrieving the whole memory from a partial cue.

5. **Retrieval-induced forgetting is computationally valid and worth implementing.** When `mem cat` is called on a specific file, the brain actively suppresses competing memories. This is an adaptive mechanism that reduces future interference. In `mem`, when a file is accessed, semantically similar files that were *not* accessed should receive a small strength decay on their edges to the accessed file. The mechanism: after `cat`, compute cosine similarity against all files with overlapping edges; apply a penalty of `-0.05` to co_access edges of similar-but-not-accessed neighbors. This keeps the graph from becoming uniformly dense with noise.

6. **Schema acceleration should modify the `co_access_increment`.** Schema theory shows that new information fitting existing structure consolidates faster and with less hippocampal mediation. In `mem`, files in the same directory share a structural context (a schema). When two files in the same directory are co-accessed, the increment should be higher (e.g., `+0.15` vs. `+0.10` for cross-directory co-access). This is a simple two-line change with empirical backing.

7. **Spacing effect inverts the naive intuition about decay.** The neuroscience of spaced repetition says memory is *stronger* when retrieval is difficult (desirable difficulty). A connection that was co-accessed yesterday and today is less proven than one co-accessed six months apart. The current formula treats all co-accesses as equal increments. A time-weighted increment would be: `increment = 0.1 * log(1 + days_since_last_co_access)` — so co-accessing two files after a week-long gap earns more strength than doing it twice in a row.

---

## Details

### 1. Hebbian Learning and LTP/LTD — The Wrong Decay Curve

**The neuroscience:**
Ebbinghaus established the forgetting curve in 1880/1885. He first fit it as a power function, then a logarithm. Wickelgren (1974) formalized it as `P(t) = m(1 + ht)^-f`, where forgetting is initially rapid and progressively slower — a power law. Modern replication studies (Murre & Dros, 2015) find the best fit is a *summed exponential* (two-component model), not a single exponential. A 2023 replication confirms "the forgetting curve is not completely smooth but shows a jump upwards starting at the 24-hour data point" — consistent with sleep consolidation boosting retention.

The single-exponential formula `0.97^days` is the one model the data consistently rejects. It predicts linear log-decay, but actual forgetting is steep at first, then levels off.

**Does it apply to `mem`?**
Yes, directly. Edge strength in `mem` represents connection salience over time. The wrong decay curve means:
- Edges to files accessed together 2 days ago decay almost as fast as edges from 30 days ago (exponential is relatively flat early, steep late relative to power law).
- Genuinely important but infrequent co-accesses (agent reads two related docs every 3 months) will be pruned before they demonstrate their value.

**Concrete change:**
Replace the current formula:
```
strength = strength * (0.97 ^ days_since_last_co_access)
```
With a power-law formula:
```
strength = initial_strength * (1 + 0.1 * days_since_last_co_access) ^ -0.5
```
Where `-0.5` is the `f` parameter (controls steepness; calibrate against pruning behavior). This gives a half-life that grows with age — exactly what the data supports.

Alternatively, use the two-component summed exponential to capture the 24-hour consolidation spike:
```
strength = A * exp(-a1 * days) + B * exp(-a2 * days)
```
With `a1 >> a2` (fast initial decay + slow residual). The 24-hour uptick can be modeled by not applying decay for the first 24 hours post-access. Parameter recommendation: `A=0.8, a1=0.1, B=0.2, a2=0.005` (fast component decays in ~10 days; slow component persists for ~200 days).

**Verdict:** High-value change. The current formula is the one forgetting curve model that empirical data rejects.

---

### 2. Memory Consolidation — Active Reorganization Pass

**The neuroscience:**
Sleep consolidation is not passive decay. During NREM slow-wave sleep, the hippocampus replays memory traces (sharp-wave ripples), and this replay facilitates:
1. Transfer from hippocampal to neocortical representation (systems consolidation)
2. Abstraction: extraction of rules/schema from individual episodes
3. Selective forgetting of episodic detail while preserving gist

The brain's "consolidation" is an active reorganization pass that compresses, abstracts, and merges related memories into schema structures. New evidence (Tse et al., 2007, *Science*; van Kesteren et al., 2012) shows that when new information fits existing schema, it can be consolidated directly into neocortex, bypassing the slow hippocampal route.

**Does it apply to `mem`?**
Yes, with constraints. The brain's consolidation does several things computers don't need (e.g., emotional tagging) but several things that are genuinely useful for a file memory system:
- Merge near-duplicate nodes (two files that say essentially the same thing)
- Extract implicit clusters (files that always appear together should be explicitly linked)
- Compress episodic specifics into summary nodes

**Concrete change — new command `mem consolidate`:**

This is the nightly pass the brain runs during sleep. Recommended implementation:

```
mem consolidate [--dry-run] [--min-similarity 0.85]
```

Steps:
1. **Merge candidates:** Compute pairwise cosine similarity across all embedded nodes. Any pair with similarity > 0.85 gets flagged. Show both files, ask agent to choose keeper or create merged node.
2. **Implicit edge strengthening:** Find all node pairs with >= 3 co-access log entries in the past 30 days but no explicit `co_access` edge (edge was pruned or never created). Recreate those edges at `strength = 1.0`.
3. **Orphan detection:** Any node with 0 edges and `access_count < 2` and `created_at > 90 days ago` gets flagged for archival.
4. **Schema extraction:** Find directory clusters where >= 5 files all link to each other. Create or strengthen a `schema_root` node (a directory-level summary) if one exists.

The key insight from sleep research: consolidation involves *selective forgetting* as much as strengthening. Step 3 is the forgetting half of consolidation.

**Verdict:** Medium-priority new command. The merge/deduplicate pass alone (`--min-similarity 0.85`) would prevent the graph from accumulating noise over time.

---

### 3. Hippocampal Indexing Theory — The Stale Index Bug

**The neuroscience:**
Teyler and DiScenna (1986, updated 2007 with Rudy) proposed the hippocampus as an index into cortical storage. The hippocampus doesn't store content — it stores *pointers* to neocortical patterns. On retrieval, the hippocampal index reactivates the distributed cortical pattern.

The critical question for `mem`: what happens when the underlying content changes but the index doesn't?

In the brain, this is handled via **reconsolidation**: when a memory is retrieved, it enters a labile (plastic) state and can incorporate new information before restabilization. The index is updated at the moment of access. If the cortical content has drifted from what the index points to, retrieval can produce false or outdated reconstructions — which is exactly what happens in `mem` when a file is modified but the FTS5 index and embeddings are stale.

**Does it apply to `mem`?**
Yes, and there is a concrete bug in the current plan. `mem rebuild` is the only stated mechanism for refreshing embeddings and FTS. But `mem cat` records accesses without checking whether the content has changed since last indexing. This means an agent can call `mem cat file.md`, get strengthened edges, return the file path to an LLM — but the LLM is working from a file that may be semantically different from what the FTS/vector index thinks it is.

The brain's reconsolidation mechanism says: *retrieval is the update trigger.*

**Concrete change:**
In `mem cat`, after recording the access, check `content_hash` against what's stored in `nodes`. If they differ:
1. Recompute FTS5 entry (stdlib, no venv needed)
2. Mark `embedded_at = NULL` to queue re-embedding on next `--embed` pass
3. Update `content_hash` and `modified_at`

This turns `cat` into a lazy index updater — the brain's reconsolidation pattern applied to a file system index. Cost: one hash computation per `cat` call. Benefit: the index is always consistent with what was last read.

Additionally, `mem find` should check the age of embeddings before using them. If `embedded_at` is NULL or older than 7 days for an active file, downweight the vector score and rely more on FTS5.

**Verdict:** This is a correctness issue, not just an optimization. The plan as written can produce stale-index retrievals.

---

### 4. Engram Networks — Pattern Completion in `mem find`

**The neuroscience:**
Engrams are not stored in single neurons or locations. They are distributed ensembles. Tonegawa's lab demonstrated that stimulating as few as two cortical neurons can trigger recall of a full engram through the pattern completion mechanism. The hippocampal DG (dentate gyrus) performs pattern separation (distinguishing similar inputs) while CA3 performs pattern completion (filling in the full memory from partial input).

Critically, "silent engrams" exist: memories that are biologically intact but inaccessible via natural cues, only reachable via direct stimulation. This suggests there are files in `mem` that FTS5 and vector search will never surface because the query doesn't lexically or semantically overlap — but which are genuinely relevant via their graph connections.

**Does it apply to `mem`?**
Yes. The current plan combines FTS5 + vector via RRF fusion (two-signal retrieval). But both signals operate on individual nodes in isolation. The graph edges are only used for co-access strengthening, not for retrieval expansion. This leaves an entire retrieval pathway unused.

**Concrete change — edge-expansion pass in `mem find`:**

After RRF fusion produces an initial ranked list (say top-10), run one hop of edge expansion:
1. For each top-N result, fetch all co_access and link edges with `strength > 0.5`
2. Score each neighbor: `edge_strength * 0.3 + original_node_rank_score * 0.1`
3. Merge neighbor scores into the result set, deduplicate
4. Re-rank combined list

This is pattern completion: you found the "two neurons" (the top FTS5 results), and the system completes the engram (retrieves the connected nodes that didn't match the direct query).

The expansion should be bounded: max 1 hop, max 5 neighbors per top result, max 20 total expanded results. Otherwise queries on well-connected nodes flood the output.

**Practical gain:** This directly addresses the "silent engram" problem — a file that has been co-accessed 40 times with the top result will surface even if its own content doesn't match the query.

**Verdict:** High-value change to `mem find`. Requires no new storage, only a post-RRF graph traversal.

---

### 5. Retrieval-Induced Forgetting — Competitive Edge Decay

**The neuroscience:**
Anderson et al. (1994, 2004) demonstrated that retrieving a target memory actively suppresses competing memories in the same retrieval category. Wimber et al. (2015, *Nature Neuroscience*) showed the suppression is real neural inhibition, not just interference: cortical patterns unique to competitors are measurably suppressed across multiple retrieval repetitions, and this suppression correlates with later behavioral forgetting. The effect is specific: categorical information is not suppressed, only item-specific competitors.

**Does it apply to `mem`?**
This is the most contested principle in this review. The brain uses RIF to manage limited working memory capacity — suppressing competitors reduces cognitive interference during retrieval. `mem` doesn't have this constraint; you can retrieve all files.

However, the information-organization problem it solves *does* apply: a graph where every access uniformly strengthens edges to all co-accessed nodes eventually becomes uniformly dense, where everything is equally strongly connected to everything else. That's not a useful memory system — it's noise. RIF is the mechanism that keeps the graph sparse and meaningful.

**Concrete change:**
When `mem cat file_a.md` is called:
1. Find all files with a co_access edge to `file_a.md`
2. Compute cosine similarity between `file_a.md` and each neighbor
3. For neighbors with similarity > 0.7 (semantically competing) that were NOT accessed in this session, apply `edge_strength *= 0.95` (a 5% penalty on their co_access edge to `file_a.md`)
4. Do NOT penalize explicit `link` or `explicit` edges — only `co_access` edges
5. Do NOT apply if `file_a.md` has fewer than 3 total co_access edges (not enough signal)

The mechanism keeps similar-but-not-accessed nodes from free-riding on their connection to a popular node. If you always access `project_alpha.md` without `project_beta.md`, the alpha-beta co_access edge should weaken over time.

**Caution:** This requires the venv (cosine similarity needs embeddings). Gate it behind `--embed` flag. Without embeddings, skip the RIF pass.

**Verdict:** Implementable but optional. The graph-densification problem it solves is real and will appear at scale (10K+ nodes). Include in Sprint 2 as a configurable flag `--rif-decay` on `mem cat`, default off.

---

### 6. Spacing Effect — Time-Weighted Co-Access Increment

**The neuroscience:**
The spacing effect is one of the most robustly replicated findings in memory research. Ebbinghaus observed it in 1885. The core finding: retrieval that requires effort (because some forgetting has occurred) produces stronger memories than immediate re-retrieval. Wozniak's SM-2/SuperMemo algorithm operationalizes this as `I(n) = I(n-1) * EF` where EF (ease factor) depends on retrieval difficulty.

The counterintuitive implication: *easier* retrievals (accessing two files that were just co-accessed an hour ago) should produce *weaker* strengthening than *harder* retrievals (accessing two files not co-accessed for two weeks). The difficulty is the signal.

**Does it apply to `mem`?**
Yes. The current plan uses a flat `co_access_increment = 0.1` regardless of time since last co-access. This treats an agent accessing the same pair of files 10 times in one session the same as accessing them once a month for 10 months. These are very different signals. The second pattern is much stronger evidence that the connection is genuinely meaningful.

**Concrete change:**
Replace the flat increment with a time-scaled increment:
```python
days_gap = (now - last_co_accessed).days  # days since last co-access
increment = 0.05 * (1 + log(1 + days_gap))
```
With `days_gap = 0` (same session), increment = 0.05. With `days_gap = 7`, increment = ~0.10. With `days_gap = 30`, increment = ~0.13. With `days_gap = 365`, increment = ~0.17.

This naturally caps the reward for rapid re-accessing (spam-proofing the graph) while giving disproportionately higher weight to connections that survive the test of time. It also means the `max_strength = 5.0` cap is reached only by connections that have been independently reconfirmed across many months — which is the right semantic for "this connection is important."

**Caveat:** For brand-new co_access edges (`last_co_accessed = NULL`), use a baseline increment of `0.1` (current behavior) since there's no gap to measure.

**Verdict:** High-value, low-cost change. Swap two lines of code in the increment calculation. Directly grounded in the most replicated memory finding in experimental psychology.

---

### 7. Schema Theory — Directory-Aware Consolidation Weighting

**The neuroscience:**
Bartlett (1932) introduced schema theory; van Kesteren and Morris operationalized it with fMRI. The key finding: when new information is congruent with an existing schema, the medial prefrontal cortex (mPFC) detects the match and enables fast neocortical encoding, bypassing the slow hippocampal consolidation route. In practical terms, information that fits an existing framework is remembered better and consolidated faster than information requiring a new framework.

Tse et al. (2007, *Science*) showed that rats could learn new paired associates in one trial (instead of the usual 6) once they had acquired a hippocampal-independent schema. Systems consolidation that normally takes weeks happened in hours.

**Does it apply to `mem`?**
Yes, with a useful analog. The `mem` filesystem already has a natural schema representation: directories. Files in `nodes/projects/` share a project schema. Files in `nodes/people/` share a person schema. When two files in the same directory are co-accessed, the connection fits within an existing schema — it should consolidate faster (higher increment).

Cross-directory co-access represents schema-bridging — more cognitively expensive, slower to consolidate, but when it does persist, it represents a genuine semantic relationship that transcends category.

**Concrete change:**
Apply a schema multiplier to the co_access increment:
```python
same_dir = os.path.dirname(source) == os.path.dirname(target)
schema_multiplier = 1.5 if same_dir else 1.0
increment = base_increment * schema_multiplier
```
This means intra-directory co-accesses consolidate at 1.5x the rate of cross-directory ones. It doesn't create two classes of edges — it just tilts the probability that intra-schema connections emerge faster.

Combined with the spacing effect change above:
```python
days_gap = max(0, (now - last_co_accessed).days)
base = 0.05 * (1 + log(1 + days_gap))
increment = base * (1.5 if same_dir else 1.0)
```

**Additional schema application — `mem find` result re-ranking:**
After RRF fusion, apply a schema bonus: if the top result and a lower-ranked result share the same directory, bump the lower result up slightly (`score *= 1.1`). This models the brain's schema-consistent retrieval bias — when you recall something, nearby schema members are more likely to be co-retrieved.

**Verdict:** Medium-value, low-cost. The directory multiplier is a two-line addition. The find re-ranking adds minor complexity but improves coherence of results.

---

## What NOT to Implement (Biological Constraints That Don't Transfer)

**Emotional tagging:** The brain uses amygdala-mediated emotional salience to strengthen memories. No analog exists in a file system — skip.

**REM vs. NREM stage separation:** Sleep stages serve different consolidation functions. A file system runs 24/7 and has no quiescent phase. The "sleep" analog is a scheduled consolidation run, but it doesn't need to be split into phases.

**Neurogenesis:** The hippocampus generates new neurons to create new index entries. `mem` handles this natively — every new file is a new node. No special mechanism needed.

**Homeostatic plasticity / synaptic scaling:** The brain globally downscales synapse strength during sleep to prevent runaway potentiation. `mem` handles this via the `prune_threshold` floor already. The `max_strength = 5.0` cap plays the same role.

**Place cells and spatial encoding:** Memories are partially encoded in spatial context. Not applicable to a text file system (though directory structure is a weak analog, already covered by schema theory above).

---

## Prioritized Change List

| Priority | Change | Sprint | Lines |
|----------|--------|--------|-------|
| 1 | **Power-law decay formula** — replace `0.97^days` with `(1 + 0.1*days)^-0.5` | Sprint 2 | 3 |
| 2 | **Stale index check on `cat`** — recompute FTS5 + mark embedding stale if content_hash changed | Sprint 1 | 15 |
| 3 | **Time-weighted co_access increment** — `0.05 * (1 + log(1 + days_gap))` | Sprint 2 | 5 |
| 4 | **Edge-expansion in `mem find`** — 1-hop graph traversal after RRF fusion | Sprint 3 | 30 |
| 5 | **Schema directory multiplier** — 1.5x increment for same-dir co-access | Sprint 2 | 3 |
| 6 | **Two-phase decay** — skip decay for first 24h post-access | Sprint 2 | 5 |
| 7 | **`mem consolidate` command** — merge candidates + implicit edge recovery + orphan detection | Sprint 5 | 80 |
| 8 | **RIF competitive decay** — `--rif-decay` flag on `cat`, default off, requires embeddings | Sprint 4 | 25 |

---

## Sources

- Murre, J.M.J. & Dros, J. (2015). Replication and Analysis of Ebbinghaus' Forgetting Curve. *PMC*. [https://pmc.ncbi.nlm.nih.gov/articles/PMC4492928/](https://pmc.ncbi.nlm.nih.gov/articles/PMC4492928/)
- Wickelgren, W.A. (1974). Single-trace fragility theory of memory dynamics. *Memory & Cognition*. Analyzed via: [Semantic Scholar](https://www.semanticscholar.org/paper/The-Wickelgren-Power-Law-and-the-Ebbinghaus-Savings-Wixted-Carpenter/1c03c10e5d168eb990a9ce5a68267ac4b6b59c53/figure/0)
- Wimber, M. et al. (2015). Retrieval induces adaptive forgetting of competing memories via cortical pattern suppression. *Nature Neuroscience*. [https://pubmed.ncbi.nlm.nih.gov/25774450/](https://pubmed.ncbi.nlm.nih.gov/25774450/)
- Tonegawa Lab (2015, 2018). Memory engram storage and retrieval. [MIT Open Access](https://dspace.mit.edu/bitstream/handle/1721.1/126264/Tonegawa%20NRN%202018%20-%20Main%20Text%20Final.pdf)
- Engram neurons: encoding, consolidation, retrieval, and forgetting. *PMC* (2023). [https://pmc.ncbi.nlm.nih.gov/articles/PMC10618102/](https://pmc.ncbi.nlm.nih.gov/articles/PMC10618102/)
- Teyler, T.J. & Rudy, J.W. (2007). The hippocampal indexing theory and episodic memory: updating the index. *Hippocampus*. [https://pubmed.ncbi.nlm.nih.gov/17696170/](https://pubmed.ncbi.nlm.nih.gov/17696170/)
- Memory Reconsolidation Mediates the Updating of Hippocampal Memory Content. *Frontiers in Behavioral Neuroscience* (2010). [https://www.frontiersin.org/journals/behavioral-neuroscience/articles/10.3389/fnbeh.2010.00168/full](https://www.frontiersin.org/journals/behavioral-neuroscience/articles/10.3389/fnbeh.2010.00168/full)
- Systems memory consolidation during sleep. *Nature Neuroscience* (2019). [https://www.nature.com/articles/s41593-019-0467-3](https://www.nature.com/articles/s41593-019-0467-3)
- Memory consolidation (review). *PMC* (2015). [https://pmc.ncbi.nlm.nih.gov/articles/PMC4526749/](https://pmc.ncbi.nlm.nih.gov/articles/PMC4526749/)
- Overlapping memory replay during sleep builds cognitive schemata. *ScienceDirect* (2011). [https://www.sciencedirect.com/science/article/abs/pii/S1364661311001094](https://www.sciencedirect.com/science/article/abs/pii/S1364661311001094)
- van Kesteren, M.T.R. et al. (2012). How schema and novelty augment memory formation. *Trends in Neurosciences*. [https://pubmed.ncbi.nlm.nih.gov/22398180/](https://pubmed.ncbi.nlm.nih.gov/22398180/)
- Tse, D. et al. (2007). Schemas and Memory Consolidation. *Science*. [https://www.science.org/doi/abs/10.1126/science.1135935](https://www.science.org/doi/abs/10.1126/science.1135935)
- Anderson, M.C. et al. Retrieval-induced forgetting: inhibitory mechanisms. *ScienceDirect Topics*. [https://www.sciencedirect.com/topics/neuroscience/retrieval-induced-forgetting](https://www.sciencedirect.com/topics/neuroscience/retrieval-induced-forgetting)
- Spacing effect. *Wikipedia / Decision Lab*. [https://thedecisionlab.com/biases/spacing-effect](https://thedecisionlab.com/biases/spacing-effect)
- SM-2 Algorithm. *SuperMemo*. [https://help.supermemo.org/wiki/SuperMemo_Algorithm](https://help.supermemo.org/wiki/SuperMemo_Algorithm)
- Hebbian plasticity in parallel synaptic pathways. *PLOS Computational Biology* (2021). [https://pmc.ncbi.nlm.nih.gov/articles/PMC8683039/](https://pmc.ncbi.nlm.nih.gov/articles/PMC8683039/)

---

## Cross-References

- `/Users/michaeldarmousseh/workspace/doc/research/llm-memory-architectures.md` — comparative analysis of MemGPT, GraphRAG, Mem0, Zep, LangGraph, Cognee
- `/Users/michaeldarmousseh/workspace/doc/research/filesystem-memory-search.md` — fuzzy/semantic search for LLM agent memory
- `/Users/michaeldarmousseh/workspace/doc/synthesis/llm-memory-organization.md` — three-tier memory model, research-backed

---

## Open Questions

1. **Power-law calibration:** What value of `f` in `(1 + h*days)^-f` best matches the `prune_threshold = 0.05` goal without premature pruning of long-dormant important edges? Needs empirical tuning against LongMemEval access patterns.

2. **RIF threshold sensitivity:** The `0.7 cosine similarity` threshold for "competing memory" is a guess. Too low penalizes unrelated files. Too high means no suppression happens. Needs measurement.

3. **Does edge-expansion hurt precision at LongMemEval?** Pattern completion could surface noise. The 1-hop / strength>0.5 bounds are conservative, but the effect on Top-1 recall vs. MRR needs empirical testing.

4. **Schema boundaries:** Directories as schema is a weak proxy. A more principled schema detection would cluster by embedding centroids. Worth revisiting after Sprint 3 when embeddings exist.

5. **Two-phase decay timing:** The 24-hour "consolidation bump" is measured in human subjects. LLM agents may have very different temporal access patterns (bursts of queries in a session, then silence). The two-phase timing should be parameterized and tuned, not hard-coded.
