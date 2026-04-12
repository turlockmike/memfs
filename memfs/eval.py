"""LongMemEval benchmark harness — ingest sessions, compute Recall@k / MRR."""

import json
import os
from datetime import datetime, timezone

from memfs.db import create_db, connect
from memfs.indexer import index_directory
from memfs.search import grep


def ingest_benchmark(benchmark_path: str, eval_root: str) -> int:
    """Ingest LongMemEval benchmark sessions as markdown files.

    Each unique session becomes a file at <eval_root>/sessions/<session_id>.md.
    Returns the number of unique sessions written.
    """
    with open(benchmark_path) as f:
        data = json.load(f)

    sessions_dir = os.path.join(eval_root, "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    # Collect all unique sessions across all questions
    seen_sessions = set()
    session_data = {}  # session_id -> (date, turns)

    for entry in data:
        for i, session_id in enumerate(entry["haystack_session_ids"]):
            if session_id in seen_sessions:
                continue
            seen_sessions.add(session_id)
            date = entry["haystack_dates"][i] if i < len(entry["haystack_dates"]) else ""
            turns = entry["haystack_sessions"][i] if i < len(entry["haystack_sessions"]) else []
            session_data[session_id] = (date, turns)

    # Write session files
    count = 0
    for session_id, (date, turns) in session_data.items():
        filepath = os.path.join(sessions_dir, f"{session_id}.md")
        if os.path.exists(filepath):
            continue  # Dedup — don't overwrite

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

    # Initialize memfs index
    db_path = os.path.join(eval_root, ".mem", "memory.db")
    create_db(db_path)
    conn = connect(db_path)
    index_directory(conn, eval_root)
    conn.close()

    return count


def compute_recall(db_path: str, benchmark_data: list[dict], k: int = 5) -> dict:
    """Compute Recall@k, MRR, and Precision@k for the benchmark.

    For each question, runs `memfs grep` with the question text and checks
    whether any of the answer_session_ids appear in the top-k results.
    """
    conn = connect(db_path)
    per_question = []
    total_hits = 0
    total_reciprocal_rank = 0.0
    total_precision_hits = 0

    for entry in benchmark_data:
        question = entry["question"]
        answer_ids = set(entry.get("answer_session_ids", []))

        # Search
        results = grep(conn, question, limit=k)
        result_paths = [r["path"] for r in results]

        # Check if any answer session is in results
        hit = False
        first_rank = None
        precision_hits = 0

        for i, path in enumerate(result_paths):
            # Extract session_id from path: "sessions/session_a.md" -> "session_a"
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
    conn.close()

    return {
        "recall_at_k": total_hits / n if n > 0 else 0.0,
        "mrr": total_reciprocal_rank / n if n > 0 else 0.0,
        "precision_at_k": total_precision_hits / (n * k) if n > 0 else 0.0,
        "k": k,
        "total_questions": n,
        "total_hits": total_hits,
        "per_question": per_question,
    }
