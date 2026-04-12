# Mastra Memory System

*Researched: 2026-04-12*

## Summary

Mastra is a TypeScript agent framework (from the Gatsby team) with a layered memory architecture that handles three distinct problems: short-term context management, structured persistent state, and long-context compression. The system is built around a SQL-backed storage foundation with optional vector augmentation, and segments memory into four addressable types: message history, working memory, semantic recall, and observational memory.

The key innovation is Observational Memory (OM) — a system of two background LLM agents (Observer and Reflector) that compress conversation history into a dense, prioritized, append-only observation log. This replaces both traditional message-windowing and RAG retrieval for long-running agents, achieving state-of-the-art benchmark results (94.87% on LongMemEval with gpt-5-mini) while being significantly cheaper due to stable prompt prefixes that cache aggressively.

There is no native graph/relationship layer in Mastra's core memory system. Connections between memory items are implicit and semantic — the vector store provides similarity-based recall, and the Observer/Reflector agents impose narrative structure on observations through the compression process itself. GraphRAG exists as a separate RAG tool for document retrieval and is not part of the agent memory subsystem.

## Key Findings

1. **Storage model is SQL-first, vector-optional.** The base layer is relational (libSQL, PostgreSQL, MongoDB, Upstash, DynamoDB, and others), not a graph or vector database. A separate vector store is required only when semantic recall or OM vector retrieval is enabled. The `MastraCompositeStore` pattern lets you route different data types to different backends.

2. **Two primary identity dimensions: resource + thread.** Every memory operation scopes to a `resourceId` (stable user/entity identifier) and `threadId` (conversation session). These are not interchangeable — the thread has an owner (resourceId) that's immutable after creation. This also gates cross-thread recall: set `scope: 'resource'` to retrieve memories across all threads for a user.

3. **Observational Memory is the architectural breakthrough.** Instead of maintaining raw message history or dynamically injecting retrieved chunks per turn, OM builds an append-only observation log that sits in the prompt prefix. The context window has two blocks: observations (compressed history) + recent raw messages. This keeps the prefix stable — enabling aggressive prompt caching — while compressing 5–40x for tool-heavy agents.

4. **"Observable" refers to the Observer agent, not event subscriptions.** The term "observational memory" means an LLM agent (Observer) watches conversations and writes structured observations. There is no event emitter or pub/sub architecture. It is not "observable" in the reactive programming sense.

5. **Working memory is a free-text scratchpad with tool-based writes.** The agent uses an `updateWorkingMemory` tool to persist a Markdown string (or validated JSON via Zod schema). It is not automatic — the agent must decide to call the tool. Templates are suggestions, not enforced schemas (the LLM has discretion).

6. **Memory is injected via processor pipeline, not at inference time.** Mastra inserts `MessageHistory`, `SemanticRecall`, and `WorkingMemory` as input/output processors that run before/after the LLM call. This is a middleware pattern — it transforms the message array before the model sees it.

7. **No native graph representation.** Relationships between memory items are not modeled explicitly. Semantic proximity is the only connection primitive (via vector similarity). Observation groups have a range-based positioning system and reflection provenance tracking, but this is internal bookkeeping for OM's compression process — not a user-facing graph model.

8. **Observational Memory shipped in Mastra 1.0** with plugins for LangChain, Vercel AI SDK, and others, making it available beyond the Mastra framework itself.

## Details

### Architecture Overview

```
Agent
 └── Memory (config object)
      ├── Storage (SQL backend) — required
      ├── Vector store — optional; required for semantic recall
      ├── Embedder — optional; required for semantic recall
      └── Options
           ├── lastMessages: N       — message history window
           ├── workingMemory: {...}  — structured persistent state
           ├── semanticRecall: {...} — embedding-based retrieval
           └── observationalMemory: {...} — OM compression pipeline
```

The `Memory` class extends `MastraMemory` (from `@mastra/core`). It lives in `packages/memory/src/index.ts`. The agent passes it a `Memory` instance, which attaches input and output processors automatically unless manually overridden.

### Data Model

**Thread & Message Tables**

Threads are the primary organizational unit:
```
Thread {
  id: string
  title: string
  resourceId: string      // owner — immutable after creation
  metadata: {
    workingMemory?: string  // thread-scoped working memory
    clone?: { sourceId }    // for cloned threads
  }
  createdAt: Date
}

Message {
  id: string
  threadId: string
  resourceId: string
  role: 'user' | 'assistant' | 'system' | 'tool'
  content: CoreMessage content parts
  createdAt: Date
}
```

