---
source: examples/decision-log
kind: canonical
summary: "Example KB entry: why this project uses local ONNX embeddings over an API embedder — the decision, its reasons, and its revisit triggers."
ingested_at: 2026-08-10
---

# Decision: local ONNX embeddings over an API embedder

**This is an example entry** showing the doc half of a KB pair. Its content is
a fictional-but-realistic engineering decision — the class of knowledge an
external memory exists for, because no model's weights can ever contain *your*
project's decisions.

## The decision

Semantic search uses a local ONNX embedding model (via `fastembed`) rather
than a hosted embedding API.

## Reasons (ranked)

1. **Ingest-time coupling.** Every KB write triggers a re-embed. A hosted API
   makes the write path depend on network + billing state; a local model makes
   it depend only on disk. Write paths should have the fewest failure modes.
2. **Cost shape.** Embedding cost scales with corpus churn, not corpus size.
   Local inference makes re-embedding free, which changes maintenance
   behavior: sweeps re-embed liberally instead of rationing calls.
3. **Privacy floor.** KB content never leaves the machine as a side effect of
   indexing.

## Revisit triggers (what would change this decision)

- Corpus outgrows what local inference re-embeds in under a minute.
- Retrieval quality measurably lags a hosted embedder on this corpus's own
  eval set (measure with a frozen-question A/B, not vibes).

## The principle

When a component sits on the WRITE path of a memory system, prefer the
dependency with the fewest external failure modes — the write path is the one
place where an outage silently becomes permanent knowledge loss.
