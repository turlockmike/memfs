"""Tests for QA evaluation — can the agent answer questions using retrieved context?"""

import json
import os
import pytest
from unittest.mock import patch, MagicMock

from memfs.db import create_db, connect
from memfs.eval import ingest_benchmark, generate_hypothesis, score_hypothesis


@pytest.fixture
def sample_benchmark(tmp_path):
    data = [{
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "What was wrong with the GPS?",
        "answer": "It was not functioning correctly after the service",
        "question_date": "2023/04/10 (Mon) 23:07",
        "haystack_dates": ["2023/04/10 (Mon) 17:50"],
        "haystack_session_ids": ["session_a"],
        "answer_session_ids": ["session_a"],
        "haystack_sessions": [[
            {"role": "user", "content": "My car GPS is not functioning correctly after the service on March 15th.", "has_answer": True},
            {"role": "assistant", "content": "I'm sorry to hear that. Have you tried resetting it?"},
        ]],
    }]
    f = tmp_path / "benchmark.json"
    f.write_text(json.dumps(data))
    return str(f)


@pytest.fixture
def eval_root(tmp_path):
    root = tmp_path / "eval"
    root.mkdir()
    return root


class TestGenerateHypothesis:
    def test_returns_question_id_and_hypothesis(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        data = json.loads(open(sample_benchmark).read())
        entry = data[0]

        # Mock the LLM call
        with patch("memfs.eval._call_llm") as mock_llm:
            mock_llm.return_value = "The GPS was not functioning correctly"
            result = generate_hypothesis(db_path, entry)

        assert result["question_id"] == "q1"
        assert "hypothesis" in result
        assert len(result["hypothesis"]) > 0

    def test_passes_retrieved_context_to_llm(self, sample_benchmark, eval_root):
        ingest_benchmark(sample_benchmark, str(eval_root))
        db_path = str(eval_root / ".mem" / "memory.db")
        data = json.loads(open(sample_benchmark).read())
        entry = data[0]

        with patch("memfs.eval._call_llm") as mock_llm:
            mock_llm.return_value = "GPS not working"
            generate_hypothesis(db_path, entry)
            # Check that the LLM was called with context containing session content
            call_args = mock_llm.call_args[0][0]
            assert "GPS" in call_args or "car" in call_args


class TestScoreHypothesis:
    def test_correct_answer_scores_true(self):
        with patch("memfs.eval._call_llm") as mock_llm:
            mock_llm.return_value = "yes"
            score = score_hypothesis(
                question="What was wrong with the GPS?",
                answer="GPS not functioning correctly",
                hypothesis="The GPS was not functioning correctly after service",
                question_type="single-session-user",
            )
        assert score is True

    def test_wrong_answer_scores_false(self):
        with patch("memfs.eval._call_llm") as mock_llm:
            mock_llm.return_value = "no"
            score = score_hypothesis(
                question="What was wrong with the GPS?",
                answer="GPS not functioning correctly",
                hypothesis="The brakes were squeaking",
                question_type="single-session-user",
            )
        assert score is False
