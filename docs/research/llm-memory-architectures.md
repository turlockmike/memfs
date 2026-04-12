# LLM Agent Memory Architectures: Comparative Analysis

*Researched: 2026-04-12*

## Summary

Six major approaches to LLM agent memory each make fundamentally different bets about what the hard problem actually is. MemGPT/Letta bets that the LLM should manage its own memory like an OS process. GraphRAG bets that pre-computed hierarchical summaries can answer questions no vector search can. Mem0 bets that compact extracted facts beat raw conversation history. Zep bets that temporal provenance — knowing *when* a fact was true, not just *that* it was true — is the real unsolved problem. LangGraph bets that memory is an infrastructure concern orthogonal to graph logic, so it should be pluggable. Cognee bets that graph triplets are more retrievable than flat chunks, but the LLM shouldn't have to manage the graph.

The deepest architectural divide is between **LLM-managed memory** (MemGPT/Letta) and **externally-managed memory** (everyone else). The second deepest is between **pre-computed structure** (GraphRAG, Zep, Cognee) and **on-demand extraction** (Mem0, LangGraph). Neither axis has a clear winner — the right choice depends on query patterns, update frequency, and acceptable latency.

## Key Findings

1. **Only MemGPT/Letta gives the LLM direct write access to its own memory.** All other systems use the LLM for extraction/summarization but make external code responsible for persistence and retrieval routing.

2. **Zep is the only system with a principled bi-temporal model.** It tracks both when something happened and when the system learned about it — enabling retroactive correction without data loss. Everyone else either overwrites or marks-deleted.

3. **GraphRAG's global search is architecturally unique.** Its map-reduce over pre-computed community summaries lets it answer "what are the main themes across this corpus?" — a query class vector search fundamentally cannot handle. The cost is massive upfront indexing.

4. **Mem0's key insight is compression.** Rather than retrieving chunks from raw history, it maintains a small set of extracted facts (~7k tokens). This produces 91% lower p95 latency vs full-context and ~26% better accuracy vs OpenAI memory.

5. **LangGraph separates concerns cleanly but provides no memory semantics.** Checkpointers (thread-scoped) vs Stores (cross-thread) is a solid infrastructure split, but LangGraph gives you no opinion on what to store or how to consolidate — you build that yourself.

6. **Cognee is the only system shipping explicit graph maintenance (Memify).** It acknowledges that graphs degrade over time and provides a post-processing pipeline for pruning, re-embedding, and relevancy-based cleanup.

7. **No system has a fully documented garbage collection strategy.** All approach scale the same way: delegate to the underlying store's TTL/retention (Pinecone, Redis), explicit API deletes, or "the LLM will decide to DELETE eventually" (Mem0).

8. **Vector search is a baseline capability everywhere.** The differentiator is what's layered on top: graph traversal (Zep, Mem0^g, Cognee), hierarchical summarization (GraphRAG), or nothing (LangGraph base).

---

## Details

### 1. MemGPT / Letta

**Origin:** UC Berkeley research paper (2023). "Towards LLMs as Operating Systems." The OS metaphor is not decorative — it's the actual design principle: virtual memory paging, where the context window is RAM and external storage is disk.

**Storage Layer:**

