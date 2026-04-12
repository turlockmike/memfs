"""Tests for the LongMemEval benchmark harness."""

import json
import os
import pytest

from memfs.db import create_db, connect
from memfs.eval import ingest_benchmark, compute_recall


@pytest.fixture
def sample_benchmark(tmp_path):
    """Create a minimal benchmark JSON for testing."""
    data = [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "question": "What was the first issue with my car?",
            "answer": "GPS not working",
            "question_date": "2023/04/10 (Mon) 23:07",
            "haystack_dates": ["2023/04/10 (Mon) 17:50", "2023/04/10 (Mon) 14:47"],
            "haystack_session_ids": ["session_a", "session_b"],
            "answer_session_ids": ["session_a"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "My car GPS is not working after the service.", "has_answer": True},
                    {"role": "assistant", "content": "I'm sorry to hear that. Have you tried resetting it?"},
                ],
                [
                    {"role": "user", "content": "What's a good restaurant nearby?"},
                    {"role": "assistant", "content": "I'd recommend checking Yelp for local options."},
                ],
            ],
        },
        {
            "question_id": "q2",
            "question_type": "multi-session",
            "question": "How many miles per gallon does my car get?",
            "answer": "32 miles per gallon",
            "question_date": "2023/05/01 (Mon) 10:00",
            "haystack_dates": ["2023/04/20 (Thu) 09:00"],
            "haystack_session_ids": ["session_c"],
            "answer_session_ids": ["session_c"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "I've been getting around 32 miles per gallon.", "has_answer": True},
                    {"role": "assistant", "content": "That's great fuel efficiency!"},
                ],
            ],
        },
    ]
    benchmark_file = tmp_path / "benchmark.json"
    benchmark_file.write_text(json.dumps(data))
    return str(benchmark_file)


@pytest.fixture
def eval_root(tmp_path):
    """Create a separate temp dir for eval ingestion."""
    root = tmp_path / "eval_root"
    root.mkdir()
    return root


class TestIngest:
    def test_creates_session_files(self, sample_benchmark, eval_root):
        count = ingest_benchmark(sample_benchmark, str(eval_root))
        # 3 unique sessions across both questions
        assert count == 3
        assert (eval_root / "sessions" / "session_a.md").exists()
        assert (eval_root / "sessions" / "session_b.md").exists()
        assert (eval_root / "sessions" / "session_c.md").exists()

    def test_session_has_frontmatter(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        content = (eval_root / "sessions" / "session_a.md").read_text()
        assert "session_id: session_a" in content
        assert "date:" in content

    def test_session_has_conversation_content(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        content = (eval_root / "sessions" / "session_a.md").read_text()
        assert "GPS" in content

    def test_deduplicates_sessions(self, sample_benchmark, eval_root):
        # Ingest twice — should not create duplicates
        ingest_benchmark(sample_benchmark, str(eval_root))
        count2 = ingest_benchmark(sample_benchmark, str(eval_root))
        files = list((eval_root / "sessions").iterdir())
        assert len(files) == 3

    def test_initializes_memfs_index(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        assert (eval_root / ".mem" / "memory.db").exists()
        conn = connect(str(eval_root / ".mem" / "memory.db"))
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert count == 3


class TestRecall:
    def test_recall_at_k(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        benchmark_data = json.loads(open(sample_benchmark).read())

        results = compute_recall(db_path, benchmark_data, k=5)
        assert "recall_at_k" in results
        assert "mrr" in results
        assert "precision_at_k" in results
        assert "per_question" in results
        assert 0.0 <= results["recall_at_k"] <= 1.0
        assert len(results["per_question"]) == 2

    def test_recall_returns_per_question_details(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        benchmark_data = json.loads(open(sample_benchmark).read())

        results = compute_recall(db_path, benchmark_data, k=5)
        # Each question should have per-question details
        for pq in results["per_question"]:
            assert "question_id" in pq
            assert "hit" in pq
            assert "answer_session_ids" in pq

    def test_recall_with_keyword_matching_query(self, sample_benchmark, eval_root):
        """FTS5 finds sessions when query terms match content directly."""
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        # Direct keyword match — "miles per gallon" appears verbatim in session_c
        test_data = [{
            "question_id": "direct_match",
            "question_type": "single-session-user",
            "question": "miles per gallon",
            "answer": "32",
            "question_date": "2023/05/01",
            "answer_session_ids": ["session_c"],
            "haystack_session_ids": ["session_c"],
            "haystack_sessions": [],
        }]
        results = compute_recall(db_path, test_data, k=5)
        assert results["per_question"][0]["hit"] is True


class TestMrr:
    def test_mrr_range(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        benchmark_data = json.loads(open(sample_benchmark).read())

        results = compute_recall(db_path, benchmark_data, k=5)
        assert 0.0 <= results["mrr"] <= 1.0