Working memory for resource-scoped mode lives in a separate `Resource` table, not in thread metadata.

**Observation Groups (OM)**

The observation log is stored as structured text with XML-like wrapper markers:
```typescript
interface ObservationGroup {
  id: string;      // 8-byte hex string (crypto-generated)
  range: string;   // comma-separated "start:end" position references
  content: string; // the observation text
  kind?: string;   // "reflection" for Reflector-derived content
}
```

These are serialized into the conversation context as `<observation-group id="..." range="...">` blocks. Parsing uses regex extraction, not a DOM parser.

### Memory Types in Detail

#### 1. Message History

The simplest layer. Configured with `lastMessages: N` to bound context size. Implemented as a `MessageHistory` input/output processor:
- **Input:** Fetches the last N messages from storage, prepends to conversation
- **Output:** Persists new messages to storage after LLM response

Setting `lastMessages: false` skips history loading entirely (useful for stateless agents).

#### 2. Working Memory

A single Markdown string (or JSON object) the agent actively maintains. Behavior:

- **Read:** Injected into the system message each turn
- **Write:** Agent calls the `updateWorkingMemory` tool explicitly
- **Scope:** `resource` (default — persists across threads for same user) or `thread` (per-conversation)
- **Storage location:** Thread metadata field (thread-scope) or Resource table (resource-scope)

Two tool versions exist:
- `updateWorkingMemory` — replace semantics for Markdown, merge semantics for schema-based
- `__experimental_updateWorkingMemoryToolVNext` — find-and-replace with append fallback; requires `updateReason` enum (`append-new-memory`, `clarify-existing-memory`, `replace-irrelevant-memory`)

Two formats:
```typescript
// Markdown template (replace semantics)
workingMemory: {
  enabled: true,
  template: `# User Profile\n- Name:\n- Location:\n- Preferences:`
}

// Zod schema (merge semantics — only specify fields to update)
workingMemory: {
  enabled: true,
  schema: z.object({
    name: z.string().optional(),
    preferences: z.object({ style: z.string() }).optional()
  })
}
```

Concurrency issue: concurrent tool calls can race on working memory. Mastra uses per-resource/thread mutexes to prevent overwrites.

Critical limitation surfaced in GitHub issue #2838: templates are suggestions, not enforced schemas. The LLM can write arbitrary content regardless of template structure. Zod schema mode is the stricter option, using tool-call validation to enforce types.

#### 3. Semantic Recall

RAG-based retrieval using vector embeddings. Requires a vector store and an embedder.

```typescript
semanticRecall: {
  topK: 3,          // similar messages to retrieve
  messageRange: 2,  // adjacent messages to include per match for context
  scope: 'resource' // 'thread' (default) or 'resource' (cross-thread)
}
```

How it works:
- On input: vector similarity search against stored embeddings for current query text
- On output: all new messages (user, assistant, tool calls/results) are embedded and stored

Supports multiple embedding providers: OpenAI (`text-embedding-3-small`, `text-embedding-3-large`), Google Gemini, local FastEmbed, any AI SDK-compatible model.

The `recall()` method supports programmatic semantic search:
```typescript
const { messages } = await memory.recall({
  threadId: 'thread-123',
  vectorSearchString: 'query text',
  threadConfig: { semanticRecall: true }
})
```

Vector cleanup is batched in chunks of 100 to avoid pool exhaustion. Deleting a thread also deletes orphaned vector embeddings.

Embedding cache uses xxhash (fast, low memory) to avoid re-embedding identical content within a process lifetime.

#### 4. Observational Memory (OM)

The flagship feature. Replaces both working memory and message history for long-running agents.

**Architecture:**

The context window has a fixed structure:
```
[System prompt]
[Observation block]        ← compressed history, append-only, stable
[Recent raw messages]      ← current session, shrinks as Observer runs
[Current user message]
```

**Two background agents:**

*Observer* — triggers when unobserved messages exceed `messageTokens` threshold (default: 30,000):
- Reads unobserved messages
- Outputs dated, emoji-prioritized observations in two-level bullet format
- Tracks `<current-task>` and `<suggested-response>` for continuity
- Typical compression: 5–40x

*Reflector* — triggers when observation log exceeds `observationTokens` threshold (default: 40,000):
- Consolidates related observations
- Identifies patterns
- Removes superseded information
- Outputs `<observations>` + optional `<suggested-response>`

Observation format produced by Observer:
```
Date: 2026-01-15

