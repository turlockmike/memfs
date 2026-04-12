# Fuzzy/Semantic Search Over Local File Systems for LLM Agent Memory

*Researched: 2026-04-12*

## Summary

An LLM agent that stores knowledge as plain files faces three distinct engineering problems: finding the right files given a natural language query, maintaining the system's quality as it scales to thousands of files, and representing the graph structure that makes knowledge navigable. Each problem has multiple viable approaches, and the right choices depend heavily on whether you prioritize zero-dependency simplicity, accuracy, or latency.

The central tension running through all three problems is the same: plain files are legible to humans and agents alike, but they have no built-in index, no referential integrity, and no query planner. Every capability you want — fuzzy search, deduplication, graph traversal — has to be bolted on. The question is where and how. The most defensible answer in 2025 is a hybrid: store knowledge as Markdown files (the file system wins on legibility and tooling compatibility), maintain a lightweight side-car index (SQLite is the right primitive here), and run semantic search only on top of that index rather than re-embedding from scratch on every query.

The write amplification problem is real but solvable. The graph structure question has a clear winner (inline Markdown links as the source of truth, with a SQLite adjacency table as the query layer). The deduplication problem is the hardest and the most underspecified in the current literature.

---

## Problem 1: Fuzzy/Semantic Search Approaches

### 1.1 Local Embedding Indexes

**How it works.** Chunk file content into passages (typically 256–512 tokens), run each chunk through a local embedding model to produce a dense vector, and store the vectors in a vector index. At query time, embed the query and retrieve the top-K most similar vectors by cosine distance.

**Model comparison for local use.**

| Model | Params | Embedding speed (CPU) | Embedding dim | BEIR top-5 accuracy |
|---|---|---|---|---|
| all-MiniLM-L6-v2 | 22M | ~14.7 ms/1K tokens (~5-14K sentences/sec) | 384 | 78.1% |
| E5-Base-v2 | 125M | ~20.2 ms/1K tokens | 768 | 83.5% |
| BGE-Base-v1.5 | 110M | ~22.5 ms/1K tokens | 768 | 84.7% |
| nomic-embed-text v1 | ~500M | ~41.9 ms/1K tokens | 768 | 86.2% |
| nomic-embed-text v2 | MoE | — | 768 | SOTA multilingual |

For a local file memory system with low resource constraints, **all-MiniLM-L6-v2** is the practical default: 22M parameters, runs on CPU at ~5-14K sentences/second, edge-compatible. The 5-8% accuracy gap vs. larger models is real but acceptable for most memory retrieval use cases. If the corpus is large and documents are long (notes, meeting transcripts), **nomic-embed-text v1** is worth the cost — its 8,192-token context window means you don't have to over-chunk.

**Vector store options for local deployment.**

- **FAISS** — Facebook's library, not a server. Requires you to wire up persistence, updates, and metadata yourself. Maximum control; most work.
- **Chroma** — Embedded database, DuckDB+Parquet under the hood. Developer-friendly API, strong LangChain/LlamaIndex integration. Best for prototyping and mid-scale (under ~100K chunks).
- **LanceDB** — Apache Arrow-based embedded database, designed for multimodal data. Near-in-memory performance from disk. SQLite-like deployment model — single file, no server. Best-in-class for file-system-native agents because it has zero operational overhead and supports hybrid search natively.
- **SQLite + vss extension** — The sqlite-vec extension (or the older sqlite-vss) adds approximate nearest-neighbor search directly into SQLite. Single-file, zero-server, transactional. The query performance doesn't match dedicated vector libraries, but for a memory system with < 50K chunks it's sufficient and unifies FTS and vector search in one database.

**The index maintenance challenge** with embedding-based search: every file write potentially requires re-embedding affected chunks and updating the index. This is manageable with an event-driven architecture (file system watcher triggers incremental updates), but it means you cannot treat the vector index as append-only. More on this in Problem 2.

### 1.2 BM25/TF-IDF over File Content

**How it works.** Build a traditional inverted index over file content. For each query term, look up the postings list, score documents using BM25 (saturation of term frequency, length normalization), and return ranked results.

**BM25 vs TF-IDF.** BM25 is strictly better than raw TF-IDF for document retrieval. The key improvements: term frequency saturation (the 10th occurrence of a word contributes less than the 1st, preventing keyword stuffing from dominating) and explicit document length normalization. Use BM25 everywhere TF-IDF would appear.

**SQLite FTS5** is the correct primitive for local BM25 search. It ships with SQLite (no external dependency), supports boolean queries (AND/OR/NOT), phrase queries, prefix searches, and proximity/NEAR clauses. It maintains its own inverted index as a virtual table and updates incrementally on INSERT/UPDATE/DELETE — which means you can treat it as a live index that mirrors your file content. This is the most underrated option in the ecosystem.

