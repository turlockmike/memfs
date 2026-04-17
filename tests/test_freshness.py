"""Tests for M5 — freshness stamps + filter + scan."""

from datetime import datetime, timezone, timedelta
import json
import os
import subprocess
import sys
import pytest

from memfs.indexer import index_file
from memfs.search import grep, _freshness_status
from memfs.graph import get_node
from memfs.parser import parse_file


def _ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class TestParserFreshness:
    def test_extracts_verified_at(self, tmp_path):
        f = tmp_path / "fact.md"
        f.write_text(
            "---\nlayer: 2\n"
            'freshness_verified_at: "2026-04-16T00:00:00+00:00"\n'
            "freshness_source_url: https://example.com/docs\n"
            "freshness_stale_after_days: 30\n"
            "---\n# Fact\nBody"
        )
        parsed = parse_file(str(f))
        assert "2026-04-16" in parsed["freshness_verified_at"]
        assert parsed["freshness_source_url"] == "https://example.com/docs"
        assert parsed["freshness_stale_after_days"] == 30


class TestFreshnessStatus:
    def test_fresh(self):
        assert _freshness_status({
            "freshness_verified_at": _ago(5),
            "freshness_stale_after_days": 30,
        }) == "fresh"

    def test_stale(self):
        assert _freshness_status({
            "freshness_verified_at": _ago(45),
            "freshness_stale_after_days": 30,
        }) == "stale"

    def test_never_verified(self):
        assert _freshness_status({
            "freshness_verified_at": None,
            "freshness_stale_after_days": 30,
        }) == "never_verified"

    def test_verified_no_window(self):
        # If verified_at set but no stale_after_days — treat as fresh
        assert _freshness_status({
            "freshness_verified_at": _ago(5),
            "freshness_stale_after_days": None,
        }) == "fresh"


class TestIndexerFreshness:
    def test_stores_freshness_fields(self, graph, tmp_path):
        f = tmp_path / "fact.md"
        f.write_text(
            f"---\nlayer: 2\n"
            f"freshness_verified_at: {_ago(5)}\n"
            f"freshness_source_url: https://ex.com\n"
            f"freshness_stale_after_days: 30\n"
            f"---\n# Fact\nBody"
        )
        index_file(graph, str(tmp_path), "fact.md")
        node = get_node(graph, "fact.md")
        assert node["freshness_stale_after_days"] == 30
        assert node["freshness_source_url"] == "https://ex.com"


class TestGrepFreshness:
    @pytest.fixture
    def graph_with_freshness(self, graph, tmp_path):
        (tmp_path / "fresh.md").write_text(
            f"---\nlayer: 2\n"
            f"freshness_verified_at: {_ago(5)}\n"
            f"freshness_stale_after_days: 30\n"
            f"---\n# Fresh fact\nOllama supports gemma4 models."
        )
        (tmp_path / "stale.md").write_text(
            f"---\nlayer: 2\n"
            f"freshness_verified_at: {_ago(90)}\n"
            f"freshness_stale_after_days: 30\n"
            f"---\n# Stale fact\nOllama supports gemma4 models."
        )
        (tmp_path / "unverified.md").write_text(
            "---\nlayer: 2\n---\n# Unverified\nOllama supports gemma4 models."
        )
        for p in ("fresh.md", "stale.md", "unverified.md"):
            index_file(graph, str(tmp_path), p)
        return graph

    def test_grep_returns_freshness_field(self, graph_with_freshness):
        results = grep(graph_with_freshness, "ollama gemma4")
        statuses = {r["path"]: r["freshness"] for r in results}
        assert statuses.get("fresh.md") == "fresh"
        assert statuses.get("stale.md") == "stale"
        assert statuses.get("unverified.md") == "never_verified"

    def test_fresh_only_filters_stale(self, graph_with_freshness):
        results = grep(graph_with_freshness, "ollama gemma4", fresh_only=True)
        statuses = {r["path"]: r["freshness"] for r in results}
        assert "stale.md" not in statuses
        # fresh.md and unverified.md remain
        assert "fresh.md" in statuses


class TestFreshnessScanCli:
    def test_reports_stale(self, graph, tmp_path):
        (tmp_path / "stale.md").write_text(
            f"---\nlayer: 2\n"
            f"freshness_verified_at: {_ago(60)}\n"
            f"freshness_stale_after_days: 30\n"
            f"freshness_source_url: https://ex.com\n"
            f"---\n# Stale\nBody"
        )
        (tmp_path / "fresh.md").write_text(
            f"---\nlayer: 2\n"
            f"freshness_verified_at: {_ago(5)}\n"
            f"freshness_stale_after_days: 30\n"
            f"---\n# Fresh\nBody"
        )
        env = os.environ.copy()
        env["MEM_HOME"] = str(tmp_path)
        subprocess.run(
            [sys.executable, "-m", "memfs.cli", "init", str(tmp_path)],
            env=env, capture_output=True, check=True,
        )
        result = subprocess.run(
            [sys.executable, "-m", "memfs.cli", "freshness-scan"],
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        lines = [json.loads(l) for l in result.stdout.strip().split("\n") if l]
        paths = {l["path"] for l in lines}
        assert "stale.md" in paths
        assert "fresh.md" not in paths
