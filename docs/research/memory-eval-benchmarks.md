# LLM Memory System Evaluation Benchmarks

*Researched: 2026-04-12*

## Summary

The field of LLM memory evaluation has exploded in 2024-2026. There are now over a dozen runnable benchmarks, ranging from foundational academic benchmarks (LoCoMo, LongMemEval) to commercial evaluation harnesses (Supermemory's MemoryBench, Vectorize's Agent Memory Benchmark) to cutting-edge research pushing evaluation to 10M-token scales (BEAM). The landscape breaks cleanly into four camps: (1) multi-session conversation benchmarks that test recall across separate chats, (2) long-context retrieval tests (needle-in-haystack variants), (3) personalization and user modeling benchmarks, and (4) continual/lifelong learning benchmarks that test whether the memory system can handle updates, contradictions, and staleness.

The key meta-finding: most benchmarks designed pre-2025 are now partially defeated by large context windows — GPT-4o can fit all of LoCoMo's 10 conversations into context and brute-force the answers. The newer generation of benchmarks (BEAM at 10M tokens, MemoryAgentBench's Conflict Resolution tasks, LifeBench's non-declarative memory) is specifically designed to remain hard even when context-stuffing is possible.

The hardest unsolved task across every benchmark is **Conflict Resolution** — when memory must be updated and old facts discarded. Best-in-class systems score under 6% on multi-hop conflict resolution (MemoryAgentBench CR-MH). That's where the real work is.

## Key Findings

1. **LoCoMo** (Snap Research, ACL 2024) is the most widely-used baseline. Only 10 conversations, but they're synthetic long-session dialogues that are the common denominator for comparing memory systems. Mem0, Zep, MemGPT all report scores on it. Dataset is public.

2. **LongMemEval** (ICLR 2025) is the current academic standard for rigorous multi-session evaluation. 500 hand-curated questions, 5 distinct memory task types, configurable context sizes from 115K to 1.5M tokens. Full harness on GitHub. Dataset on HuggingFace.

3. **MemoryAgentBench** (ICLR 2026) is the most comprehensive for testing memory *maintenance* — specifically Conflict Resolution (belief updates) and Test-Time Learning (the memory actually improves from interactions). Published at ICLR 2026 with code + HuggingFace dataset.

4. **BEAM** (ICLR 2026) is the most demanding retrieval benchmark — 100 conversations scaling to 10M tokens. Designed explicitly to defeat context-stuffing strategies. Repository at GitHub.

5. **PersonaMem / PersonaMem-v2** (COLM 2025) is the best benchmark for user modeling / personalization. Tests implicit preference tracking across 32K–1M token histories. Top models score ~52% at 1M tokens. HuggingFace dataset.

6. **MemSim / MemDaily** (NeurIPS 2025) is the only benchmark that uses a Bayesian simulator to generate evaluation data, making it harder to game with static dataset memorization.

7. **LMEB** (March 2026) is specifically for evaluating the *embedding models* used in memory retrieval — 22 datasets, 193 retrieval tasks, 4 memory types. Tests whether your embedding model is even capable of the fuzzy search your memory system needs.

8. **Contradiction handling is the unsolved frontier.** MemoryAgentBench's CR-MH scores stay at or below 6% for the best models. LongMemEval's knowledge update tasks show similar weakness. This is where file-based memory graphs will either prove their value or fail.

9. **Commercial harnesses are now competitive with academic ones.** Supermemory's MemoryBench wraps LoCoMo + LongMemEval + ConvoMem and lets you swap in any provider (Mem0, Zep, Supermemory) with one CLI command. Vectorize's Agent Memory Benchmark wraps 6 datasets and adds agentic evaluation modes.

10. **DMR (Deep Memory Retrieval)** from MemGPT/Letta is the industry's earliest named benchmark, but it has a critical flaw: each conversation is only ~60 messages, which fits comfortably in any modern LLM context window. Scores of 94-98% should be treated with skepticism.

---

## Details

### Category 1: Multi-Session Memory Benchmarks

---