Key FTS5 characteristics:
- Zero server, zero external dependencies beyond SQLite
- Handles millions of small documents efficiently
- Built-in BM25 scoring via the `rank` column
- Customizable tokenizers (supports stemming, stopword filtering)
- Updates atomically with the rest of the database

**ripgrep/rg** is the right tool when you don't want an index at all — brute-force regex search over raw files. At sub-10K files and sub-1GB corpus it's fast enough (~50ms for most queries on an SSD). `ripgrep-all` (rga) extends this to PDFs, docx, epubs, and archives by caching extracted text. Neither is a replacement for a real index at scale, but they're excellent for bootstrapping before an index exists or for interactive ad-hoc queries.

**fzf** provides interactive fuzzy filtering over file names and content lines. It operates on a stream of text, not an index — it's O(n) over the input. Useful as a human-facing exploration tool, but not appropriate for programmatic agent search. The fzf+ripgrep combination (ripgrep produces matching lines, fzf filters interactively) is a solid developer workflow tool.

### 1.3 Hybrid Search (Keyword + Semantic)

**Why hybrid outperforms either alone.** BM25 has precision on exact term matches (code identifiers, names, specific dates) and is fast. Semantic search has recall on paraphrase and concept matches but drifts on exact terms. The standard empirical finding: BM25 Top-1 passage recall ~22%, dense retriever ~48.7%, hybrid pipeline up to 53.4%. For a memory retrieval use case where queries are natural language but memories often contain specific identifiers, hybrid is the right default.

**Reciprocal Rank Fusion (RRF)** is the standard fusion algorithm. It combines ranked lists from BM25 and vector search without requiring score calibration between the two systems. The formula: `RRF(d) = Σ 1/(k + rank(d))` for each ranker, where k is typically 60. It's robust to the score distribution differences between sparse and dense retrievers.

**Dynamic Alpha Tuning (DAT)** is a 2024 improvement: use an LLM to score the top-1 BM25 and top-1 dense result, then adaptively set weights for the fusion. Reported gains of 6-7 points over static weighting. Expensive per query but appropriate for high-stakes retrieval.

**Practical architecture for a file-based agent memory.**
```
Write path: file → extract text → FTS5 index + vector index (incremental)
Query path: query → parallel BM25 (FTS5) + vector search → RRF fusion → rerank top-K → return
```
LanceDB supports hybrid search natively (FTS + vector in one query). For a DIY approach: SQLite FTS5 for the BM25 side, sqlite-vec or LanceDB for the vector side.

### 1.4 File-Name and Path-Based Heuristics

**Structured naming as a search primitive.** Dendron's dot-notation hierarchy (`project.area.topic.md`) encodes the taxonomy directly in file names. This makes path-prefix search equivalent to hierarchical navigation: `SELECT * FROM files WHERE path LIKE 'project.%'` is effectively a subtree query. No full-text index needed for navigation within known hierarchy.

This is the least discussed but most underrated approach for agent memory specifically: if the agent is also the writer, it can enforce a consistent naming convention that makes files partially self-indexing. A file named `meetings/2026-04-12-john-monsod.md` is findable by date, person, and type with a simple pattern match — no embedding required.

**Key design principle:** structured file names eliminate a category of queries entirely. Reserve semantic/vector search for the queries that genuinely require understanding meaning — "what did I learn about Bedrock costs?" — and handle the structural queries (by date, person, topic prefix) through path matching.

### 1.5 How Apple Spotlight Works Under the Hood

Understanding Spotlight illuminates the design space for any local file indexing system.

**Architecture.**
1. `mds` (metadata server) — the root daemon, runs as root, owns the `.Spotlight-V100` database on each volume
2. `mdworker` — worker processes, sandboxed, one per file type being indexed
3. `mdimporter` plugins — type-specific importers (one per UTI, e.g., `RichText.mdimporter` for RTF)
4. FSEvents — the OS-level change notification system that triggers re-indexing

**Indexing pipeline.** FSEvents detects a file change → notifies `mds` → `mds` identifies file UTI → spawns appropriate `mdworker` → `mdworker` calls `mdimporter` to extract metadata + content → writes to `.Spotlight-V100` inverted index.

**Index contents.** Content extracted per file: extended attributes (keywords, copyright), structured metadata (EXIF, creator, modification dates), and exported text content. Text is tokenized with stopwords filtered, optionally stemmed. The index uses inverted lists (dictionary of tokens → postings list of file locations), with frequent tokens separated from rare ones.

