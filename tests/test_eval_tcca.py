"""Tests for M3 — TCCA instrumentation + reference adapters."""

import json
import os
import pytest

from memfs.eval import ingest_benchmark
from memfs.tcca import (
    run_tcca, adapter_memfs, adapter_bm25, adapter_accumulate,
    _compute_baseline_tokens, _count_tokens, ADAPTERS,
)


@pytest.fixture
def synth_benchmark(tmp_path):
    data = [{
        "question_id": "syn_1",
        "question_type": "single-session-user",
        "question": "What color is the sky?",
        "answer": "blue",
        "question_date": "2026-04-16",
        "haystack_dates": ["2026-04-15"],
        "haystack_session_ids": ["sky_session"],
        "answer_session_ids": ["sky_session"],
        "haystack_sessions": [[
            {"role": "user", "content": "The sky is blue most days. Sometimes it's gray.", "has_answer": True},
            {"role": "assistant", "content": "Indeed — atmospheric scattering."},
        ]],
    }, {
        "question_id": "syn_2",
        "question_type": "multi-session",
        "question": "What is my favorite fruit?",
        "answer": "mango",
        "question_date": "2026-04-16",
        "haystack_dates": ["2026-04-15"],
        "haystack_session_ids": ["fruit_session"],
        "answer_session_ids": ["fruit_session"],
        "haystack_sessions": [[
            {"role": "user", "content": "My favorite fruit is mango. I love them.", "has_answer": True},
            {"role": "assistant", "content": "Nice tropical pick."},
        ]],
    }]
    f = tmp_path / "bench.json"
    f.write_text(json.dumps(data))
    return str(f), data


@pytest.fixture
def ingested_root(graph, tmp_path, synth_benchmark):
    bench_path, _ = synth_benchmark
    root = tmp_path / "eval"
    root.mkdir()
    ingest_benchmark(bench_path, str(root))
    return root


class TestTokenCounting:
    def test_empty_is_zero(self):
        assert _count_tokens("") == 0

    def test_non_empty_is_positive(self):
        assert _count_tokens("Hello world") > 0


class TestAdapters:
    def test_memfs_adapter_returns_context(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        ctx = adapter_memfs(str(ingested_root), data[0])
        assert "adapter" in ctx
        assert ctx["adapter"] == "memfs"
        assert ctx["retrieval_tokens"] == 0
        assert isinstance(ctx["retrieved_paths"], list)

    def test_bm25_adapter_returns_context(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        ctx = adapter_bm25(str(ingested_root), data[0])
        assert ctx["adapter"] == "bm25"
        assert ctx["retrieval_tokens"] == 0

    def test_accumulate_adapter_loads_all(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        ctx = adapter_accumulate(str(ingested_root), data[0])
        assert ctx["adapter"] == "accumulate"
        # accumulate loads all sessions
        assert len(ctx["retrieved_paths"]) >= 2


class TestBaselineTokens:
    def test_baseline_positive(self, ingested_root):
        baseline = _compute_baseline_tokens(str(ingested_root))
        assert baseline > 0


class TestTcca:
    def test_correct_answer_gives_positive_tcca(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        baseline = _compute_baseline_tokens(str(ingested_root))

        result = run_tcca(
            str(ingested_root), data[0], adapter_name="memfs",
            baseline_tokens=baseline,
            llm=lambda prompt: "The sky is blue.",
            judge=lambda **kw: True,
        )
        assert result["answer_correct"] is True
        assert result["tcca"] > 0
        # For memfs, retrieval_tokens = 0; total = prompt + generation
        assert result["retrieval_tokens"] == 0
        assert result["prompt_tokens"] > 0
        assert result["generation_tokens"] > 0
        assert result["total_tokens"] == result["prompt_tokens"] + result["generation_tokens"]

    def test_wrong_answer_gives_zero_tcca(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        baseline = _compute_baseline_tokens(str(ingested_root))

        result = run_tcca(
            str(ingested_root), data[0], adapter_name="memfs",
            baseline_tokens=baseline,
            llm=lambda prompt: "It's purple with green stripes.",
            judge=lambda **kw: False,
        )
        assert result["answer_correct"] is False
        assert result["tcca"] == 0.0

    def test_accumulate_has_larger_context(self, ingested_root, synth_benchmark):
        """Accumulate adapter should have context_tokens >= memfs adapter."""
        _, data = synth_benchmark
        baseline = _compute_baseline_tokens(str(ingested_root))

        memfs_result = run_tcca(
            str(ingested_root), data[0], adapter_name="memfs",
            baseline_tokens=baseline,
            llm=lambda prompt: "Blue.",
            judge=lambda **kw: True,
        )
        acc_result = run_tcca(
            str(ingested_root), data[0], adapter_name="accumulate",
            baseline_tokens=baseline,
            llm=lambda prompt: "Blue.",
            judge=lambda **kw: True,
        )
        assert acc_result["context_tokens"] >= memfs_result["context_tokens"]
        # memfs TCCA should be >= accumulate TCCA for a correct answer
        # (smaller denominator, same numerator)
        assert memfs_result["tcca"] >= acc_result["tcca"]

    def test_bm25_adapter_produces_result(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        baseline = _compute_baseline_tokens(str(ingested_root))

        result = run_tcca(
            str(ingested_root), data[0], adapter_name="bm25",
            baseline_tokens=baseline,
            llm=lambda prompt: "Blue.",
            judge=lambda **kw: True,
        )
        assert result["adapter"] == "bm25"
        assert result["answer_correct"] is True

    def test_all_adapters_registered(self):
        assert set(ADAPTERS.keys()) == {"memfs", "bm25", "accumulate"}

    def test_question_type_preserved(self, ingested_root, synth_benchmark):
        _, data = synth_benchmark
        baseline = _compute_baseline_tokens(str(ingested_root))
        result = run_tcca(
            str(ingested_root), data[1], adapter_name="memfs",
            baseline_tokens=baseline,
            llm=lambda prompt: "Mango.",
            judge=lambda **kw: True,
        )
        assert result["question_type"] == "multi-session"