#### LoCoMo (Long-term Conversational Memory)
- **Source:** Snap Research — Maharana et al., ACL 2024
- **Paper:** [arXiv:2402.17753](https://arxiv.org/abs/2402.17753) | [ACL Anthology](https://aclanthology.org/2024.acl-long.747/)
- **Repo:** [snap-research/locomo](https://github.com/snap-research/locomo)
- **Dataset:** `./data/locomo10.json` in the repo (public, no separate download)

**What it measures:** Memory across very long-term multi-session conversations. Three tasks: (1) QA (factual recall), (2) event summarization, (3) multimodal dialog generation.

**How it works:** 10 LLM-generated conversations between virtual agents with assigned personas. Each conversation spans up to 32 sessions, averaging ~600 turns and ~16K tokens, covering 6-12 months of simulated time. Conversations are human-verified for coherence. Questions require cross-session reasoning.

**Dataset availability:** Fully public in the GitHub repo. Images not released but web URLs/captions included.

**How to run:** Evaluation scripts for OpenAI (`evaluate_gpts.sh`), Anthropic Claude (`evaluate_claude.sh`), Google Gemini (`evaluate_gemini.sh`), HuggingFace models (`evaluate_hf_llm.sh`), and RAG (`evaluate_rag_gpts.sh`). You configure API keys in `scripts/env.sh`.

**Metrics:** QA F1 score (human baseline ~88%), event summarization accuracy, dialog generation quality. Human performance substantially above model baselines.

**Caveat:** Only 10 conversations. With large context windows, full context stuffing is competitive. Mostly measures LLM reading comprehension at this point. More useful as a common denominator for cross-system comparison.

---

#### LongMemEval
- **Source:** University of Illinois — Wu et al., ICLR 2025
- **Paper:** [arXiv:2410.10813](https://arxiv.org/abs/2410.10813) | [OpenReview](https://openreview.net/forum?id=pZiyCaVuti)
- **Repo:** [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval)
- **Dataset:** HuggingFace — `xiaowu0162/longmemeval-cleaned`

**What it measures:** Five core long-term memory abilities:
1. **Information Extraction** — retrieving specific facts
2. **Multi-Session Reasoning** — synthesizing across sessions
3. **Temporal Reasoning** — time-based relationships (hardest; models lag humans by ≥50 pp)
4. **Knowledge Updates** — handling evolving/changing facts
5. **Abstention** — knowing when the answer isn't in memory

**How it works:** 500 hand-curated questions. Human experts write questions, then manually decompose answers into evidence statements with optional timestamps. Chat histories are LLM-simulated and human-edited. Three dataset variants:
- `LongMemEval_S` — ~40 sessions, ~115K tokens
- `LongMemEval_M` — ~500 sessions, up to 1.5M tokens
- `LongMemEval_Oracle` — evidence-only sessions for upper-bound testing

**Dataset availability:** Download from HuggingFace:
```
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_oracle.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
wget https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_m_cleaned.json
```

**How to run:**
- Minimal (eval only): `conda create -n longmemeval-lite python=3.9 && pip install -r requirements-lite.txt`
- Full: `pip install -r requirements-full.txt`
- Evaluate: `python3 evaluate_qa.py gpt-4o output_file data/longmemeval_oracle.json`
- Evaluation uses GPT-4o as judge

**Metrics:** Memory recall accuracy (turn-level and session-level), QA correctness, retrieval precision/recall. Commercial chat assistants score 30-60% lower than Oracle baselines.

---

#### MSC (Multi-Session Chat)
- **Source:** Facebook AI Research — Xu et al., ACL 2022
- **Paper:** [arXiv:2107.07567](https://arxiv.org/abs/2107.07567)
- **Dataset:** [ParlAI MSC project](https://parl.ai/projects/msc/)

**What it measures:** Memory and persona consistency across multiple chat sessions. The MSC dataset models how two speakers chat in repeated sessions over simulated days.

**How it works:** Sessions 1-4 are annotated. 130K train / 25K validation examples. Each session can reference shared history from prior sessions. The key design: conversations pause and resume across sessions, testing whether the model remembers context from the last chat.

**Dataset availability:** Public via ParlAI framework.

**How to run:** Via ParlAI — `parlai interactive --model-file zoo:blenderbot2/blenderbot2_3B/model --task blended_skill_talk`. DMR (Deep Memory Retrieval) task uses MSC as its underlying dataset.

**Metrics:** Response consistency with prior sessions, persona accuracy.

**Note:** MSC was the original benchmark the MemGPT team used for their DMR task. Individual sessions are short (~60 messages each), which limits its challenge for modern systems.

---

#### LoCoMo-Plus
- **Source:** Kumihoclouds — February 2026
- **Paper:** [arXiv:2602.10715](https://arxiv.org/abs/2602.10715)
- **Repo:** [kumihoclouds/kumiho-benchmarks](https://github.com/kumihoclouds/kumiho-benchmarks)

**What it measures:** Goes beyond factual recall to test *cognitive memory* — the ability to apply latent constraints that were never explicitly queried. Decomposes cognitive memory into four constraint types:
- **Causal** — cause-effect chains across sessions
- **State** — tracking evolving user state
- **Goal** — user's long-term objectives
- **Value** — user's principles and preferences

**How it works:** Built on top of the original LoCoMo conversations. Adds constraint-consistency-based evaluation — models must not just recall facts but apply implicit constraints (e.g., the user is vegan, so recommending a steak restaurant is wrong even if never explicitly asked about steaks).

**Dataset availability:** Via the kumiho-benchmarks repo.

**Metrics:** Reports both LoCoMo score (factual) and LoCoMo-Plus score (cognitive) with a Gap column showing performance degradation. Current models show large gaps — they handle facts better than implicit constraints.

---

### Category 2: Long-Context Retrieval / Needle-in-Haystack Variants

---

#### BABILong (NeurIPS 2024 Spotlight)
- **Source:** AIRI / Yandex — NeurIPS 2024 Datasets & Benchmarks Track
- **Paper:** [arXiv:2406.10149](https://arxiv.org/abs/2406.10149) | [NeurIPS proceedings](https://papers.nips.cc/paper_files/paper/2024/hash/c0d62e70dbc659cc9bd44cbcf1cb652f-Abstract-Datasets_and_Benchmarks_Track.html)
- **Repo:** [booydar/babilong](https://github.com/booydar/babilong)

**What it measures:** Reasoning across facts distributed in extremely long documents. 20 task types including: fact chaining, simple induction, deduction, counting, handling lists/sets.

**How it works:** Embeds bAbI reasoning tasks within irrelevant PG-19 background text. Test samples can scale to millions of tokens. The "needle" is task-relevant sentences; the "haystack" is PG-19 book text. Designed to be extensible to any length.

**Dataset availability:** Public on HuggingFace and GitHub.

**How to run:** Standard HuggingFace dataset loading. Scripts in repo for various evaluation configurations.

**Metrics:** Task accuracy per reasoning type. Models effectively use only 10-20% of context; performance drops sharply with reasoning complexity. RAG achieves ~60% on single-fact QA. Fine-tuned Recurrent Memory Transformers handle up to 50M tokens.

---

#### BEAM (Beyond a Million Tokens) — ICLR 2026
- **Source:** ICLR 2026 — Tavakoli et al.
- **Paper:** [arXiv:2510.27246](https://arxiv.org/abs/2510.27246)
- **Repo:** [mohammadtavakoli78/BEAM](https://github.com/mohammadtavakoli78/BEAM)

**What it measures:** Long-term memory and long-context reasoning at scales where context-stuffing is impossible. Specifically designed to defeat the "put it all in context" baseline.

**How it works:** 100 diverse conversations, scaling up to 10 million tokens each. 2,000 probing questions covering a wide range of memory abilities. Conversations are generated via an automatic framework producing narrative-coherent, topically diverse dialogues.

**Dataset availability:** Public via repo/HuggingFace (confirm at repo).

**How to run:** See repo for harness. Hindsight (Vectorize) is current SOTA.

**Metrics:** Accuracy on probing questions. The LIGHT framework proposed in the paper achieves 3.5-12.69% improvement over strong baselines. This is one of few benchmarks where context-stuffing literally fails.

**Why it matters:** 10M tokens ~ a year of daily AI conversations, or an entire internal documentation corpus. If you're building a memory system that will operate over months of use, BEAM is the stress test.

---

#### LLMTest_NeedleInAHaystack (Baseline reference)
- **Source:** Greg Kamradt (2023, community standard)
- **Repo:** [gkamradt/LLMTest_NeedleInAHaystack](https://github.com/gkamradt/LLMTest_NeedleInAHaystack)

**What it measures:** Whether a specific fact ("needle") can be retrieved from a long document ("haystack") at varying context positions and lengths.

**How it works:** Embeds a random statement at various positions within Paul Graham essays. Tests retrieval across the full context window at multiple depth percentages. Classic 2D heatmap: context length vs. needle position.

**Dataset availability:** Self-generated — no fixed dataset. You run the needle placement yourself.

**Metrics:** Recall accuracy as a 2D function of (context length, needle depth). Classic Claude 3 Opus and GPT-4 heatmaps are the industry reference.

**Note:** This tests context window retrieval, not memory system retrieval. It's useful as a baseline to understand the base model's raw capability before adding a memory layer. Results do *not* transfer to multi-session memory scenarios.

---

#### LMEB (Long-horizon Memory Embedding Benchmark)
- **Source:** KaLM-Embedding team — March 2026
- **Paper:** [arXiv:2603.12572](https://arxiv.org/abs/2603.12572)
- **Repo:** [KaLM-Embedding/LMEB](https://github.com/KaLM-Embedding/LMEB)
- **Dataset:** [HuggingFace: KaLM-Embedding/LMEB](https://huggingface.co/datasets/KaLM-Embedding/LMEB)

**What it measures:** The quality of *embedding models* used in memory retrieval — not the end-to-end memory system but the retrieval component specifically. Targets memory-augmented systems where embedding quality determines what gets surfaced.

**How it works:** 22 datasets, 193 zero-shot retrieval tasks across 4 memory types:
- **Episodic** — recall of specific events
- **Dialogue** — conversational context retrieval
- **Semantic** — knowledge and factual retrieval
- **Procedural** — how-to and process recall

**Dataset availability:** Public on HuggingFace.

**Metrics:** Retrieval accuracy per memory type. Key finding: LMEB and MTEB are orthogonal — performance on standard passage retrieval does *not* generalize to memory retrieval. Larger models do not always win. This means you can't just pick the top MTEB embedding model and assume your fuzzy search quality is good.

**Directly applicable to:** Evaluating which embedding model to use in a file-based memory graph. Run your candidate embedding models against LMEB's episodic and semantic tasks to pick the right one.

---

### Category 3: Personalization / User Modeling Benchmarks

---

#### LaMP (Large Language Models Personalization)
- **Source:** UMass et al., ACL 2024
- **Paper:** [arXiv:2304.11406](https://arxiv.org/abs/2304.11406) | [ACL Anthology](https://aclanthology.org/2024.acl-long.399/)
- **Repo:** [LaMP-Benchmark/LaMP](https://github.com/LaMP-Benchmark/LaMP)
- **Website:** [lamp-benchmark.github.io](https://lamp-benchmark.github.io/)
- **Dataset:** Public on HuggingFace ([download page](https://lamp-benchmark.github.io/download))

**What it measures:** 7 personalized NLP tasks across text classification and generation, testing whether models can adapt to individual user profiles from retrieval history.

**How it works:** Each task has a user profile (history of past outputs/preferences) and a new request. Two evaluation splits: (a) user-based (new users) and (b) time-based (future interactions for existing users). Tasks include: citation identification, news category prediction, tweet paraphrasing, email subject generation, scholarly title generation.

**Dataset availability:** Public on HuggingFace.

**How to run:** Code at GitHub repo. Standard fine-tuning + RAG evaluation pipeline.

**Metrics:** Task-specific accuracy/F1. RAG-based methods improve ~15% over non-personalized baseline. PEFT improves ~1%. Combined RAG+PEFT improves ~16%.

**Note:** LaMP tests static user profiles, not evolving preferences. Good for baseline personalization but doesn't capture dynamic preference drift.

---

#### PersonaMem / PersonaMem-v2
- **Source:** Bowen Peng et al., COLM 2025
- **Paper (original):** [PersonaMem GitHub](https://zhuoqunhao.github.io/PersonaMem.github.io/)
- **Paper (v2):** [arXiv:2512.06688](https://arxiv.org/abs/2512.06688)
- **Repos:** [bowen-upenn/PersonaMem](https://github.com/bowen-upenn/PersonaMem) | [bowen-upenn/PersonaMem-v2](https://github.com/bowen-upenn/PersonaMem-v2)
- **Dataset:** HuggingFace — `bowen-upenn/PersonaMem` and `bowen-upenn/PersonaMem-v2`

**What it measures:** Dynamic user profiling — whether models can infer *implicit* user preferences from conversation histories and apply them in new scenarios. PersonaMem-v2 expands to 1000 user personas and 20,000+ preferences across 300+ scenarios.

**How it works:** Synthetic multi-session conversation histories (18 topics: therapy, food, travel, coding, etc.) where user preferences are revealed implicitly — not stated directly. Tests 7 question types: factual recall, preference inference, preference evolution, cross-domain generalization, etc. Three context window sizes: 32K, 128K, 1M tokens.

**Dataset availability:** Public on HuggingFace, organized by context window size.

**How to run:** Ready-made inference scripts for 15 models (GPT-4.5, Claude-3.7, Gemini-2.5, Llama-4). Run via `bash scripts/inference_gpt_4o.sh`.

**Metrics:** Accuracy on multiple-choice questions. Best models score ~52% at 1M context. Significant degradation in longer contexts means model can read the answer but fails to apply the implicit constraint.

---

#### MemSim / MemDaily
- **Source:** NeurIPS 2025
- **Paper:** [arXiv:2409.20163](https://arxiv.org/abs/2409.20163) | [NeurIPS poster](https://neurips.cc/virtual/2025/poster/115437)
- **Repo:** [nuster1128/MemSim](https://github.com/nuster1128/MemSim)

**What it measures:** Memory of LLM-based personal assistants — recall, association, and update of user-specific facts.

**How it works:** A Bayesian simulator generates user profiles via a Bayesian Relation Network (BRNet), then produces diverse user messages and QAs via a causal generation mechanism that mitigates hallucination in evaluation data. The generated dataset (MemDaily) covers the daily-life scenario. QA types include: single-hop, multi-hop, comparative, aggregative, and post-processing.

**Dataset availability:** Generated dataset (MemDaily) available via the GitHub repo.

**Metrics:** QA accuracy across QA types. First objective, automatic evaluation framework for personal assistant memory.

**Why it's notable:** The Bayesian generation approach means the dataset can be regenerated without risk of training data contamination — useful for fair evaluation of models trained on public datasets.

---

#### LifeBench
- **Source:** March 2026
- **Paper:** [arXiv:2603.03781](https://arxiv.org/abs/2603.03781)
- **Repo:** [1754955896/LifeBench](https://github.com/1754955896/LifeBench)

**What it measures:** Long-horizon multi-source memory — synthesizing memory from fragmented digital traces (chats, calendar, notes, SMS, health records) rather than explicit dialogue. Specifically tests *non-declarative memory* (habits, procedural knowledge inferred from patterns) which all other benchmarks ignore.

**How it works:** Synthesizes one year of user activity for 10 users — ~5,149 events per user (~14 events/day). Uses real-world priors: anonymized social surveys, map APIs, holiday-integrated calendars. Memory types:
- **Information Extraction (IE)**
- **Multi-hop reasoning (MR)**
- **Temporal and Knowledge Updating (TKU)**
- **Nondeclarative Memory Reasoning (ND)** — the novel task
- **Unanswerable (UA)**

**Dataset availability:** Public at GitHub.

**Metrics:** Accuracy per memory type category. ND tasks are hardest — models that do fine on declarative recall fail here.

---

### Category 4: Memory Maintenance / Update / Conflict Benchmarks

---

#### MemoryAgentBench — ICLR 2026
- **Source:** HUST AI Lab — ICLR 2026
- **Paper:** [arXiv:2507.05257](https://arxiv.org/abs/2507.05257) | [OpenReview](https://openreview.net/forum?id=ZgQ0t3zYTQ)
- **Repo:** [HUST-AI-HYZ/MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench)
- **Dataset:** [HuggingFace: ai-hyz/MemoryAgentBench](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench)

**What it measures:** Four core memory competencies based on cognitive science:
1. **Accurate Retrieval (AR)** — finding stored information
2. **Test-Time Learning (TTL)** — acquiring and applying knowledge during interaction
3. **Long-Range Understanding (LRU)** — reasoning across extended contexts
4. **Conflict Resolution (CR)** — handling contradictions and knowledge updates. Sub-task CR-MH (multi-hop) is the hardest: best models score ≤6%.

**How it works:** "Inject once, query multiple times" design — one piece of information supports multiple questions. Combines reformulated data from RULER, InfBench, HELMET, LongMemEval, plus two new datasets (EventQA and FactConsolidation). Structured as incremental multi-turn interactions with automatic HuggingFace data download.

**Dataset availability:** Auto-downloaded from HuggingFace on first run.

**How to run:**
```bash
conda create -n memoryagentbench python=3.10.16
pip install -r requirements.txt
# Long-context agents:
bash bash_files/eniac/run_memagent_longcontext.sh
# RAG/memory methods:
bash bash_files/eniac/run_memagent_rag_agents.sh
# Evaluation:
python llm_based_eval/longmem_qa_evaluate.py
```
Requires `.env` with OpenAI, Anthropic, Google API keys. GPT-4o used as judge.

**Metrics:** Accuracy per competency. CR-MH (multi-hop conflict resolution) is the unsolved frontier.

---

#### TRACE (Continual Learning)
- **Source:** BeyonderXX et al., 2023/2024
- **Paper:** [arXiv:2310.06762](https://arxiv.org/abs/2310.06762) | [OpenReview](https://openreview.net/forum?id=xelrLobW0n)
- **Repo:** [BeyonderXX/TRACE](https://github.com/BeyonderXX/TRACE)

**What it measures:** Continual learning in LLMs — whether fine-tuned models retain prior knowledge while learning new tasks. Tests catastrophic forgetting across 8 diverse domains.

**How it works:** 8 training datasets across domain-specific tasks, multilingual capabilities, code generation, and math reasoning: C-STANCE, FOMC, MeetingBank, Py150, ScienceQA, NumGLUE-cm, NumGLUE-ds, 20Minuten. All standardized into unified format for automatic evaluation. Replay dataset (Lima) included.

**Dataset availability:** Public at GitHub. All 8 datasets in standardized format.

**How to run:** Standardized evaluation pipeline at GitHub repo. Automatic evaluation via provided scripts.

**Metrics:** Task accuracy before/after continual training. Key finding: after training on TRACE, llama2-chat-13B drops from 28.8% to 2% on GSM8K. Demonstrates that standard fine-tuning catastrophically forgets general capabilities.

**Applicable to:** Testing whether a memory system's fine-tuning approach degrades base model capability. TRACE is more about model weight updates than retrieval-based memory, but it's the right benchmark if you're exploring write-to-weights approaches.

---

#### Evo-Memory (Google DeepMind + UIUC)
- **Source:** Google DeepMind + UIUC — November 2025
- **Paper:** [arXiv:2511.20857](https://arxiv.org/abs/2511.20857)

**What it measures:** Test-time learning with self-evolving memory — whether the memory system improves from accumulated experience across task streams (not just retrieval but learning to do better over time).

**How it works:** Sequential task streams require the agent to search, adapt, and evolve memory after each interaction. 10 diverse multi-turn and single-turn datasets. Over 10 memory modules unified and compared. Proposes ReMem: action-think-memory-refine pipeline for continual improvement.

**Dataset availability:** 10 public datasets (listed in paper).

**Metrics:** Performance trajectory across task streams — does accuracy improve over time? Baseline method: ExpRAG (retrieve and adapt prior experience).

---

#### MemoryRewardBench
- **Source:** LCM-Lab — January 2026
- **Paper:** [arXiv:2601.11969](https://arxiv.org/abs/2601.11969)
- **Repo:** [LCM-Lab/MemRewardBench](https://github.com/LCM-Lab/MemRewardBench)

**What it measures:** Whether *reward models* can accurately score long-term memory management quality — specifically, whether RLHF supervision signals are reliable for memory-capable systems.

**How it works:** 10 settings with different memory management patterns. Context lengths from 8K to 128K tokens. Three task categories: long-context reasoning, multi-turn dialogue, long-form generation. Evaluates 13 reward models.

**Metrics:** Reward model accuracy at judging memory quality. Key finding: reward models show process/positional biases that undermine process-based learning; fragility beyond 64K tokens.

**Why it matters:** If you're training a memory system with RLHF, the reward model itself may be biased against the right memory behaviors. This benchmark lets you audit that.

---

### Category 5: Comprehensive / Multi-Capability Benchmarks

---

#### MemBench (ACL 2025)
- **Source:** Tan et al., ACL 2025 Findings
- **Paper:** [arXiv:2506.21605](https://arxiv.org/abs/2506.21605) | [ACL Anthology](https://aclanthology.org/2025.findings-acl.989/)
- **Repo:** [import-myself/Membench](https://github.com/import-myself/Membench)

**What it measures:** Memory from multiple angles: factual memory and reflective memory (two memory levels), participation and observation (two interactive scenarios). Adds efficiency and capacity dimensions that other benchmarks miss.

**How it works:** Two memory levels: factual (explicit information) and reflective (derived insights/summaries). Two scenarios: participation (agent is in the conversation) and observation (agent watches a conversation). Designed to expose gaps that single-scenario benchmarks hide.

**Metrics:** Four metrics: **accuracy** (correctness), **recall** (coverage), **capacity** (how much can be stored), **temporal efficiency** (time to retrieve).

---

#### MemoryBench (MemoryBench.ai / Supermemory)
- **Source:** Supermemory.ai
- **Repo:** [supermemoryai/memorybench](https://github.com/supermemoryai/memorybench)
- **Docs:** [supermemory.ai/docs/memorybench](https://supermemory.ai/docs/memorybench/overview)

**What it measures:** Cross-provider comparison of memory systems on standard benchmark datasets. Tests accuracy, latency, and token cost simultaneously.

**How it works:** Harness wraps LoCoMo, LongMemEval, and ConvoMem. Supports Supermemory, Mem0, and Zep as providers. Pipeline: INGEST → INDEX → SEARCH → ANSWER → EVALUATE.

**How to run:**
```bash
bun install
# Configure .env.local with provider + judge model API keys
bun run src/index.ts run -p supermemory -b locomo
bun run src/index.ts run -p mem0 -b longmemeval
```

**Metrics:** MemScore — a composite triple: `Accuracy% / Latency(ms) / ContextTokens`. E.g., "86% / 145ms / 1823tok". Designed to prevent gaming a single dimension.

**Why it's useful:** One command to compare memory providers on established benchmarks. If you want to benchmark a file-based memory system against Mem0 or Zep, plug into this harness.

---

#### Agent Memory Benchmark (AMB / Vectorize)
- **Source:** Vectorize / Hindsight — March 2026
- **Website:** [agentmemorybenchmark.ai](https://agentmemorybenchmark.ai)
- **Repo:** [vectorize-io/agent-memory-benchmark](https://github.com/vectorize-io/agent-memory-benchmark)
- **Blog:** [Manifesto post](https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark)

**What it measures:** Memory in agentic workflows (multi-step planning, tool-use, research tasks) rather than simple chatbot conversations. Four dimensions: accuracy, speed, cost, usability.

**How it works:** v1 wraps 6 datasets:
- LoComo (multi-hop reasoning)
- LongMemEval (knowledge updates)
- LifeBench (multi-source personalization)
- MemBench (memory abstraction levels)
- MemSim (comparative/aggregative questions)
- PersonaMem (preference tracking)

Two evaluation modes: **single-query** (one retrieval call, fast) and **agentic** (multiple LLM-driven queries, higher accuracy, higher cost).

**How to run:** Open-source harness at GitHub. Evaluation prompts and methodology fully published for independent reproduction.

**Metrics:** Accuracy per dataset, plus latency and token costs in agentic mode. Rejects single-metric rankings.

**Note:** This harness was designed to be provider-agnostic. You can plug in any memory backend and compare against Hindsight's published scores.

---

#### MemoryBench (arXiv 2510.17281)
- **Source:** October 2025
- **Paper:** [arXiv:2510.17281](https://arxiv.org/abs/2510.17281)
- **Dataset:** [HuggingFace](https://huggingface.co/) (see paper for links)

**What it measures:** Continual learning from user feedback — whether memory systems can learn from accumulated explicit and implicit feedback over time. Tests declarative and procedural knowledge.

**How it works:** User feedback simulation framework across 11 datasets: Locomo, DialSim, LexEval, JuDGE, IdeaBench, LimitGen-Syn, WritingPrompts, HelloBench, WritingBench, NF-Cats, SciTechNews. Covers legal, academic, and open-domain data in multiple languages.

**Metrics:** Task accuracy across feedback rounds. Key finding: existing memory systems are not effective at utilizing procedural knowledge to improve performance.

---

### Category 6: Commercial Benchmark Claims (Evaluate with Skepticism)

---

#### Deep Memory Retrieval (DMR)
- **Source:** MemGPT / Letta team — 2023
- **Original context:** MemGPT paper, [arXiv:2310.08560](https://arxiv.org/abs/2310.08560)
- **Dataset:** MSC (Multi-Session Chat, Facebook AI)

**What it measures:** Consistency of answers about prior conversation sessions. Agent is asked a question about a topic from sessions 1-5.

**How it works:** 5-session MSC conversations (~60 messages per conversation). Agent must retrieve and answer from stored memory. Scored against gold answers with accuracy and F1.

**Published scores (as of early 2026):**
- MemGPT: ~93.4%
- Zep (gpt-4-turbo): 94.8%
- Zep (gpt-4o-mini): 98.2%

**Critical caveat:** Each conversation contains only ~60 messages, which fits comfortably in any modern LLM context window. A simple "stuff it all in context" strategy would likely match or beat these scores. The MemGPT team designed DMR before LLMs had large context windows. It should not be used as evidence of memory system quality for modern use cases.

---

## Benchmark Comparison Quick Reference

| Benchmark | Sessions | Scale | Tasks | Contradictions | Public | Harness |
|-----------|----------|-------|-------|---------------|--------|---------|
| LoCoMo | Up to 32 | ~16K tokens | QA, summarization, dialog | No | Yes | Scripts per model |
| LongMemEval | ~40-500 | 115K-1.5M | 5 memory types | Partial (knowledge update) | Yes (HF) | Python + GPT-4o judge |
| MemoryAgentBench | Multi-turn | Variable | 4 competencies | Yes (CR-MH hardest) | Yes (HF) | Python + .env |
| BEAM | Long conv | Up to 10M | QA | No | Yes (repo) | See repo |
| PersonaMem-v2 | Multi-session | 32K-1M | User modeling | Implicit (preference drift) | Yes (HF) | Inference scripts |
| LifeBench | Year-long | 5K events/user | 5 types incl. ND | TKU task | Yes (GitHub) | See repo |
| MemSim/MemDaily | Daily | Variable | 5 QA types | Via BRNet | Generated | GitHub |
| MemBench | Multi-scenario | Variable | 4 metrics | No | Yes (GitHub) | See repo |
| BABILong | Single doc | Up to 50M | 20 reasoning types | No | Yes (HF) | Standard HF eval |
| LMEB | N/A (embeddings) | N/A | 193 retrieval tasks | No | Yes (HF) | See repo |
| AMB (Vectorize) | Agentic | Variable | 6 datasets | Yes | Harness is public | bun CLI |
| MemoryBench (Super) | Conversation | Variable | 3 datasets | No | Harness is public | bun CLI |
| DMR | 5 sessions | ~60 msgs | Factual recall | No | Via MSC | ParlAI |
| TRACE | N/A (fine-tuning) | N/A | 8 task domains | Forgetting | Yes (GitHub) | Python pipeline |

---

## Recommended Evaluation Stack

For a file-based memory graph system, I'd run these three tiers:

**Tier 1 — Baseline (do these first):**
- LongMemEval_S — establishes your baseline on the current academic standard
- LoCoMo — gives you a number comparable to Mem0, Zep, MemGPT published claims

**Tier 2 — Differentiated capabilities:**
- MemoryAgentBench CR tasks — specifically tests contradiction/update handling (the hard problem)
- PersonaMem at 128K — tests implicit preference tracking
- LMEB episodic + semantic tasks — validates your embedding model choice before building on top of it

**Tier 3 — Stress test:**
- BEAM — tests whether the architecture holds at 10M-token scale
- LifeBench ND tasks — tests non-declarative memory (the rarest test)

---

## Sources

- [LoCoMo paper (arXiv:2402.17753)](https://arxiv.org/abs/2402.17753)
- [LoCoMo repo (snap-research/locomo)](https://github.com/snap-research/locomo)
- [LoCoMo project page (snap-research.github.io)](https://snap-research.github.io/locomo/)
- [LongMemEval paper (arXiv:2410.10813)](https://arxiv.org/abs/2410.10813)
- [LongMemEval repo (xiaowu0162/LongMemEval)](https://github.com/xiaowu0162/LongMemEval)
- [LongMemEval ICLR 2025 poster](https://iclr.cc/virtual/2025/poster/28290)
- [LoCoMo-Plus paper (arXiv:2602.10715)](https://arxiv.org/abs/2602.10715)
- [MemoryAgentBench paper (arXiv:2507.05257)](https://arxiv.org/abs/2507.05257)
- [MemoryAgentBench repo (HUST-AI-HYZ)](https://github.com/HUST-AI-HYZ/MemoryAgentBench)
- [MemoryAgentBench HuggingFace dataset](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench)
- [BEAM paper (arXiv:2510.27246)](https://arxiv.org/abs/2510.27246)
- [BEAM repo](https://github.com/mohammadtavakoli78/BEAM)
- [Hindsight BEAM SOTA post](https://hindsight.vectorize.io/blog/2026/04/02/beam-sota)
- [BABILong paper (arXiv:2406.10149)](https://arxiv.org/abs/2406.10149)
- [BABILong repo (booydar/babilong)](https://github.com/booydar/babilong)
- [LaMP paper (arXiv:2304.11406)](https://arxiv.org/abs/2304.11406)
- [LaMP repo (LaMP-Benchmark/LaMP)](https://github.com/LaMP-Benchmark/LaMP)
- [LaMP benchmark website](https://lamp-benchmark.github.io/)
- [PersonaMem repo (bowen-upenn/PersonaMem)](https://github.com/bowen-upenn/PersonaMem)
- [PersonaMem-v2 repo (bowen-upenn/PersonaMem-v2)](https://github.com/bowen-upenn/PersonaMem-v2)
- [PersonaMem-v2 HuggingFace](https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2)
- [MemSim paper (arXiv:2409.20163)](https://arxiv.org/abs/2409.20163)
- [MemSim repo (nuster1128/MemSim)](https://github.com/nuster1128/MemSim)
- [LifeBench paper (arXiv:2603.03781)](https://arxiv.org/abs/2603.03781)
- [LifeBench repo](https://github.com/1754955896/LifeBench)
- [LMEB paper (arXiv:2603.12572)](https://arxiv.org/abs/2603.12572)
- [LMEB repo (KaLM-Embedding/LMEB)](https://github.com/KaLM-Embedding/LMEB)
- [LMEB HuggingFace dataset](https://huggingface.co/datasets/KaLM-Embedding/LMEB)
- [MemBench paper (arXiv:2506.21605)](https://arxiv.org/abs/2506.21605)
- [MemBench ACL 2025](https://aclanthology.org/2025.findings-acl.989/)
- [MemBench repo (import-myself/Membench)](https://github.com/import-myself/Membench)
- [MemoryBench (arXiv:2510.17281)](https://arxiv.org/abs/2510.17281)
- [MemoryRewardBench paper (arXiv:2601.11969)](https://arxiv.org/abs/2601.11969)
- [MemoryRewardBench repo (LCM-Lab/MemRewardBench)](https://github.com/LCM-Lab/MemRewardBench)
- [Evo-Memory paper (arXiv:2511.20857)](https://arxiv.org/abs/2511.20857)
- [TRACE paper (arXiv:2310.06762)](https://arxiv.org/abs/2310.06762)
- [TRACE repo (BeyonderXX/TRACE)](https://github.com/BeyonderXX/TRACE)
- [LLM-KG-Bench repo (AKSW/LLM-KG-Bench)](https://github.com/AKSW/LLM-KG-Bench)
- [Zep paper (arXiv:2501.13956)](https://arxiv.org/abs/2501.13956)
- [MemoryBench (Supermemory) repo](https://github.com/supermemoryai/memorybench)
- [Agent Memory Benchmark (Vectorize) repo](https://github.com/vectorize-io/agent-memory-benchmark)
- [AMB Manifesto post](https://hindsight.vectorize.io/blog/2026/03/23/agent-memory-benchmark)
- [MSC (Facebook AI) ParlAI project](https://parl.ai/projects/msc/)
- [MemGPT paper (arXiv:2310.08560)](https://arxiv.org/abs/2310.08560)
- [NeedleInAHaystack repo (gkamradt)](https://github.com/gkamradt/LLMTest_NeedleInAHaystack)
- [Mem0 benchmark comparison post](https://mem0.ai/blog/benchmarked-openai-memory-vs-langmem-vs-memgpt-vs-mem0-for-long-term-memory-here-s-how-they-stacked-up)
- [Mem0 research paper (ECAI 2025, arXiv:2504.19413)](https://mem0.ai/research)
- [Cognee memory tools evaluation](https://www.cognee.ai/blog/deep-dives/ai-memory-tools-evaluation)
- [State of AI Agent Memory 2026 (Mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Letta benchmarking post](https://www.letta.com/blog/benchmarking-ai-agent-memory)

## Cross-References

- [synthesis/llm-memory-organization.md](../synthesis/llm-memory-organization.md) — memory architecture best practices that these benchmarks test against
- [synthesis/eval-frameworks-research.md](../synthesis/eval-frameworks-research.md) — broader eval framework landscape
- [research/multi-turn-agent-eval.md](multi-turn-agent-eval.md) — multi-turn agent evaluation research

## Open Questions

1. **BEAM dataset access:** The GitHub repo exists but public availability of the full 10M-token dataset needs confirmation. The paper says it's available but the exact download mechanism needs verification.

2. **MemoryBench (Supermemory) ConvoMem dataset:** The third dataset wrapped by Supermemory's harness ("ConvoMem") is not a published academic benchmark — it appears to be internal. Unclear if it's public or proprietary.

3. **LoCoMo at 10 conversations:** The original LoCoMo dataset is only 10 conversations. Whether this is statistically sufficient for robust system comparison is questionable. LoCoMo-Plus extends it but is brand new (Feb 2026) without widespread adoption.

4. **LLM-KG-Bench applicability:** The LLM-KG-Bench framework (AKSW/LLM-KG-Bench v3.0) focuses on RDF/SPARQL tasks and KG engineering, not directly on whether a memory graph stores and retrieves concepts correctly. There's a gap between "can the LLM write SPARQL" and "does the knowledge graph memory architecture work." KG-LLM-Bench (arXiv:2504.07087) is more applicable but also focuses on KG reasoning, not memory architecture evaluation per se.

5. **Association/fuzzy search quality:** No benchmark directly tests whether the memory system can find related but not-identical concepts (the fuzzy search quality question). LMEB gets closest by testing embedding models, but end-to-end fuzzy association quality remains unmeasured. This may be the most important gap for file-based memory graphs.

6. **Contradiction resolution remains unsolved:** All benchmarks that test CR-MH (multi-hop conflict resolution) show best-in-class scores at or below 6%. This should be treated as a known hard problem, not a solvable gap in current systems.