**Key lesson for agent memory design:** Spotlight's strength is event-driven incremental indexing. It does not batch-rebuild — every change triggers a targeted update. This is the right model. Its weakness is that it indexes everything and cannot be queried programmatically with SQL-like semantics. For an agent memory system, you want Spotlight's incremental update model but with a SQL-queryable backend.

### 1.6 Tantivy, Meilisearch, Typesense

These are production-grade search engines appropriate for larger-scale deployments.

**Tantivy** — Rust library inspired by Lucene. Not a server — you build the server on top of it. Excellent performance, full control over index structure and scoring, supports BM25 and custom ranking. The right choice if you're building a search service and need maximum flexibility. Not directly embeddable like SQLite.

**Meilisearch** — Ready-to-deploy REST search server (Rust, LMDB storage). Zero-config typo tolerance, search-as-you-type, faceted search. Memory-mapped storage handles up to 80TiB on Linux. Better for larger corpora than Typesense (which requires the full index in RAM). For an agent memory system deployed locally, it's heavyweight relative to SQLite FTS5 unless you already need the REST API for multi-client access.

**Typesense** — C++, full index in RAM, fastest query performance of the three. Single binary deployment. Better configuration flexibility than Meilisearch. The RAM requirement makes it impractical for very large indexes on constrained machines.

**Verdict:** For a local agent memory system, SQLite FTS5 + LanceDB is the right embedded-first stack. Meilisearch or Typesense become relevant only if you're building a shared memory service that multiple agents query over a network.

---

## Problem 2: Maintaining a Memory File System at Scale

### 2.1 How Degradation Happens

A file-based memory system degrades along three axes as it grows:
1. **Retrieval noise** — more files means more potential matches, many irrelevant. Signal-to-noise ratio falls.
2. **Index staleness** — as files are added/modified, indexes that aren't maintained return outdated results.
3. **Conceptual drift** — early files encode concepts differently from late files; without consolidation, related knowledge is scattered across incompatible vocabularies.

The canonical symptom: a query for "Bedrock costs" returns 40 files, 35 of which mention Bedrock in passing. The useful signal is buried.

### 2.2 Garbage Collection Strategies

**TTL-based expiry.** Assign each file a time-to-live based on its type. Immutable facts get infinite TTL. Meeting notes: 180 days. Task scratch pads: 30 days. The mathematical model from FadeMem: `p(t) = 1 - exp(-r · e^(-at))` — recall probability as a function of contextual relevance (r), elapsed time (t), and recall frequency. In practice: implement TTL as a frontmatter field (`expires: 2026-07-12`) and run a daily pruning pass.

**Access-frequency pruning.** Track file read counts (this requires the agent to update a metadata field or a side-car database). Files that have never been accessed after N days and have low semantic centrality (few inbound links) are candidates for archival or deletion. The formal deletion policy from the literature: `φ_period(q_i, e_i, t, t') = 1[freq_t(q_i, e_i) - freq_t'(q_i, e_i) ≤ α]` — prune records whose access frequency has not increased over a time window. This approach yields ~10% performance gains over naive no-GC strategies.

**LLM-judged relevance decay.** Periodically run an LLM pass over low-priority files with the prompt "Is this still relevant? What context does it require? Can it be summarized and merged?" This is expensive but catches things TTL and access frequency cannot — files that are accessed but add little marginal value. Run on a monthly schedule over the bottom quartile by centrality score.

**Archival vs. deletion.** Delete almost nothing — archive instead. A compressed archive directory preserves provenance without polluting the active search corpus. The vector index and FTS5 index simply don't include archived paths.

### 2.3 Index Maintenance

**Incremental update is the only viable model at scale.** Full index rebuilds become prohibitively slow past ~10K files (consider: 10K files × 512-token average × embedding time = minutes on CPU). The correct architecture:

1. File system watcher (FSEvents on macOS, inotify on Linux, or polling for portability) detects changes
2. Change event queued in a durable queue (SQLite table with `processed: bool`)
3. Index worker processes queue: re-chunks changed files, re-embeds, updates vector index, updates FTS5
4. Maintain a `last_indexed` timestamp per file in the side-car database

**Index fragmentation** is a real concern for the FTS5 index specifically. FTS5 accumulates "segments" that must be periodically merged. The correct maintenance schedule: run `INSERT INTO fts_table(fts_table) VALUES('optimize')` periodically (e.g., after every 100 new documents or once daily). This is analogous to VACUUM for the inverted index.

**Vector index consistency** requires more care because approximate nearest-neighbor indexes (HNSW, IVF) can return stale results if not updated transactionally with the underlying files. LanceDB handles this correctly with versioning. With FAISS, you must manage this yourself.