Three tiers:
- **Core Memory** — in-context, structured key-value blocks (~86 tokens each in the paper's examples). Pinned to the context window. Editable by the LLM via tool calls.
- **Recall Memory** — the complete conversation history, stored externally as a vector DB table. Searchable but not always in context.
- **Archival Memory** — long-term semantic knowledge base. Also a vector DB table. Distinct from raw history — this is processed, indexed knowledge.

Default local storage: SQLite (the pip package). Production: Postgres (Docker image runs Alembic migrations creating ~42 tables). Vector search layer sits on top of either.

**How Paging Works:**

The LLM itself calls six tools to manage memory:

| Tool | Action |
|------|--------|
| `core_memory_append` | Add to in-context blocks |
| `core_memory_replace` | Update in-context blocks |
| `archival_memory_insert` | Push facts to external archive |
| `archival_memory_search` | Pull relevant facts from archive into context |
| `conversation_search` | Retrieve prior messages by text match |
| `send_message` | Output to user |

The paging decision is entirely the LLM's. When context fills, the system evicts ~70% of messages via recursive summarization — older summaries get progressively compressed. The LLM reasons through what to retain: "User's boyfriend is named James — that's worth remembering" triggers a `core_memory_append` call.

**Retrieval:** Semantic embedding search against the archival vector table. Keyword/text search against recall. No graph traversal.

**Memory Maintenance at Scale:** The "sleep-time compute" enhancement adds asynchronous memory agents that run during idle periods to reorganize and consolidate memory proactively. Not the default behavior.

**Who manages memory:** The LLM. This is the system's defining architectural bet and its biggest risk — the LLM must be reliable enough to make good paging decisions. It also means memory management consumes tokens.

**Key Trade-off:** Maximum flexibility and autonomy vs. token cost and reliability. The LLM can store anything it judges relevant, but it can also forget to store things, store the wrong things, or hit a context crisis mid-conversation.

---

### 2. Microsoft GraphRAG

**Origin:** Microsoft Research, 2024. Paper: "From Local to Global: A Graph RAG Approach to Query-Focused Summarization." Designed specifically for corpus-wide analytical queries, not conversational memory — this distinction matters.

**Storage Layer:**

Apache Parquet files on the local filesystem (or Azure blob). Not a live database — the output of an offline indexing pipeline. Tables:
- `Documents`
- `Text Units` (chunks)
- `Entities` (title, type, description)
- `Relationships` (source, target, description, weight)
- `Communities` (cluster ID, level, parent)
- `Community Reports` (title, summary, rating, key insights in JSON)
- `Covariates` (claims/facts about entities)

Embeddings written to a configured vector store (Cosmos DB, Azure AI Search, or local).

**How the Graph is Built:**

1. Chunk documents into text units
2. LLM extracts entities (title, type, description) and relationships from each chunk
3. LLM self-reflection loop checks extraction completeness
4. Subgraphs merged by entity title+type (deduplication)
5. **Leiden algorithm** applied hierarchically — recursively partitions until communities are below a size threshold. This creates C0 (root, fewest communities) through C3+ (most granular)
6. LLM generates community reports: structured JSON with title, executive summary, impact rating, 5-10 key insights

**Retrieval — the Two Modes:**

- **Local Search**: Vector similarity search for specific entities + their immediate neighborhood. Equivalent to enriched RAG. Fast, precise for entity-specific questions.
- **Global Search**: Map-reduce over community summaries. Each community summary gets a partial answer to the query (map), then all partial answers are synthesized (reduce). This is the unique capability — handles "what are the major themes?" questions that vector search cannot.
- **DRIFT Search** (newer): Hybrid — starts local, expands to global when local hits are insufficient.

**Memory Maintenance / Updates:**

Version 0.5.0+ supports incremental indexing with consistent entity IDs, enabling insert-update-merge. Pre-0.5 required full reindexing. The team describes current incremental indexing as "clunky." Full streaming ingestion is a future goal.

**Who manages memory:** Entirely external. The LLM is a worker in the pipeline (extraction, summarization), not the manager. Users query it; the graph serves results.

**Key Trade-off:** Extraordinary query capability for analytical, corpus-wide questions, in exchange for massive upfront indexing cost (many LLM calls per document), large storage footprint, and poor fit for conversational/real-time memory use cases.

---

### 3. Mem0

**Origin:** YCombinator-backed company (mem0.ai). Academic paper: "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory" (2025). Two variants: base Mem0 (natural language facts) and Mem0^g (graph-enhanced).

**Storage Layer:**

Hybrid three-store architecture:
- **Vector Store** — primary retrieval layer. 24+ supported providers: Qdrant (default), Pinecone, ChromaDB, PGVector, FAISS, Redis, and more. Stores embedded fact representations.
- **Graph Store** (optional, Mem0^g) — Neo4j, Memgraph, Kuzu, Neptune, or Apache AGE. Models entities as nodes, relationships as typed edges.
- **History DB** — SQLiteManager at `~/.mem0/history.db`. Tracks all memory operations (CREATE, UPDATE, DELETE) with timestamps. Audit trail, not retrieval layer.

**Memory CRUD — the Pipeline:**

`memory.add(messages, user_id=...)` triggers:
1. LLM (GPT-4o-mini via function-calling) extracts structured facts from messages, with a 10-message recency window and async-updated conversation summary as context
2. Embedding generated for each fact
3. Vector similarity search finds semantically similar existing memories
4. LLM receives both new facts and similar existing memories, decides for each: ADD / UPDATE / DELETE / NOOP
5. Vector store and graph store updated in parallel
6. Operation logged to SQLite history

**Two Modes:**
- **Infer Mode** (default): Full LLM extraction + conflict resolution pipeline
- **Direct Mode**: Messages embed directly, no LLM processing. Faster and cheaper, loses conflict detection

**Retrieval:**

`memory.search(query, user_id=...)`:
- Query embedded, vector similarity search against scoped memories (user_id/agent_id/run_id/session_id)
- Optional reranker (Cohere, HuggingFace)
- If graph enabled: parallel graph entity extraction + BM25 reranking of related entities
- Graph results in `relations` array alongside vector hits in `memories` array — they don't merge automatically

**Conflict Resolution:**

Temporal inconsistencies resolved by recency preference. Mem0 marks outdated memories as superseded (preserving them for temporal reasoning); Zep physically invalidates them. The paper notes Zep's token consumption explodes to 600k+ tokens at scale partly because it preserves all temporal edges as context.

**Garbage Collection:**

No automatic eviction documented. Relies on: explicit DELETE calls via API, LLM reasoning to issue DELETE operations when facts are superseded, or managed vector store TTL features. The history DB provides an audit trail but not cleanup logic.

**Performance (LOCOMO benchmark, 2025):**
- 26% higher accuracy vs OpenAI memory
- 91% lower p95 latency vs full-context (1.44s vs 17.12s)
- ~1.8k tokens per conversation vs 26k for full-context

**Who manages memory:** Hybrid. The LLM decides what to extract and how to handle conflicts. External code manages storage, indexing, and retrieval routing.

**Key Trade-off:** Best-in-class latency and token efficiency for factual retrieval, but the graph layer (Mem0^g) adds ~3.4x storage overhead and the conflict resolution LLM call adds latency on write. The base variant has a well-documented blind spot for multi-hop relational reasoning.

---

### 4. Zep

**Origin:** Zep (getzep.com). Academic paper: "Zep: A Temporal Knowledge Graph Architecture for Agent Memory" (January 2025). Core engine is Graphiti, open-sourced separately. The defining claim: temporality is the unsolved problem in agent memory.

**Storage Layer:**

**Neo4j** (5.26+) as the primary graph database, using Neo4j's Lucene integration for full-text search. Also supports FalkorDB, Kuzu, and Amazon Neptune + OpenSearch Serverless. All queries use predefined Cypher patterns — the LLM never generates database queries directly. This is an explicit safety/consistency decision.

**Data Model (Graphiti):**

Four entity types:
- **Episodes** — raw ingested data with `t_ref` timestamp. Three forms: message, text, or JSON. Non-lossy data store; everything else derives from episodes.
- **Entities (Nodes)** — extracted people, products, concepts with evolving summaries
- **Facts/Relationships (Edges)** — temporal triplets connecting entities
- **Communities** — clusters of strongly-connected entities with summary information

**The Bi-Temporal Model:**

This is Zep's core architectural differentiator. Every fact edge carries four timestamps:
- `t'_created` — when the fact was ingested (system time)
- `t'_expired` — when the fact was superseded in the system
- `t_valid` — when the fact became semantically true in the world
- `t_invalid` — when the fact became semantically false in the world

When new information contradicts an existing edge, the old edge's `t_invalid` is set to the new edge's `t_valid`. The old edge is not deleted. This preserves the full history for temporal reasoning ("what did we believe about X as of date Y?").

**Episode Ingestion:**

Incremental, not batch. Each episode processes with the previous four messages for context. The speaker entity is automatically extracted. Concurrency controlled by `SEMAPHORE_LIMIT` (default: 10) to prevent LLM provider rate limits.

**Community Detection:**

Label propagation algorithm (not Leiden — a notable difference from GraphRAG). When new entity nodes arrive, the algorithm assigns them to the plurality community of their neighbors. Periodic full refreshes prevent cumulative drift from incremental updates, but the default is incremental to minimize latency and inference cost.

**Retrieval:**

Three parallel search methods, then reranking, then construction:

1. **Search phase (φ)**: Cosine semantic similarity (embeddings) + Okapi BM25 full-text (Lucene) + breadth-first graph traversal — run in parallel, returning semantic edges, entity nodes, and community nodes respectively
2. **Reranking phase (ρ)**: Reciprocal Rank Fusion, Maximal Marginal Relevance, episode-mention frequency, node distance, or cross-encoder LLM reranking
3. **Construction phase (χ)**: Selected nodes and edges formatted into text strings with validity date ranges for the agent context

**Who manages memory:** Externally. The LLM handles extraction tasks (entity extraction, fact extraction, entity resolution) via structured prompts. Retrieval and graph maintenance are done by Graphiti's engine, not the LLM.

**Key Trade-off:** The bi-temporal model is theoretically correct for enterprise use cases with changing facts (employee roles, product states, policy updates). The cost: complexity. Two timelines, four timestamps per edge, periodic community refresh. The paper shows 90% latency reduction vs some baselines, but Mem0's paper characterizes Zep as producing 600k+ token contexts at scale, suggesting the temporal edge preservation creates its own retrieval overhead when poorly bounded.

---

### 5. LangGraph Memory

**Origin:** LangChain team. Memory in LangGraph is deliberately infrastructure-agnostic — the framework provides the plumbing; you provide the semantics.

**Architectural Split:**

LangGraph separates two fundamentally different memory concerns:

**Checkpointers (short-term, thread-scoped):**
- `BaseCheckpointSaver` interface: `.put`, `.put_writes`, `.get_tuple`, `.list`
- Serializes full graph state after each node execution using `JsonPlusSerializer` (msgpack + JSON; pickle fallback for unsupported types)
- Scoped to a single `thread_id` — one conversation
- Backends: `InMemorySaver` (dev), `SqliteSaver`, `PostgresSaver`, `CosmosDBSaver`
- AES encryption support via `EncryptedSerializer` for sensitive state

**Store API (long-term, cross-thread):**
- `BaseStore` interface for persistent key-value storage shared across thread IDs
- Namespaced by tuple: `(user_id, "memories")` — arbitrary namespace length
- Operations: `.put(namespace, key, value)`, `.search(namespace)`, `.asearch(namespace, query=..., limit=n)` (semantic, via embeddings)
- Backends: `InMemoryStore` (dev), `PostgresStore`, `RedisStore` (with vector search), `MongoDBStore`
- Agents access stores via `runtime.store` inside node functions

**How They Interact:**

Checkpointers replay the graph from the last checkpoint within a thread. Stores give agents access to memories from other threads (other users, other sessions). A typical production pattern: Postgres for checkpoints, Redis or Mongo for cross-thread memory.

**Agent Self-Management:**

Agents cannot configure their own persistence infrastructure — that's set at compile time. But agents can programmatically write to stores: `await runtime.store.aput(namespace, key, {"fact": "..."})`. The framework provides no built-in memory extraction or consolidation logic — if you want Mem0-style fact extraction, you build it yourself as a LangGraph node.

**Garbage Collection:**

None built-in. Stores implement whatever TTL/cleanup the underlying backend provides. Thread-scoped checkpoints accumulate indefinitely unless pruned via the `.list` API or backend-level retention policies.

**Key Trade-off:** Maximum architectural flexibility — you can build any memory pattern on top of LangGraph. The cost is that you're building it yourself. LangGraph is the chassis; the memory system is a blank canvas. Teams who want drop-in memory reach for Mem0, Zep, or Letta instead.

---

### 6. Cognee

**Origin:** cognee.ai, open-source framework (topoteretes/cognee). Production: 1M+ pipelines/month, 70+ enterprise adopters (Bayer, etc.) as of 2025.

**Storage Layer:**

Three-store default architecture designed for zero infrastructure setup:
- **Graph Store**: Kuzu (default, embedded, file-based) / Neo4j / FalkorDB / Amazon Neptune / Memgraph
- **Vector Store**: LanceDB (default, file-based) / Qdrant / pgvector / Redis / DuckDB / Pinecone / ChromaDB
- **Relational Store**: SQLite (default) / PostgreSQL

The defaults are all embedded/file-based — no infrastructure required to start. Production deployments swap to managed services.

**Knowledge Graph Construction (the `cognify` pipeline):**

Six stages:
1. Classify documents
2. Check permissions (ownership controls at dataset level)
3. Extract chunks
4. LLM extracts entities and relationships → triplets (subject-relation-object)
5. Generate summaries
6. Embed into vector store + commit edges to graph

For structured/deterministic data: the `add` command can bypass the LLM and translate directly from relational form to graph. Content is deduplicated via hashing; only new/updated files re-processed.

Supports 38+ input formats: PDF, CSV, JSON, audio, images.

**Two Memory Scopes:**
- **Session Memory**: Short-term working memory for agent reasoning
- **Permanent Memory**: Long-term knowledge artifacts with cross-connections in the graph

**Retrieval (14 modes):**

The default `GRAPH_COMPLETION` mode:
1. Vector search identifies relevant graph nodes (as hints)
2. Graph traversal from those nodes builds structured context (triplets + neighborhood)
3. LLM generates answer from graph-structured context

This is architecturally distinct from RAG: vector search is a *pointer* into the graph, not the retrieval result itself. Cognee claims this produces ~90% accuracy vs RAG's ~60%.

Other modes include classic RAG (flat chunk retrieval) and chain-of-thought graph traversal. The 14-mode design lets users tune the cost/accuracy tradeoff.

**Memory Maintenance — Memify Pipeline:**

Cognee is the only system with a documented post-processing maintenance pipeline:
- **Data Pruning**: Remove infrequently-accessed nodes
- **Relevancy Optimization**: Infer which answers were relevant, weight accordingly
- **Embedding Refinement**: Generate workload-specific embeddings

Operates as a plugin-based, parameterized pipeline that runs without disrupting the active knowledge base. Incremental — commits enhancements back safely.

The underlying garbage collection logic (what "infrequently accessed" means, what the threshold is) is not publicly documented at the algorithm level.

**Who manages memory:** Externally. The LLM handles extraction (triplet generation) and retrieval (answer generation). Graph maintenance is done by Memify, not the LLM.

**Key Trade-off:** The most complete out-of-box package among the graph-based systems — zero infrastructure defaults, 14 retrieval modes, explicit maintenance pipeline. The cost is opacity: the "Memphis" maintenance algorithms and Memify pruning criteria are not fully documented. Also: Kuzu + LanceDB + SQLite as defaults is good for development but the upgrade path to production requires coordinated store migration.

---

## Cross-System Comparison

| Dimension | MemGPT/Letta | GraphRAG | Mem0 | Zep | LangGraph | Cognee |
|-----------|-------------|----------|------|-----|-----------|--------|
| **Storage** | SQLite/Postgres + vector DB | Parquet files + vector store | Vector DB + Neo4j + SQLite audit | Neo4j/FalkorDB | Pluggable (Sqlite/Postgres/Redis) | Kuzu/LanceDB/SQLite (defaults) |
| **Connections** | Flat vector memories | Typed entity-relationship graph with community hierarchy | Typed entity-relationship edges (optional) | Temporal triplets with 4-timestamp edges | Key-value namespace, no native graph | Subject-relation-object triplets |
| **Search/Retrieval** | Embedding similarity + keyword | Local: vector+entity graph; Global: map-reduce summaries | Vector similarity + optional BM25+graph | Cosine + BM25 + breadth-first graph traversal, fused | Embedding similarity (Store); exact match (Checkpointer) | Vector hint → graph traversal (default) |
| **Memory Maintenance** | Recursive summarization on eviction + sleep-time agents | Incremental indexing (v0.5+, described as "clunky") | LLM DELETE decisions + manual API | Edge invalidation (not deletion), periodic community refresh | None built-in; delegate to backend | Memify pipeline: pruning + re-embedding |
| **LLM manages memory?** | Yes — LLM calls tools to read/write its own context | No — LLM is extraction worker | Hybrid — LLM decides ADD/UPDATE/DELETE | No — LLM handles extraction; engine manages graph | Optional — agents can write to Store programmatically | No — LLM handles extraction; Memify handles maintenance |
| **Temporal model** | None — recency via eviction | None | Recency preference; marks superseded | Bi-temporal (event time + ingestion time) | None | None |
| **Update cost** | LLM token cost per decision | Full or incremental re-index | LLM extraction + vector upsert per message | Incremental ingestion + edge invalidation | Immediate write to backend | Incremental cognify re-run (hash dedup) |
| **Scale weakness** | LLM reliability for self-management | Massive upfront indexing; poor for real-time | No GC strategy; vector store growth unbounded | Token bloat at scale per Mem0 paper | No semantic consolidation; checkpoints grow forever | Pruning criteria undocumented |

---

## Architectural Trade-offs Summary

**LLM-managed vs. externally-managed memory:**
The MemGPT/Letta bet is that future LLMs will be reliable enough to manage their own context. The bet is plausible given model improvement trajectories, but today it means memory quality is correlated with model capability — weak models make bad paging decisions. All other systems treat the LLM as a worker, not the manager.

**Pre-computed structure vs. on-demand extraction:**
GraphRAG, Zep, and Cognee invest heavily at ingest time to build structures that make retrieval fast and semantically rich. Mem0 and base LangGraph do minimal structure at ingest (extract facts or just checkpoint state) and rely on retrieval-time search. Pre-computed structure wins for query richness; on-demand wins for update latency.

**Temporal fidelity:**
Only Zep has a principled answer to "what did we believe about X as of date Y?" The others either overwrite (LangGraph checkpoints), mark-supersede (Mem0), or don't track time at the edge level at all (Cognee, GraphRAG). For enterprise use cases where facts change (org structures, product states, policy), Zep's model is the right abstraction — but the implementation complexity is real.

**The garbage collection gap:**
None of these systems has a satisfying answer to unbounded memory growth. The honest answer from the field is: delegate to managed vector store TTL, write explicit DELETE logic in your application, or accept that memory grows until it's expensive. Cognee's Memify is the closest thing to a principled maintenance story, but even it relies on access frequency as a proxy for relevance — which fails for infrequently-asked-but-critical facts.

---

## Sources

- [MemGPT: Towards LLMs as Operating Systems (arXiv)](https://arxiv.org/abs/2310.08560)
- [Virtual context management with MemGPT and Letta — Leonie Monigatti](https://www.leoniemonigatti.com/blog/memgpt.html)
- [Letta docs: Memory management](https://docs.letta.com/advanced/memory-management/)
- [Agent Memory: How to Build Agents that Learn and Remember — Letta Blog](https://www.letta.com/blog/agent-memory)
- [Zep: A Temporal Knowledge Graph Architecture for Agent Memory (arXiv)](https://arxiv.org/abs/2501.13956)
- [Zep arXiv full HTML](https://arxiv.org/html/2501.13956v1)
- [Graphiti GitHub (Zep's graph engine)](https://github.com/getzep/graphiti)
- [From Local to Global: A Graph RAG Approach (arXiv)](https://arxiv.org/html/2404.16130v2)
- [GraphRAG default dataflow — Microsoft](https://microsoft.github.io/graphrag/index/default_dataflow/)
- [GraphRAG community detection — Microsoft](https://www.mintlify.com/microsoft/graphrag/concepts/community-detection)
- [LazyGraphRAG — Microsoft Research](https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/)
- [Mem0 architecture deep-dive — DeepWiki](https://deepwiki.com/mem0ai/mem0)
- [Mem0 graph memory docs](https://docs.mem0.ai/open-source/features/graph-memory)
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory (arXiv)](https://arxiv.org/html/2504.19413v1)
- [Mem0 vs. Letta comparison — Vectorize](https://vectorize.io/articles/mem0-vs-letta)
- [LangGraph persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangGraph memory architecture — DEV Community](https://dev.to/sreeni5018/the-architecture-of-agent-memory-how-langgraph-really-works-59ne)
- [LangGraph & Redis integration](https://redis.io/blog/langgraph-redis-build-smarter-ai-agents-with-memory-persistence/)
- [LangGraph MongoDB Store](https://www.mongodb.com/company/blog/product-release-announcements/powering-long-term-memory-for-agents-langgraph)
- [Cognee: How Cognee Builds AI Memory](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory)
- [Cognee Memify post-processing pipeline](https://medium.com/@cognee/cognee-knowledge-graph-optimization-memify-post-processing-pipeline-ce049417d9c3)
- [From RAG to Graphs: How Cognee is Building Self-Improving AI Memory — Memgraph](https://memgraph.com/blog/from-rag-to-graphs-cognee-ai-memory)
- [State of AI Agent Memory 2026 — Mem0 Blog](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AWS blog: Mem0 with ElastiCache + Neptune](https://aws.amazon.com/blogs/database/build-persistent-memory-for-agentic-ai-applications-with-mem0-open-source-amazon-elasticache-for-valkey-and-amazon-neptune-analytics/)

---

## Cross-References

- [doc/synthesis/llm-memory-organization.md](../synthesis/llm-memory-organization.md) — three-tier model and best practices (ship's own memory architecture synthesis)
- [doc/research/filesystem-as-graph-db.md](filesystem-as-graph-db.md) — filesystem as graph database (directly relevant to file-system-based approach)
- [doc/systems/agent-system.md](../systems/agent-system.md) — ship's current memory patterns

---

## Open Questions

1. **Can Zep's bi-temporal model be simplified?** Four timestamps per edge is conceptually correct but operationally complex. Is there a lighter implementation that preserves the key property (fact invalidation without deletion) without full bi-temporality?

2. **What is Cognee's Memify pruning threshold?** "Infrequently accessed" nodes get pruned, but the algorithm and threshold are not public. This matters for critical-but-rare facts.

3. **Does MemGPT/Letta's sleep-time compute change the self-management calculus?** If asynchronous memory agents handle consolidation during idle time, the token cost argument against LLM-managed memory weakens. No published benchmarks on this yet.

4. **GraphRAG for conversational memory?** The framework was designed for static corpus analysis, not real-time conversation. Incremental indexing is improving, but the latency budget per conversation turn is unclear.

5. **How does Mem0's conflict resolution perform on contradictory facts from the same timestamp?** The recency preference heuristic works for temporal supersession, but what about genuine contradictions in a single message?

6. **LangGraph + Mem0/Zep integration pattern?** LangGraph's Store API is generic enough that Mem0 or Zep could be the backend. Is this a documented pattern and what does it cost in abstraction overhead?
