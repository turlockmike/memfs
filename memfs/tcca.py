"""TCCA (Token Cost to Correct Answer) instrumentation for memfs eval.

Three adapters:
  - "memfs"       — memfs grep + read matched files (zero retrieval_tokens,
                    since FTS is not an LLM call)
  - "bm25"        — pure Neo4j fulltext, no reranker; zero retrieval_tokens
  - "accumulate"  — load every file into context; baseline_tokens

TCCA formula:
    tcca = baseline_tokens / max(total_tokens, 1)  when answer_correct
    tcca = 0                                       when wrong

(Higher is better — more answer per token.)

Token counting: we use tiktoken if available, otherwise a char-based
approximation (1 token ≈ 4 chars). The approximation is fine for relative
comparisons across adapters; only absolute numbers depend on accuracy.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Callable

from memfs.graph import connect
from memfs.search import grep
from memfs.eval import _call_llm, score_hypothesis


# -------- token counting --------

def _count_tokens(text: str) -> int:
    """Count tokens. Uses tiktoken.cl100k_base if available; falls back to
    chars/4."""
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        return max(1, len(text) // 4)


# -------- adapters --------

def _read_file(mem_home: str, rel_path: str) -> str:
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.exists(abs_path):
        return ""
    with open(abs_path, encoding="utf-8") as f:
        return f.read()


def adapter_memfs(mem_home: str, entry: dict, k: int = 5) -> dict:
    """memfs grep → top-k file contents as context. Zero retrieval tokens
    (FTS, not LLM)."""
    graph = connect()
    try:
        results = grep(graph, entry["question"], limit=k)
    finally:
        graph.close()

    contexts = []
    for r in results:
        body = _read_file(mem_home, r["path"])
        if body:
            contexts.append(f"--- {r['path']} ---\n{body}")

    context_text = "\n\n".join(contexts) if contexts else "(No history found)"
    return {
        "adapter": "memfs",
        "context": context_text,
        "retrieval_tokens": 0,
        "retrieved_paths": [r["path"] for r in results],
    }


def adapter_bm25(mem_home: str, entry: dict, k: int = 5) -> dict:
    """Pure Neo4j fulltext, no rerank / no neighborhood / no search edges.

    Identical to memfs in M1 practice (FTS is the baseline) but bypasses
    grep-side enrichment to isolate 'pure BM25' behavior for reference.
    """
    from memfs import graph as graph_mod
    graph = connect()
    try:
        # strip-and-search like grep
        query = entry["question"]
        from memfs.search import _escape_lucene
        lucene = _escape_lucene(query)
        if not lucene:
            rows = []
        else:
            rows = graph_mod.fulltext_search(graph, lucene, limit=k)
    finally:
        graph.close()

    contexts = []
    paths = []
    for row in rows:
        body = _read_file(mem_home, row["path"])
        if body:
            contexts.append(f"--- {row['path']} ---\n{body}")
        paths.append(row["path"])

    context_text = "\n\n".join(contexts) if contexts else "(No history found)"
    return {
        "adapter": "bm25",
        "context": context_text,
        "retrieval_tokens": 0,
        "retrieved_paths": paths,
    }


def adapter_accumulate(mem_home: str, entry: dict, k: int = 5) -> dict:
    """Load every indexed file into context. Establishes baseline_tokens."""
    graph = connect()
    try:
        rows = graph.run(
            "MATCH (n:Node) RETURN n.path AS path ORDER BY n.path"
        )
        all_paths = [r["path"] for r in rows]
    finally:
        graph.close()

    contexts = []
    for p in all_paths:
        body = _read_file(mem_home, p)
        if body:
            contexts.append(f"--- {p} ---\n{body}")

    context_text = "\n\n".join(contexts) if contexts else "(Empty corpus)"
    return {
        "adapter": "accumulate",
        "context": context_text,
        "retrieval_tokens": 0,
        "retrieved_paths": all_paths,
    }


ADAPTERS: dict[str, Callable[[str, dict, int], dict]] = {
    "memfs": adapter_memfs,
    "bm25": adapter_bm25,
    "accumulate": adapter_accumulate,
}


# -------- TCCA runner --------

def _compute_baseline_tokens(mem_home: str) -> int:
    """Total tokens if the agent loaded every file in the corpus.

    This is the denominator for the TCCA numerator (baseline_tokens).
    """
    graph = connect()
    try:
        rows = graph.run(
            "MATCH (n:Node) RETURN n.path AS path, n.content AS content"
        )
        paths = [(r["path"], r.get("content") or "") for r in rows]
    finally:
        graph.close()

    total = 0
    for path, content in paths:
        if content:
            total += _count_tokens(content)
        else:
            # fallback: read from disk
            body = _read_file(mem_home, path)
            total += _count_tokens(body)
    return total


def run_tcca(
    mem_home: str,
    entry: dict,
    adapter_name: str,
    baseline_tokens: int,
    *,
    model: str = "sonnet",
    backend: str = "claude",
    llm: Callable[[str], str] | None = None,
    judge: Callable[..., bool] | None = None,
) -> dict:
    """Run a single TCCA-instrumented query.

    `llm` and `judge` are injectable for testing without hitting a real LLM.
    """
    question = entry["question"]
    question_date = entry.get("question_date", "")
    ground_truth = entry.get("answer", "")

    adapter = ADAPTERS[adapter_name]
    ctx = adapter(mem_home, entry)
    context_text = ctx["context"]

    prompt = (
        "I will give you several past chat sessions between you and a user.\n"
        "Please answer the question based on the relevant chat history.\n"
        'If the information is not in the provided history, say "I don\'t have that information."\n\n'
        f"Past Chat Sessions:\n{context_text}\n\n"
        f"Current Date: {question_date}\n"
        f"Question: {question}\n"
        "Answer:"
    )
    context_tokens = _count_tokens(context_text)
    prompt_tokens = _count_tokens(prompt)

    if llm is None:
        hypothesis = _call_llm(prompt, model=model, backend=backend)
    else:
        hypothesis = llm(prompt)
    generation_tokens = _count_tokens(hypothesis)

    if judge is None:
        correct = score_hypothesis(
            question=question, answer=ground_truth, hypothesis=hypothesis,
            question_type=entry.get("question_type", ""),
            model=model, backend=backend,
        )
    else:
        correct = judge(question=question, answer=ground_truth,
                        hypothesis=hypothesis,
                        question_type=entry.get("question_type", ""))

    retrieval_tokens = int(ctx.get("retrieval_tokens", 0))
    total_tokens = retrieval_tokens + prompt_tokens + generation_tokens

    tcca = (baseline_tokens / max(total_tokens, 1)) if correct else 0.0

    return {
        "question_id": entry.get("question_id"),
        "question_type": entry.get("question_type", ""),
        "adapter": adapter_name,
        "retrieval_tokens": retrieval_tokens,
        "context_tokens": context_tokens,
        "prompt_tokens": prompt_tokens,
        "generation_tokens": generation_tokens,
        "total_tokens": total_tokens,
        "baseline_tokens": baseline_tokens,
        "answer_correct": bool(correct),
        "tcca": round(tcca, 4),
        "hypothesis": hypothesis,
        "retrieved_paths": ctx.get("retrieved_paths", []),
    }


def cmd_tcca(args):
    """CLI entry: mem-eval tcca <benchmark> --root X --adapter {memfs,bm25,accumulate}"""
    from memfs.eval import ingest_benchmark

    print(f"Ingesting {args.benchmark}...", file=sys.stderr)
    count = ingest_benchmark(args.benchmark, args.root)
    print(f"Ingested {count} sessions", file=sys.stderr)

    with open(args.benchmark) as f:
        data = json.load(f)

    if args.limit:
        data = data[: args.limit]

    baseline = _compute_baseline_tokens(args.root)
    print(f"Baseline tokens (accumulate): {baseline}", file=sys.stderr)

    by_type = defaultdict(list)
    with open(args.output, "w") as out_f:
        for i, entry in enumerate(data):
            print(f"[{i+1}/{len(data)}] {entry['question_id']}", file=sys.stderr)
            try:
                result = run_tcca(
                    args.root, entry, adapter_name=args.adapter,
                    baseline_tokens=baseline,
                    model=args.model, backend=args.backend,
                )
            except Exception as e:
                result = {
                    "question_id": entry["question_id"],
                    "adapter": args.adapter,
                    "error": str(e),
                    "tcca": 0.0,
                    "answer_correct": False,
                }
            out_f.write(json.dumps(result) + "\n")
            out_f.flush()
            by_type[result.get("question_type", "unknown")].append(result)

    # Summary
    for qt, results in sorted(by_type.items()):
        correct = sum(1 for r in results if r.get("answer_correct"))
        avg_tcca = sum(r.get("tcca", 0) for r in results) / max(len(results), 1)
        print(
            json.dumps({
                "summary": {
                    "question_type": qt,
                    "n": len(results),
                    "correct": correct,
                    "accuracy": round(correct / max(len(results), 1), 4),
                    "avg_tcca": round(avg_tcca, 4),
                }
            })
        )