### 2.4 Deduplication

**The problem.** As an agent writes memories over time, it produces near-duplicates: two files about the same conversation from different angles, a summary file and its source, an older belief and an updated version that contradicts it.

**Exact deduplication** — trivial: SHA-256 hash of file content catches byte-for-byte duplicates.

**Syntactic near-deduplication** — MinHash + Locality-Sensitive Hashing (LSH). MinHash compresses documents to compact signatures; LSH groups likely matches. Low compute cost, high throughput. Appropriate for catching files that are textually similar (e.g., two versions of the same meeting notes).

**Semantic deduplication** — Embed all documents, cluster with k-means, compute pairwise cosine similarity within clusters. Documents with cosine similarity above a threshold (typically 0.92-0.95) are candidates for merge. **SemHash** (MinishLab, 2025) uses Model2Vec embeddings for fast multimodal semantic deduplication — appropriate for a memory system where write throughput matters.

The deduplication pipeline:
```
New file written → compute embedding → query vector index for top-K similar →
if max_similarity > threshold → flag for merge review → LLM-judged merge or archive
```

**Merge vs. archive on dedup.** When two files are near-duplicates, the options are: (a) merge into a new file that synthesizes both, delete the originals; (b) keep the more complete/recent file and archive the other; (c) keep both with a `see-also` link. Option (a) is highest quality but requires LLM involvement. Option (b) is safe and automatable. Use (b) as the default; trigger (a) for high-centrality files where quality matters most.

### 2.5 Hierarchy Rebalancing

**The flat-vs-deep tradeoff.** Flat directories (everything in one folder) optimize for global search but make navigation impossible. Deep hierarchies (5+ levels) create long path names and make files hard to discover. Practical sweet spot: 2-3 levels deep with meaningful path prefixes that can serve as category filters.

The Dendron dot-notation approach (`meetings.2026.04.john-monsod.md`) is a compelling middle ground: files remain flat on disk, but the naming convention imposes a logical hierarchy that can be navigated by prefix matching. All files sit in one directory, so search is global; but the structure is navigable without a directory tree.

**When to rebalance.** A directory with > 500 files and no subcategory structure is a signal to split. A hierarchy where every path is > 4 segments deep is a signal to flatten. Automate the detection; require human (or LLM) judgment for the actual restructuring.

**Directory size monitoring.** Add a weekly task: count files per directory. If any directory exceeds the threshold, surface it as a maintenance item. Don't auto-rebalance — the naming convention is a semantic decision, not a mechanical one.

### 2.6 The Write Amplification Problem

Every new memory potentially requires updating: the FTS5 index, the vector index, the link adjacency table, frontmatter-based metadata indexes, and any per-directory summary files. This is write amplification at the application layer (distinct from the storage-layer write amplification of LSM trees and SSDs, though the concept is the same).

**Mitigation strategies:**

1. **Batch writes.** Don't update all indexes synchronously on every file write. Queue writes, batch-process indexes every N seconds or after M writes. The cost is slightly stale indexes; the benefit is dramatically reduced I/O.

2. **Lazy indexing.** Maintain a "dirty files" set. Indexes are only updated when a dirty file is queried. This trades query latency for write throughput.

3. **Separate write and read indexes.** New files go to a "hot" FTS5 table and a small vector index. Periodically merge the hot indexes into the main index. This is the LSM-tree pattern applied to search indexes — it reduces write amplification at the cost of slightly more complex read queries (must search both hot and cold indexes and merge results).

4. **Accept staleness for low-priority updates.** The link adjacency table doesn't need to be updated on every write — batch-update it nightly. Vector indexes can tolerate being 15 minutes stale. FTS5 can be updated synchronously because it's fast.

---

## Problem 3: Graph Structure in a File System

### 3.1 Representing Edges

Three approaches, each with different tradeoffs:

**Symlinks.** `ln -s target.md source_link.md` creates OS-level edges. Navigable with standard tools, respected by many editors. Fundamental problem: symlinks are unidirectional and don't carry semantic metadata (what kind of relationship is this?). Also fragile — rename the target and the symlink breaks. Git does not follow symlinks for content indexing. **Verdict: not appropriate for a memory graph.**

**Inline Markdown links.** `[[target-file]]` or `[link text](path/to/target.md)`. This is what Obsidian, Dendron, Logseq, and Foam all use. The source of truth lives in the file itself — it's portable, readable, and editable by both humans and LLMs. The wikilink format (`[[target]]`) is more concise; the standard Markdown format (`[text](path)`) is more portable across renderers.

