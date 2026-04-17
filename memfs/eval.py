"""LongMemEval benchmark harness — ingest sessions, compute Recall@k / MRR / QA accuracy.

Neo4j-backed. `mem_home` replaces `db_path` at the API boundary since Neo4j
is server-based (not per-directory).
"""

import json
import os
import subprocess

from memfs.graph import create_db, connect, clear_data
from memfs.indexer import index_directory
from memfs.search import grep


def ingest_benchmark(benchmark_path: str, eval_root: str, *, fresh: bool = True) -> int:
    """Ingest LongMemEval benchmark sessions as markdown files.

    Each unique session becomes a file at <eval_root>/sessions/<session_id>.md.
    Returns the number of unique sessions written.

    If `fresh`, wipes node data in the graph before reindexing.
    """
    with open(benchmark_path) as f:
        data = json.load(f)

    sessions_dir = os.path.join(eval_root, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    seen_sessions = set()
    session_data = {}

    for entry in data:
        for i, session_id in enumerate(entry["haystack_session_ids"]):
            if session_id in seen_sessions:
                continue
            seen_sessions.add(session_id)
            date = entry["haystack_dates"][i] if i < len(entry["haystack_dates"]) else ""
            turns = entry["haystack_sessions"][i] if i < len(entry["haystack_sessions"]) else []
            session_data[session_id] = (date, turns)

    count = 0
    for session_id, (date, turns) in session_data.items():
        filepath = os.path.join(sessions_dir, f"{session_id}.md")
        if os.path.exists(filepath):
            continue

        lines = [
            "---",
            f"session_id: {session_id}",
            f'date: "{date}"',
            "---",
            "",
        ]
        for turn in turns:
            role = turn["role"]
            content = turn["content"]
            prefix = "**User:**" if role == "user" else "**Assistant:**"
            lines.append(f"{prefix} {content}")
            lines.append("")

        with open(filepath, "w") as f:
            f.write("\n".join(lines))
        count += 1

    # Initialize graph + index
    os.makedirs(os.path.join(eval_root, ".mem"), exist_ok=True)
    create_db()
    graph = connect()
    try:
        if fresh:
            clear_data(graph)
        index_directory(graph, eval_root)
    finally:
        graph.close()

    return count


def compute_recall(mem_home: str, benchmark_data: list[dict], k: int = 5,
                   use_vectors: bool = False) -> dict:
    """Compute Recall@k, MRR, Precision@k for the benchmark.

    `mem_home` is the root that was ingested (has a .mem dir).
    """
    graph = connect()
    try:
        per_question = []
        total_hits = 0
        total_reciprocal_rank = 0.0
        total_precision_hits = 0

        for entry in benchmark_data:
            question = entry["question"]
            answer_ids = set(entry.get("answer_session_ids", []))

            results = grep(graph, question, limit=k)
            result_paths = [r["path"] for r in results]

            hit = False
            first_rank = None
            precision_hits = 0

            for i, path in enumerate(result_paths):
                basename = os.path.splitext(os.path.basename(path))[0]
                if basename in answer_ids:
                    if not hit:
                        hit = True
                        first_rank = i + 1
                    precision_hits += 1

            per_question.append({
                "question_id": entry["question_id"],
                "question_type": entry.get("question_type", ""),
                "hit": hit,
                "first_rank": first_rank,
                "result_paths": result_paths[:k],
                "answer_session_ids": list(answer_ids),
            })

            if hit:
                total_hits += 1
                total_reciprocal_rank += 1.0 / first_rank
            total_precision_hits += precision_hits

        n = len(benchmark_data)
        return {
            "recall_at_k": total_hits / n if n > 0 else 0.0,
            "mrr": total_reciprocal_rank / n if n > 0 else 0.0,
            "precision_at_k": total_precision_hits / (n * k) if n > 0 else 0.0,
            "k": k,
            "total_questions": n,
            "total_hits": total_hits,
            "per_question": per_question,
        }
    finally:
        graph.close()


# --- QA Evaluation ---

def _call_llm(prompt: str, model: str = "sonnet", backend: str = "claude") -> str:
    """Call an LLM. Supports 'claude' (CLI) and 'ollama' backends."""
    if backend == "ollama":
        import urllib.request
        import json as json_mod
        url = "http://192.168.4.30:11434/api/generate"
        payload = json_mod.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
        }).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json_mod.loads(resp.read())
        return data.get("response", "").strip()
    else:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "text"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"LLM call failed: {result.stderr}")
        return result.stdout.strip()


def generate_hypothesis(
    mem_home: str,
    entry: dict,
    k: int = 5,
    use_vectors: bool = False,
    model: str = "sonnet",
    backend: str = "claude",
) -> dict:
    """Generate an answer hypothesis for a benchmark question."""
    graph = connect()
    try:
        question = entry["question"]
        question_date = entry.get("question_date", "")

        results = grep(graph, question, limit=k)

        contexts = []
        for r in results:
            filepath = os.path.join(mem_home, r["path"])
            if os.path.exists(filepath):
                with open(filepath, encoding="utf-8") as f:
                    contexts.append(f"--- {r['path']} ---\n{f.read()}")
    finally:
        graph.close()

    context_text = "\n\n".join(contexts) if contexts else "(No relevant history found)"

    prompt = f"""I will give you several past chat sessions between you and a user.
Please answer the question based on the relevant chat history.
If the information is not in the provided history, say "I don't have that information."

Past Chat Sessions:
{context_text}

Current Date: {question_date}
Question: {question}
Answer:"""

    hypothesis = _call_llm(prompt, model=model, backend=backend)

    return {
        "question_id": entry["question_id"],
        "hypothesis": hypothesis,
        "retrieved_paths": [r["path"] for r in results],
    }


def score_hypothesis(
    question: str,
    answer: str,
    hypothesis: str,
    question_type: str,
    model: str = "sonnet",
    backend: str = "claude",
) -> bool:
    """Score a hypothesis against ground truth via LLM-as-judge."""
    if question_type == "temporal-reasoning":
        prompt = f"""Question: {question}
Ground truth answer: {answer}
Model response: {hypothesis}

Does the model response contain the correct answer? For date/time questions, accept off-by-one errors. Answer yes or no only."""
    elif question_type == "knowledge-update":
        prompt = f"""Question: {question}
Ground truth answer (the latest/most recent answer): {answer}
Model response: {hypothesis}

Does the model response contain the correct, most up-to-date answer? It's acceptable if the response also mentions older information as long as the latest answer is present. Answer yes or no only."""
    elif question_type == "single-session-preference":
        prompt = f"""Question: {question}
Ground truth answer: {answer}
Model response: {hypothesis}

Does the model response correctly reflect the user's preference or personalization request? The response doesn't need to be identical, just correctly personalized. Answer yes or no only."""
    else:
        prompt = f"""Question: {question}
Ground truth answer: {answer}
Model response: {hypothesis}

Does the model response contain the correct answer or an equivalent response? Answer yes or no only."""

    response = _call_llm(prompt, model=model, backend=backend)
    return response.strip().lower().startswith("yes")