- 🔴 12:10 User building Next.js app with Supabase auth, due January 22nd
  - 🔴 12:10 Server components with client-side hydration
  - 🟡 12:12 Asked about middleware for protected routes
  - ✅ 12:15 App named "Acme Dashboard" — confirmed
```

Priority system: 🔴 high (explicit facts, unresolved goals), 🟡 medium (project details, tool results), 🟢 low (minor details), ✅ completed.

Temporal anchoring: each observation can carry up to three date types — creation date, referenced date (the date mentioned in content), and relative date. This is what gives OM its strong performance on temporal reasoning tasks.

**Configuration:**
```typescript
observationalMemory: {
  model: 'google/gemini-2.5-flash',  // default; needs 128K+ context
  observation: {
    messageTokens: 30_000,      // Observer trigger threshold
    bufferTokens: 0.2,          // Pre-compute every 20% accumulation
    bufferActivation: 0.8,      // Clear 80% of messages when activating
    blockAfter: 1.2,            // Force-sync at 1.2x threshold
    previousObserverTokens: 10_000, // Recent observation context for Observer
  },
  reflection: {
    observationTokens: 40_000,  // Reflector trigger threshold
    bufferActivation: 0.5,      // Start background reflection at 50%
    blockAfter: 1.2,
  },
}
```

**Async buffering:** By default, OM pre-computes observations in the background as tokens accumulate. When the threshold hits, buffered content activates instantly instead of causing a synchronous pause. This can be disabled with `bufferTokens: false`.

**Retrieval mode (experimental):** OM can expose a `recall` tool to the agent that pages through raw messages and performs semantic search across observation groups:
```typescript
observationalMemory: {
  retrieval: true,           // browsing only
  retrieval: { vector: true, scope: 'thread' },  // + semantic search
}
```

**Token-tiered model routing:** Cost optimization by routing smaller inputs to cheaper models:
```typescript
observation: {
  model: new ModelByInputTokens({
    upTo: {
      5_000: 'openrouter/mistralai/ministral-8b-2512',
      20_000: 'openrouter/mistralai/mistral-small-2603',
      40_000: 'openai/gpt-5.4-mini',
      1_000_000: 'google/gemini-3.1-flash-lite-preview',
    },
  }),
}
```

**Degenerate output detection:** The system samples 200-character windows from Observer output; if >40% are duplicates or a single line exceeds 50,000 characters, it's flagged as degenerate and discarded. This prevents LLM repeat-penalty loops from poisoning the observation log.

**Benchmark results:**
| Model | LongMemEval Score |
|-------|------------------|
| gpt-5-mini | 94.87% (highest ever recorded) |
| gemini-3-pro-preview | 93.27% |
| gpt-4o | 84.23% |

Per-category (gpt-5-mini): Single-session-preference 100%, Knowledge-update 96.2%, Temporal-reasoning 95.5%, Multi-session 87.2% (hardest).

Outperforms the oracle (configuration given only answer-containing conversations) — suggesting observations are more useful than raw filtered source data.

### Read/Write Patterns

**Automatic writes (no agent action required):**
- New messages are persisted to storage after every LLM response (MessageHistory processor)
- Embeddings are created and stored (SemanticRecall processor)
- Observer and Reflector run asynchronously in the background (OM)

**Tool-based writes (agent must decide to call):**
- `updateWorkingMemory` — stores Markdown or JSON to the working memory field
- `__experimental_updateWorkingMemoryToolVNext` — find-and-replace variant
- `recall` (OM retrieval mode) — agent reads history by calling this tool

**Programmatic writes (application code):**
```typescript
// Pre-populate working memory during thread creation
const thread = await memory.createThread({
  threadId: 'thread-123',
  metadata: { workingMemory: '# Profile\n- Name: Sam' }
})

// Read working memory directly
const wm = await memory.getWorkingMemory({ threadId, resourceId })