The key insight from Obsidian's implementation: **wikilinks are the source of truth; the MetadataCache is the query layer.** Obsidian maintains a `resolvedLinks` map (source path → {target path: occurrence count}) and an `unresolvedLinks` map (source path → {link text: occurrence count}) in memory. The cache rebuilds incrementally on file change — the `'changed'` event triggers re-parse of the affected file, `'resolved'` fires when the full link graph is consistent. This architecture directly maps to: SQLite adjacency table as the `resolvedLinks` equivalent, file system watcher as the FSEvents equivalent.

**Separate adjacency list.** A dedicated file (or SQLite table) stores all edges explicitly: `(source_path, target_path, relationship_type, created_at)`. This enables fast graph queries without scanning all files, and supports edge metadata (relationship type, weight, timestamp) that inline links cannot carry. The downside: the adjacency list is a separate truth that can diverge from the inline links. **Verdict: use as the query layer, not the source of truth.** Inline links are the source of truth; the adjacency table is rebuilt from them on change.

**Hybrid (recommended).** Inline Markdown links as source of truth + SQLite adjacency table as query index. File system watcher detects changes → re-parse changed files for links → update adjacency table incrementally. This is exactly what md-graph implements (SQLite backend, regex-based link parsing, configurable traversal depth).

### 3.2 Traversal Efficiency

**The N-hop problem.** "Find all files related to X within 2 hops" requires graph traversal. On a plain file system with no index, this requires reading every linked file and parsing its links — O(E^N) in the worst case. With a SQLite adjacency table, it's a recursive CTE query:

```sql
WITH RECURSIVE neighbors(path, depth) AS (
  SELECT target_path, 1 FROM edges WHERE source_path = 'X.md'
  UNION ALL
  SELECT e.target_path, n.depth + 1
  FROM edges e JOIN neighbors n ON e.source_path = n.path
  WHERE n.depth < 2
)
SELECT DISTINCT path FROM neighbors;
```

This runs in milliseconds even on a large graph because SQLite can use indexes on `source_path` and `target_path`. The critical index: `CREATE INDEX idx_edges_source ON edges(source_path)`. Without it, every hop requires a full table scan.

**Depth-first vs. breadth-first.** For relevance-ordered traversal, BFS is more useful — it finds all 1-hop neighbors before going deeper, so you can stop early if you have enough candidates. Implement BFS via the recursive CTE with a `depth` column, sorted by depth ascending.

### 3.3 Bidirectional Links

**The backlinks problem.** If A links to B, does B know about A? In Obsidian, yes — the MetadataCache maintains `resolvedLinks` (outgoing) and implicitly provides backlinks by inverting the map. In a file-based system without an index, B has no knowledge of A unless A is explicitly mentioned in B.

**Implementation options:**

1. **Index-only backlinks.** Store only outgoing links in files; maintain a `reverse_edges` table or inverted `resolvedLinks` for backlinks. Backlinks are computed, not stored. This is Obsidian's approach. Zero file modification required — backlinks are a query result.

2. **Explicit backlink comments.** When A links to B, add a comment block to B's frontmatter: `backlinks: [A.md]`. This makes B self-contained and portable, but requires updating B on every change to A's links — write amplification again. Also creates merge conflicts in collaborative settings.

3. **Bimark-style auto-insertion.** Tools like `bimark` automatically maintain bidirectional links in both files. Convenient but creates a class of LLM-unreadable noise if the LLM doesn't understand the format.

**Recommended:** Index-only backlinks. The adjacency table stores all edges as directed `(source, target)` pairs. Backlinks for a file X are `SELECT source_path FROM edges WHERE target_path = 'X.md'`. No file modification needed. The file content is clean; the index is authoritative for structural queries.

### 3.4 Community Detection / Clustering on a File-Based Graph

**Why this matters.** As a memory graph grows to thousands of nodes, the question "what topics does this corpus cover?" requires clustering. Community detection can automatically surface thematic clusters, identify orphaned nodes (files with no connections), and reveal which files serve as hubs (high centrality).

**Algorithm choices.**

The Louvain algorithm is the standard for community detection on undirected graphs: starts with each node in its own community, greedily merges communities to maximize modularity. Available via Python's `python-louvain` or `networkx.algorithms.community.louvain_communities`. Works on graphs of ~100K nodes with good performance.

For directed graphs (which a link graph technically is), the Walktrap algorithm (random walk-based) performs well. The choice of algorithm matters less than having the infrastructure to run any algorithm.

**Practical implementation:**

