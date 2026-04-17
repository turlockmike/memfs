"""Tests for CLI commands — init, grep, ls, status, reindex."""

import json
import os
import subprocess
import sys
import pytest


def run_memfs(*args, cwd=None, env=None):
    cmd = [sys.executable, "-m", "memfs.cli"] + list(args)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, env=full_env)
    return result.stdout, result.stderr, result.returncode


# Shared wipe fixture: autouse so every test starts with an empty graph
@pytest.fixture(autouse=True)
def _fresh(graph):
    """Piggyback on `graph` conftest fixture to ensure a clean DB per test."""
    yield


class TestInit:
    def test_runs(self, tmp_path):
        stdout, stderr, code = run_memfs("init", str(tmp_path))
        assert code == 0, f"stderr: {stderr}"
        # .mem dir is created as a side effect
        assert (tmp_path / ".mem").exists()

    def test_indexes_existing_files(self, tmp_path):
        (tmp_path / "test.md").write_text("# Test\nHello world")
        stdout, stderr, code = run_memfs("init", str(tmp_path))
        assert code == 0, f"stderr: {stderr}"
        lines = [json.loads(line) for line in stdout.strip().split("\n") if line.strip()]
        assert any("nodes" in line for line in lines)

    def test_idempotent(self, tmp_path):
        run_memfs("init", str(tmp_path))
        stdout, stderr, code = run_memfs("init", str(tmp_path))
        assert code == 0


class TestGrepCli:
    @pytest.fixture
    def initialized_root(self, tmp_path):
        (tmp_path / "kanji.md").write_text("# Kanji\nLearning kanji with SRS")
        (tmp_path / "cooking.md").write_text("# Cooking\nPasta carbonara recipe")
        run_memfs("init", str(tmp_path))
        return tmp_path

    def test_grep_returns_ndjson(self, initialized_root):
        stdout, stderr, code = run_memfs(
            "grep", "kanji",
            env={"MEM_HOME": str(initialized_root)}
        )
        assert code == 0, f"stderr: {stderr}"
        lines = [json.loads(line) for line in stdout.strip().split("\n") if line.strip()]
        assert len(lines) >= 1
        paths = [l["path"] for l in lines]
        assert "kanji.md" in paths

    def test_grep_no_results_exit_0(self, initialized_root):
        stdout, stderr, code = run_memfs(
            "grep", "xyzzy_nothing",
            env={"MEM_HOME": str(initialized_root)}
        )
        assert code == 0
        assert stdout.strip() == ""

    def test_grep_output_has_required_fields(self, initialized_root):
        stdout, stderr, code = run_memfs(
            "grep", "kanji",
            env={"MEM_HOME": str(initialized_root)}
        )
        result = json.loads(stdout.strip().split("\n")[0])
        for field in ("path", "title", "rank", "score", "snippet"):
            assert field in result, f"Missing field: {field}"


class TestLsCli:
    @pytest.fixture
    def initialized_root(self, tmp_path):
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        os.makedirs(tmp_path / "sub")
        (tmp_path / "sub" / "c.md").write_text("# C")
        run_memfs("init", str(tmp_path))
        return tmp_path

    def test_ls_lists_all_files(self, initialized_root):
        stdout, stderr, code = run_memfs(
            "ls", env={"MEM_HOME": str(initialized_root)}
        )
        assert code == 0
        lines = [json.loads(line) for line in stdout.strip().split("\n") if line.strip()]
        paths = {l["path"] for l in lines}
        assert "a.md" in paths
        assert "b.md" in paths
        assert "sub/c.md" in paths

    def test_ls_subdir(self, initialized_root):
        stdout, stderr, code = run_memfs(
            "ls", "sub", env={"MEM_HOME": str(initialized_root)}
        )
        assert code == 0
        lines = [json.loads(line) for line in stdout.strip().split("\n") if line.strip()]
        paths = {l["path"] for l in lines}
        assert "sub/c.md" in paths
        assert "a.md" not in paths


class TestStatusCli:
    def test_status_shows_counts(self, tmp_path):
        (tmp_path / "a.md").write_text("# A\nLink to [[b.md]]")
        (tmp_path / "b.md").write_text("# B")
        run_memfs("init", str(tmp_path))
        stdout, stderr, code = run_memfs(
            "status", env={"MEM_HOME": str(tmp_path)}
        )
        assert code == 0
        status = json.loads(stdout.strip())
        assert status["nodes"] == 2
        assert "edges" in status


class TestReindexCli:
    def test_reindex_rebuilds(self, tmp_path):
        (tmp_path / "a.md").write_text("# A")
        run_memfs("init", str(tmp_path))
        (tmp_path / "b.md").write_text("# B")
        stdout, stderr, code = run_memfs(
            "reindex", env={"MEM_HOME": str(tmp_path)}
        )
        assert code == 0, f"stderr: {stderr}"
        stdout2, _, _ = run_memfs(
            "status", env={"MEM_HOME": str(tmp_path)}
        )
        status = json.loads(stdout2.strip())
        assert status["nodes"] == 2