// Override memory options per-request
await agent.stream(messages, {
  threadId,
  resourceId,
  memoryOptions: { lastMessages: 5, semanticRecall: { topK: 2 } }
})
```

**Read-only mode:**
Setting `readOnly: true` on the Memory instance prevents message persistence and disables the `updateWorkingMemory` tool. Useful for routing agents or sub-agents that should observe but not write.

### Processor Pipeline Integration

Mastra injects memory as processors in the agent's pipeline:

```
Input processors:  [MemoryProcessors] → [userInputProcessors]
Output processors: [userOutputProcessors] → [MemoryProcessors]
```

Memory reads happen before the LLM sees the messages. Memory writes happen after the LLM responds, and only if no output guardrail aborted the response. This means guardrails can prevent bad content from being persisted — a safe default.

Manual control: if you explicitly add a `MessageHistory` or `SemanticRecall` processor to your processor list, Mastra won't auto-add it. This enables custom ordering or configuration.

### Multi-Agent Memory Sharing

When one agent calls another via Mastra's delegation system:
- Delegation creates deterministic resource and thread IDs for sub-agents
- By default, sub-agent memory is isolated from the calling agent
- To share memory: pass matching `resourceId` and `threadId` values in direct calls
- Thread cloning: `cloneThread()` copies a thread's messages, working memory, and OM state to a new thread ID

### What "Observable" Actually Means

This is a naming clarification. "Observational Memory" does not mean:
- Event streams or pub/sub
- Reactive memory that agents can subscribe to
- Change notifications when memory is updated

It means: an LLM agent (the Observer) watches conversation events and writes observations about them. The metaphor is observational, not reactive-programming-observable. There is no event emitter. Monitoring is handled by Mastra Studio's Memory tab (token progress bars, observation status badges) — this is UI visualization, not a programmatic event API.

### Connections and Relationships

No native graph model. How associations work in practice:
1. **Semantic proximity** — vector embeddings give soft connections between messages and observations based on meaning
2. **Temporal structure** — observations are dated and grouped, giving time-based ordering
3. **Observation provenance** — `deriveObservationGroupProvenance()` maps reflected sections back to source observation groups by content intersection (this is internal OM bookkeeping, not a user-facing API)
4. **Thread/resource scoping** — the resource dimension provides a coarse user-level grouping across threads

GraphRAG (separate from memory) provides graph-traversal for document retrieval. It is not part of the agent memory system. The `createGraphRAGTool()` is added to an agent's tool list as a standalone capability, not as a memory layer.

### Storage Backends

10 supported adapters:
- libSQL (default, no server required)
- PostgreSQL
- MongoDB
- Upstash
- Cloudflare D1
- Cloudflare KV / Durable Objects
- Convex
- DynamoDB
- LanceDB
- Microsoft SQL Server

Record size limits matter for three: DynamoDB (400 KB), Convex (1 MiB), Cloudflare D1 (1 MiB). Large tool outputs should be externalized to object storage.

`MastraCompositeStore` routes different data categories to different backends — e.g., memory to libSQL for latency, workflows to PostgreSQL for durability, observability to ClickHouse for throughput.

Vector store backends for semantic recall and OM retrieval: libSQL (via libsql-vector), PostgreSQL (PgVector — IVFFlat default, HNSW recommended for OpenAI embeddings), Upstash Vector.

### Working Memory vs. Long-Term Memory

| | Working Memory | Semantic Recall | Observational Memory |
|--|--|--|--|
| Storage | SQL (metadata field) | SQL + vector | SQL + OM engine |
| Size | Small (fits in prompt) | Grows indefinitely | Grows indefinitely |
| Access | Always injected into context | Query-time retrieval | Compressed into observation block |
| Write mechanism | Agent tool call | Automatic (post-turn) | Automatic (background) |
| Format | Markdown or JSON | Raw messages | Compressed dated log |
| Good for | User preferences, task state | Relevant past context | Long-running event logs |
| Version | Stable | Stable | Shipped in Mastra 1.0 |

OM replaces both working memory and message history for long-running agents, with better accuracy and lower cost than semantic recall per the Mastra team's benchmarks.

### Source Code Map

```
packages/memory/
├── src/
│   ├── index.ts                           # Memory class (extends MastraMemory)
│   ├── tools/
│   │   ├── working-memory.ts             # updateWorkingMemory tool definitions
│   │   └── om-tools.ts                   # recall tool definition
│   └── processors/
│       ├── index.ts                      # Processor exports
│       └── observational-memory/
│           ├── observational-memory.ts   # Main OM implementation
│           ├── processor.ts              # OM processor wrapping
│           ├── observer-agent.ts         # Observer LLM agent
│           ├── observer-runner.ts        # Observer execution
│           ├── reflector-agent.ts        # Reflector LLM agent
│           ├── reflector-runner.ts       # Reflector execution
│           ├── observation-groups.ts     # ObservationGroup data structure
│           ├── buffering-coordinator.ts  # Async pre-computation
│           ├── thresholds.ts             # Token threshold logic
│           ├── token-counter.ts          # Local token estimation (tokenx)
│           ├── model-by-input-tokens.ts  # Cost-tiered model routing
│           ├── anchor-ids.ts             # Hex ID generation
│           ├── markers.ts                # XML marker handling
│           ├── date-utils.ts             # Temporal anchoring
│           ├── types.ts                  # All type definitions
│           └── constants.ts              # Default thresholds
```

## Sources

- [Memory overview | Mastra Docs](https://mastra.ai/docs/memory/overview)
- [Observational Memory | Mastra Docs](https://mastra.ai/docs/memory/observational-memory)
- [Working Memory | Mastra Docs](https://mastra.ai/docs/memory/working-memory)
- [Semantic Recall | Mastra Docs](https://mastra.ai/en/docs/memory/semantic-recall)
- [Storage | Mastra Docs](https://mastra.ai/docs/memory/storage)
- [Agent memory | Mastra Docs](https://mastra.ai/docs/agents/agent-memory)
- [Memory Class Reference | Mastra Docs](https://mastra.ai/reference/memory/memory-class)
- [Announcing Observational Memory | Mastra Blog](https://mastra.ai/blog/observational-memory)
- [Observational Memory: 95% on LongMemEval | Mastra Research](https://mastra.ai/research/observational-memory)
- [Using Mastra's Agent Memory API | Mastra Blog](https://mastra.ai/blog/agent-memory-guide)
- [mastra-ai/mastra GitHub repository](https://github.com/mastra-ai/mastra)
- [Memory processors | GitHub (mdx)](https://github.com/mastra-ai/mastra/blob/main/docs/src/content/en/docs/memory/memory-processors.mdx)
- [Issue #2838: Working Memory template bugs](https://github.com/mastra-ai/mastra/issues/2838)
- [Issue #5872: Memory vs MastraMemory type mismatch](https://github.com/mastra-ai/mastra/issues/5872)
- [packages/memory/src/index.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/index.ts)
- [packages/memory/src/tools/working-memory.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/tools/working-memory.ts)
- [packages/memory/src/tools/om-tools.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/tools/om-tools.ts)
- [packages/memory/src/processors/observational-memory/observer-agent.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/processors/observational-memory/observer-agent.ts)
- [packages/memory/src/processors/observational-memory/types.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/processors/observational-memory/types.ts)
- [packages/memory/src/processors/observational-memory/observation-groups.ts](https://raw.githubusercontent.com/mastra-ai/mastra/main/packages/memory/src/processors/observational-memory/observation-groups.ts)
- [GraphRAG | Mastra Docs](https://mastra.ai/docs/rag/graph-rag)

## Cross-References

- [doc/synthesis/llm-memory-organization.md](/Users/michaeldarmousseh/workspace/doc/synthesis/llm-memory-organization.md) — Memory architecture best practices (three-tier model, research-backed)

## Open Questions

1. **Observation group persistence:** Are observation groups stored in the same SQL tables as messages, or in a separate table? The source code suggests they're serialized into the context string, but the exact persistence mechanism for the OM log isn't clear from public source.

2. **Multi-agent OM sharing:** Can two separate agents share the same OM engine (same resourceId, different threadIds) with resource-scoped OM? The docs mark resource-scoped OM experimental and warn about slowness for high-volume users, but the multi-agent coordination story is underdeveloped.

3. **OM conflict resolution:** If two agent calls happen simultaneously with resource-scoped OM, how does the buffering coordinator handle concurrent observation writes? Mutexes cover working memory but the OM concurrency story is not documented publicly.

4. **LangChain/Vercel AI SDK plugins:** Mastra says it shipped OM plugins for these frameworks. What's the integration surface? Can you use Mastra's OM with non-Mastra agents?

5. **Schema validation timing:** Issue #2838 revealed that Zod schema enforcement for working memory happens at tool-call time, not at storage time. Does this mean a non-Mastra write path (e.g., direct `updateWorkingMemory()` call) bypasses schema validation?