```python
import networkx as nx
import sqlite3

# Load adjacency table into networkx
conn = sqlite3.connect('memory.db')
G = nx.DiGraph()
for row in conn.execute('SELECT source_path, target_path FROM edges'):
    G.add_edge(*row)

# Community detection (treat as undirected for Louvain)
G_undirected = G.to_undirected()
communities = nx.community.louvain_communities(G_undirected, resolution=1.0)

# Orphan detection
orphans = [n for n in G.nodes() if G.degree(n) == 0]

# Hub detection (high out-degree = many outgoing links)
hubs = sorted(G.out_degree(), key=lambda x: x[1], reverse=True)[:10]
```

**What to do with community structure.** Communities can be used to:
- Auto-suggest tags or categories for new files
- Identify which community a new memory belongs to (and therefore which existing files to link)
- Surface under-connected communities as candidates for consolidation
- Detect that a community has grown too large and should be subdivided

**The Logseq DB architecture** is worth understanding here: Logseq migrated from file-based storage to a DataScript (Datalog) database in its DB version. The file format remains Markdown but the query layer is a full in-memory graph database with Datalog queries. This is the fullest expression of the "files as source of truth, graph database as query layer" approach.

---

## Synthesis: Recommended Architecture

Given the three problems above, here is the architecture that best balances simplicity, capability, and maintenance cost for an LLM agent memory system:

**Storage layer:** Plain Markdown files, flat or Dendron-style dot-notation naming, one concept per file. Files are the source of truth. LLM writes inline Markdown links (`[[target]]`) for semantic edges.

**Index layer (single SQLite database alongside the files):**
- `files` table: `(path, mtime, content_hash, last_indexed, ttl, access_count)`
- `fts_content` FTS5 virtual table: full-text BM25 search over file content
- `edges` table: `(source_path, target_path, relationship_type, created_at)` — adjacency list, rebuilt from file content
- `embeddings` table: `(chunk_id, file_path, chunk_text, embedding BLOB)` — or use LanceDB as a sidecar

**Index maintenance:** File system watcher (or inotify/FSEvents) → queue → batch processor that runs every 30 seconds. Processor: parse links → update edges table; chunk content → update embeddings; update FTS5.

**Query path:** Hybrid search: FTS5 BM25 + vector similarity search → RRF fusion → return top-K file paths with excerpts.

**GC schedule:**
- Daily: apply TTL-based expiry, archive expired files, run FTS5 `optimize`
- Weekly: run semantic deduplication pass on bottom-quartile files, report directory size
- Monthly: run LLM-judged relevance review on low-access files

**Graph operations:** Recursive CTEs over the `edges` table for N-hop traversal. Nightly community detection run with Louvain; persist community assignments to the `files` table.

---

## Key Findings

1. **SQLite FTS5 + LanceDB is the right embedded stack.** FTS5 handles BM25 keyword search with zero external dependencies; LanceDB handles vector search with near-in-memory performance from disk. Both are single-file, serverless, and transactional.

2. **Hybrid search (BM25 + semantic) beats either alone.** Dense retrieval has ~22% Top-1 recall on exact terms; hybrid reaches 53%+. For agent memory where queries mix natural language with specific identifiers, hybrid is mandatory.

3. **Inline Markdown links as source of truth, SQLite adjacency table as query layer.** This is the architecture Obsidian uses (MetadataCache = adjacency table). It's the right separation: files are portable and LLM-readable; the index is fast and queryable.

4. **Write amplification is real but manageable.** Batch index updates (30-second windows), accept short staleness windows, use incremental rebuilds not full rebuilds. The LSM-tree hot/cold index pattern works well.

5. **Deduplication requires embeddings.** Syntactic near-deduplication (MinHash/LSH) catches textually similar files. Semantic deduplication (embedding + cosine similarity threshold ~0.93) is needed for conceptually equivalent files with different wording. SemHash (2025) is the current best tool for this.

6. **Dendron's dot-notation naming is the most underrated approach.** Structured file names eliminate a whole class of search queries through path-prefix matching. It's the most portable approach — requires no index, no server, no tooling beyond standard file operations.

7. **TTL + access frequency + LLM-judged GC in three tiers.** TTL handles perishable notes automatically. Access frequency catches files that never get retrieved. LLM judgment handles the hard cases where both heuristics fail.

8. **For graph traversal, recursive CTEs over a SQLite adjacency table are fast enough.** 2-hop traversal on 50K nodes completes in milliseconds with proper indexing. No dedicated graph database required.

9. **Backlinks should be index-computed, not file-embedded.** Writing backlinks into file content creates write amplification and merge conflicts. Compute them from the adjacency table on demand.

10. **all-MiniLM-L6-v2 is the right default embedding model for CPU-constrained local search** (~14.7ms/1K tokens). Use nomic-embed-text v1 when document length > 2K tokens or when accuracy matters more than speed.

---

## Details by Tool

### Existing PKM Systems and Their Relevant Patterns

**Obsidian:**
- MetadataCache maintains `resolvedLinks: Record<string, Record<string, number>>` — maps source path → {target path: count}
- Event-driven incremental updates: `'changed'` event → re-parse file → update cache; `'resolved'` fires when full graph is consistent
- Backlinks are computed by inverting `resolvedLinks` — no file modification required
- Unlinked mentions tracked in `unresolvedLinks` separately (link text that doesn't resolve to a file)
- Key lesson: the cache is not persisted to disk in Obsidian — it's rebuilt in memory on vault open. This works for <100K files but would be too slow for larger corpora. For a persistent agent memory, the SQLite adjacency table is the right analog.

**Logseq:**
- Block-level (not file-level) linking via UUIDs
- Files parsed into AST via mldoc (OCaml parser), then converted to DataScript entities
- The DB version uses DataScript (Datalog) for all queries — richer query expressivity than SQL for graph patterns
- Key lesson: block-level granularity enables more precise linking but increases index complexity dramatically. For agent memory, file-level granularity is the right default.

**Dendron:**
- Dot-notation hierarchy: `area.project.subtopic.md` — all files flat in one directory
- Schemas define allowed hierarchies and can enforce naming conventions
- Path-prefix lookup is O(log n) on a sorted file list — no index required for navigation
- Key lesson: naming convention as a free index. Design the file naming scheme carefully and you eliminate a large class of search problems.

**foam (VS Code extension):**
- Wikilinks parsed via regex on file open/change
- Graph built in memory from parsed links
- `--tag-direction In|Out|Both` parameter in the underlying md-graph library confirms that directional edge storage is the right model
- Persistent graph via SQLite backend in md-graph (the underlying library)

**md-graph (CLI tool):**
- SQLite backend stores parsed link relationships
- Supports depth-configurable subgraph traversal
- Distinguishes resolved links, unresolved links, and static resources
- Direct implementation model for a standalone agent memory graph tool

---

## Sources

- [sentence-transformers/all-MiniLM-L6-v2 — Hugging Face](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
- [Best Open-Source Embedding Models Benchmarked and Ranked — Supermemory](https://supermemory.ai/blog/best-open-source-embedding-models-benchmarked-and-ranked/)
- [Comparing Local Embedding Models for RAG Systems (All-MiniLM, Nomic, and OpenAI)](https://medium.com/@jinmochong/comparing-local-embedding-models-for-rag-systems-all-minilm-nomic-and-openai-ee425b507263)
- [Nomic Embed: Training a Reproducible Long Context Text Embedder](https://arxiv.org/html/2402.01613v2)
- [Hybrid Search: Combining BM25 and Semantic Search — LanceDB/LangChain](https://medium.com/etoai/hybrid-search-combining-bm25-and-semantic-search-for-better-results-with-lan-1358038fe7e6)
- [Full-text search for RAG apps: BM25 & hybrid search — Redis](https://redis.io/blog/full-text-search-for-rag-the-precision-layer/)
- [BMX: Entropy-weighted Similarity and Semantic-enhanced Hybrid Retrieval](https://arxiv.org/pdf/2408.06643)
- [SQLite FTS5 Extension — SQLite.org](https://www.sqlite.org/fts5.html)
- [Spotlight on search: How Spotlight works — The Eclectic Light Company](https://eclecticlight.co/2021/01/28/spotlight-on-search-how-spotlight-works/)
- [A deeper dive into Spotlight indexes — The Eclectic Light Company](https://eclecticlight.co/2025/07/30/a-deeper-dive-into-spotlight-indexes/)
- [How Does Spotlight Work? — Apple Developer Archive](https://developer.apple.com/library/archive/documentation/Carbon/Conceptual/MetadataIntro/Concepts/HowDoesItWork.html)
- [Meilisearch Comparison to Alternatives](https://www.meilisearch.com/docs/learn/what_is_meilisearch/comparison_to_alternatives)
- [Tantivy vs Meilisearch vs Typesense — Hacker News discussion](https://news.ycombinator.com/item?id=22185063)
- [FAISS vs LanceDB — Zilliz comparison](https://zilliz.com/comparison/faiss-vs-lancedb)
- [ChromaDB vs FAISS — Medium](https://mohamedbakrey094.medium.com/chromadb-vs-faiss-a-comprehensive-guide-for-vector-search-and-ai-applications-39762ed1326f)
- [Comparing File Systems and Databases for AI Agent Memory — Oracle Developers Blog](https://blogs.oracle.com/developers/comparing-file-systems-and-databases-for-effective-ai-agent-memory-management)
- [A-MEM: Agentic Memory for LLM Agents — arXiv 2502.12110](https://arxiv.org/abs/2502.12110)
- [Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory — arXiv 2504.19413](https://arxiv.org/abs/2504.19413)
- [MemGPT: Towards LLMs as Operating Systems — arXiv 2310.08560](https://arxiv.org/pdf/2310.08560)
- [Memory in LLM-based Multi-agent Systems — TechRxiv](https://www.techrxiv.org/users/1007269/articles/1367390/master/file/data/LLM_MAS_Memory_Survey_preprint_/LLM_MAS_Memory_Survey_preprint_.pdf?inline=true)
- [A Survey on the Memory Mechanism of LLM-based Agents — ACM TOIS](https://dl.acm.org/doi/10.1145/3748302)
- [GitHub: MinishLab/semhash — Fast Semantic Deduplication](https://github.com/MinishLab/semhash)
- [Large-scale Near-deduplication Behind BigCode — Hugging Face](https://huggingface.co/blog/dedup)
- [MinHash LSH in Milvus — Milvus Blog](https://milvus.io/blog/minhash-lsh-in-milvus-the-secret-weapon-for-fighting-duplicates-in-llm-training-data.md)
- [MetadataCache and Link Resolution — Obsidian API DeepWiki](https://deepwiki.com/obsidianmd/obsidian-api/2.4-metadatacache-and-link-resolution)
- [Obsidian Developer Docs: MetadataCache](https://docs.obsidian.md/Reference/TypeScript+API/MetadataCache)
- [Dendron Hierarchies](https://wiki.dendron.so/notes/f3a41725-c5e5-4851-a6ed-5f541054d409/)
- [Logseq Graph Management — DeepWiki](https://deepwiki.com/logseq/logseq/4.3-repository-and-graph-management)
- [GitHub: foambubble/foam](https://github.com/foambubble/foam)
- [GitHub: CharlesSchimmel/md-graph](https://github.com/CharlesSchimmel/md-graph)
- [GitHub: DiscreteTom/bimark — Auto bidirectional links](https://github.com/DiscreteTom/bimark)
- [Louvain Community Detection — NetworkX docs](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.community.louvain.louvain_communities.html)
- [Graph Algorithms for Community Detection — Neo4j](https://neo4j.com/blog/graph-data-science/graph-algorithms-community-detection-recommendations/)
- [GitHub: phiresky/ripgrep-all](https://github.com/phiresky/ripgrep-all)
- [Deep Dive on Read/Write/Space Amplification in LSM Storage Engines — Medium](https://gifted-dl.medium.com/deep-dive-on-read-write-and-space-amplification-in-ssds-and-lsm-storage-engines-and-what-makes-4a1e15fc6f0e)
- [How to handle incremental updates in a vector database — Milvus](https://milvus.io/ai-quick-reference/how-do-you-handle-incremental-updates-in-a-vector-database)

---

## Cross-References

- [research/filesystem-as-graph-db.md](filesystem-as-graph-db.md) — Karpathy/Obsidian vault as graph DB (52K files real-world example); PARA taxonomy applied to file hierarchies
- [synthesis/llm-memory-organization.md](../synthesis/llm-memory-organization.md) — Three-tier memory architecture (CLAUDE.md / MEMORY.md / topic files)

---

## Open Questions

1. **Chunking strategy for long files.** What is the right chunk size and overlap for embedding-based search over meeting transcripts and system docs? The standard 256-512 token window may lose context for reasoning-heavy queries.

2. **When does the overhead of a vector index become worth it?** At what corpus size does all-MiniLM-L6-v2 + FAISS beat SQLite FTS5 BM25 for agent memory queries? The crossover point is somewhere around 10K-50K files but hasn't been measured for this specific use case.

3. **How do you handle concept drift in a long-lived memory system?** If the agent's vocabulary for a concept changes over 12 months, how do you ensure old files are still retrievable? Periodic re-embedding is expensive; some form of vocabulary anchoring or term normalization may be needed.

4. **Collaborative memory across multiple agents.** This research focuses on single-agent memory. For multi-agent systems where multiple agents write to the same memory store, file-level locking and merge conflict resolution become serious problems. The Logseq DB architecture (DataScript transactions) is likely the right substrate for that use case, not plain files.

5. **Graph community detection in practice.** The Louvain algorithm is easy to run but the output (community assignments) is hard to act on automatically. What is the right LLM-in-the-loop workflow for turning community detection output into actionable file reorganization decisions?
