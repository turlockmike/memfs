"""Tests for M4 — contradiction detection on layer-3+ ingest.

Heuristic tests (original M4) run with MEMFS_CONTRADICTION_SKIP_SEMANTIC=1
(set in conftest). The semantic stage has dedicated tests in
TestSemanticStage below that mock subprocess.
"""

import json
import os
import subprocess
import unittest.mock as mock

import pytest

from memfs.indexer import index_file
from memfs.contradiction import (
    detect_contradictions,
    scan_corpus,
    _semantic_contradiction,
    _extract_judge_json,
)


class TestDetection:
    def test_no_contradictions_for_layer_2(self, graph, tmp_path):
        (tmp_path / "a.md").write_text(
            "---\nlayer: 2\n---\n# A\nNative CronCreate persists across sessions."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 2\n---\n# B\nNative CronCreate does not persist across sessions."
        )
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")
        # Both are layer 2 — detection skips
        conflicts = detect_contradictions(graph, "b.md")
        assert conflicts == []

    def test_detects_negation_asymmetry(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Claim A\nNative CronCreate persists across sessions correctly."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Claim B\nNative CronCreate does not persist across sessions — flag silently ignored."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        conflicts = detect_contradictions(graph, "b.md")
        assert len(conflicts) >= 1
        assert any(c["existing"] == "a.md" for c in conflicts)

    def test_creates_contradicts_edge(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Source doc")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nThe feature works correctly in all cases."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nThe feature is broken in all cases."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        conflicts = detect_contradictions(graph, "b.md")
        assert conflicts
        edges = graph.run(
            "MATCH (:Node {path: 'b.md'})-[r:CONTRADICTS]-(:Node {path: 'a.md'}) "
            "RETURN r.detected_at AS d"
        )
        assert list(edges)

    def test_ignores_unrelated_content(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "kanji.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Kanji\nStudying kanji with spaced repetition is effective."
        )
        (tmp_path / "cooking.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Pasta\nCarbonara requires guanciale and pecorino cheese."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "kanji.md")
        index_file(graph, str(tmp_path), "cooking.md")

        conflicts = detect_contradictions(graph, "cooking.md")
        # No meaningful overlap between kanji study and pasta
        assert conflicts == []

    def test_no_self_contradiction(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nThis is not the same as that."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        conflicts = detect_contradictions(graph, "a.md")
        # A is the only layer-3 node matching itself — should skip self
        assert not any(c["existing"] == "a.md" for c in conflicts)


class TestWatcherIntegration:
    def test_watcher_runs_detection(self, graph, tmp_path, capsys):
        """Creating a layer-3 file via the watcher handler triggers detection."""
        from memfs.watcher import MemfsEventHandler

        handler = MemfsEventHandler(str(tmp_path))
        (tmp_path / "src.md").write_text("# Src")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nThe feature works correctly in production."
        )
        handler.on_created_file(str(tmp_path / "src.md"))
        handler.on_created_file(str(tmp_path / "a.md"))

        # Create the conflicting file
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nThe feature is broken in production."
        )
        handler.on_created_file(str(tmp_path / "b.md"))

        captured = capsys.readouterr()
        lines = [l for l in captured.err.strip().split("\n") if l]
        # At least one `conflict` event should have been emitted
        assert any('"event": "conflict"' in l for l in lines) or any(
            '"event":"conflict"' in l for l in lines
        )


class TestScanCorpus:
    """Batch-scan entry point — the path reindex uses (watcher bypass).

    Motivation: viable-memory S2 absorption requires contradiction detection
    to run not just incrementally (watcher) but also across the full corpus
    on demand (post-reindex, scheduled sweep, manual audit).
    """

    def test_empty_corpus(self, graph, tmp_path):
        """Empty corpus → scanned=0, no edges, no conflicts."""
        result = scan_corpus(graph)
        assert result["scanned"] == 0
        assert result["conflicts"] == []
        assert result["edges_created"] == 0

    def test_skips_layer_2_nodes(self, graph, tmp_path):
        """Only layer-3+ nodes are scanned."""
        (tmp_path / "a.md").write_text(
            "---\nlayer: 2\n---\nX is always correct."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 2\n---\nX is never correct."
        )
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")
        result = scan_corpus(graph)
        assert result["scanned"] == 0
        assert result["edges_created"] == 0

    def test_finds_cross_node_contradictions(self, graph, tmp_path):
        """Two layer-3 nodes with reversal bigram → one conflict, one pair."""
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nRouting is always enabled in production."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nRouting is never enabled in production."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        result = scan_corpus(graph)
        assert result["scanned"] == 2  # a.md and b.md (layer 3)
        # Dedupe by sorted pair; one conflict regardless of A->B / B->A both firing
        assert len(result["conflicts"]) == 1
        pair = result["conflicts"][0]
        assert sorted([pair["new"], pair["existing"]]) == ["a.md", "b.md"]
        assert "reversal" in pair["reason"]

    def test_idempotent_across_runs(self, graph, tmp_path):
        """Re-running produces no new edges (MERGE semantics)."""
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nFeature X works correctly."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nFeature X is broken."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        r1 = scan_corpus(graph)
        r2 = scan_corpus(graph)
        assert r1["scanned"] == r2["scanned"]
        # First run may or may not create edges (depends on detector heuristics);
        # either way second run creates none.
        assert r2["edges_created"] == 0

    def test_no_conflicts_between_unrelated_layer_3(self, graph, tmp_path):
        """Low-overlap nodes produce no conflicts even at layer 3+."""
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nThe pool filter runs weekly."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nKalshi markets settle at resolution."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        result = scan_corpus(graph)
        assert result["scanned"] == 2
        assert result["conflicts"] == []
        assert result["edges_created"] == 0

    def test_dedupes_bidirectional_pairs(self, graph, tmp_path):
        """A->B and B->A of the same pair count once in `conflicts`."""
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nService is enabled globally."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nService is disabled globally."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        result = scan_corpus(graph)
        # Pair seen from both ends; conflicts list carries exactly one entry
        assert len(result["conflicts"]) == 1


class TestSemanticStage:
    """The LLM-backed contradiction judge.

    Shipped 2026-04-18 after a coverage-level scan produced 12 conflicts, 0
    true positives — every flag was `reversal:correct->wrong` firing on
    shared vocabulary between retrospective documents that weren't actually
    contradicting each other. The semantic stage is the precision fix.

    These tests mock the `infer` subprocess so they run deterministically
    without Ollama. The 12 FPs from the 2026-04-18 02:06 CDT scan serve as
    regression fixtures — they must NOT produce edges under the mocked
    semantic judge returning `contradicts: false`.
    """

    def test_extract_judge_json_plain(self):
        assert _extract_judge_json('{"contradicts": true, "subject": "X"}') == {
            "contradicts": True, "subject": "X"
        }

    def test_extract_judge_json_with_prose(self):
        raw = 'Sure, here is the verdict: {"contradicts": false, "subject": null, "reason": "unrelated"}\nEnd.'
        got = _extract_judge_json(raw)
        assert got == {"contradicts": False, "subject": None, "reason": "unrelated"}

    def test_extract_judge_json_empty(self):
        assert _extract_judge_json("") is None
        assert _extract_judge_json("no json here") is None

    def test_extract_judge_json_malformed(self):
        assert _extract_judge_json("{not valid json}") is None

    def test_bypass_via_env(self, monkeypatch):
        """SKIP_SEMANTIC=1 → returns (True, semantic_bypassed) — heuristic wins."""
        monkeypatch.setenv("MEMFS_CONTRADICTION_SKIP_SEMANTIC", "1")
        ok, subj = _semantic_contradiction("A text", "B text")
        assert ok is True
        assert subj == "semantic_bypassed"

    def test_missing_infer(self, monkeypatch):
        """Missing infer binary → (False, infer_not_found)."""
        monkeypatch.delenv("MEMFS_CONTRADICTION_SKIP_SEMANTIC", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: None)
        ok, subj = _semantic_contradiction("A", "B")
        assert ok is False
        assert "infer_not_found" in subj

    def test_missing_role(self, monkeypatch, tmp_path):
        """Missing role file → (False, role_missing)."""
        monkeypatch.delenv("MEMFS_CONTRADICTION_SKIP_SEMANTIC", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/infer-fake")
        monkeypatch.setenv("HOME", str(tmp_path))  # no role file under this HOME
        ok, subj = _semantic_contradiction("A", "B")
        assert ok is False
        assert "role_missing" in subj

    def _mock_infer(self, stdout: str, returncode: int = 0):
        """Helper: patch subprocess.run to return canned infer output."""
        result = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        )
        return mock.patch("subprocess.run", return_value=result)

    def _prepare_infer_env(self, monkeypatch, tmp_path):
        """Create fake infer binary + role file so the guards pass."""
        monkeypatch.delenv("MEMFS_CONTRADICTION_SKIP_SEMANTIC", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/infer-fake")
        role_dir = tmp_path / ".config" / "infer" / "roles"
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "contradiction-judge.md").write_text("# judge")
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_semantic_agrees(self, monkeypatch, tmp_path):
        """Judge says contradicts:true → (True, subject)."""
        self._prepare_infer_env(monkeypatch, tmp_path)
        with self._mock_infer('{"contradicts": true, "subject": "CronCreate persistence", "reason": "opposite claims"}'):
            ok, subj = _semantic_contradiction("A text", "B text")
        assert ok is True
        assert "CronCreate" in subj

    def test_semantic_disagrees(self, monkeypatch, tmp_path):
        """Judge says contradicts:false → (False, ...). THE CORE FIX."""
        self._prepare_infer_env(monkeypatch, tmp_path)
        with self._mock_infer('{"contradicts": false, "subject": null, "reason": "different subjects"}'):
            ok, subj = _semantic_contradiction("A", "B")
        assert ok is False

    def test_semantic_garbage_output(self, monkeypatch, tmp_path):
        """Unparseable stdout → (False, parse)."""
        self._prepare_infer_env(monkeypatch, tmp_path)
        with self._mock_infer("not json at all"):
            ok, subj = _semantic_contradiction("A", "B")
        assert ok is False
        assert "parse" in subj

    def test_semantic_timeout(self, monkeypatch, tmp_path):
        """subprocess.TimeoutExpired → (False, timeout)."""
        self._prepare_infer_env(monkeypatch, tmp_path)
        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="infer", timeout=15)
        with mock.patch("subprocess.run", side_effect=_raise):
            ok, subj = _semantic_contradiction("A", "B")
        assert ok is False
        assert "timeout" in subj


class TestSemanticIntegration:
    """End-to-end: heuristic + semantic interaction on real-style inputs.

    Mocks the subprocess.run call to keep the test deterministic. Uses the
    actual graph + indexer path.
    """

    def _install_fake_infer(self, monkeypatch, tmp_path_factory):
        """Satisfy both `which infer` and the role-file existence check."""
        monkeypatch.delenv("MEMFS_CONTRADICTION_SKIP_SEMANTIC", raising=False)
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/infer-fake")
        # Role file must exist under HOME
        home = tmp_path_factory.mktemp("fake_home")
        role_dir = home / ".config" / "infer" / "roles"
        role_dir.mkdir(parents=True)
        (role_dir / "contradiction-judge.md").write_text("# judge")
        monkeypatch.setenv("HOME", str(home))

    def test_semantic_vetoes_heuristic_false_positive(
        self, graph, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Heuristic fires (reversal bigram) but semantic says no → NO edge.

        This is the regression test for the 2026-04-18 12-FP bug: Karpathy
        retrospective docs containing 'correct' and 'wrong' in different
        contexts triggered the heuristic but were not actual contradictions.
        """
        self._install_fake_infer(monkeypatch, tmp_path_factory)
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nThe old approach was correct for that era. New approach needed."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nThe prior design turned out to be wrong; hence the rework."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        # Semantic judge says NOT a contradiction
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"contradicts": false, "subject": null, "reason": "retrospective, different subjects"}',
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=fake):
            conflicts = detect_contradictions(graph, "b.md")

        # Zero edges in the graph
        edges = graph.run(
            "MATCH ()-[r:CONTRADICTS]-() RETURN count(r) AS n"
        )
        assert edges[0]["n"] == 0
        assert conflicts == []

    def test_semantic_confirms_heuristic_true_positive(
        self, graph, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Heuristic fires AND semantic agrees → edge emitted with subject."""
        self._install_fake_infer(monkeypatch, tmp_path_factory)
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nNative CronCreate persists across sessions reliably."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nNative CronCreate does not persist across sessions — flag ignored."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"contradicts": true, "subject": "CronCreate persistence across sessions", "reason": "opposite"}',
            stderr="",
        )
        with mock.patch("subprocess.run", return_value=fake):
            conflicts = detect_contradictions(graph, "b.md")

        assert len(conflicts) >= 1
        # The subject from the judge surfaces in the reason field
        assert any("CronCreate" in c["reason"] for c in conflicts)

    def test_semantic_error_fails_closed(
        self, graph, tmp_path, monkeypatch, tmp_path_factory
    ):
        """Semantic subprocess error → no edge emitted. Precision over recall."""
        self._install_fake_infer(monkeypatch, tmp_path_factory)
        (tmp_path / "src.md").write_text("# Source")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# A\nService is enabled globally in production."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# B\nService is disabled globally in production."
        )
        index_file(graph, str(tmp_path), "src.md")
        index_file(graph, str(tmp_path), "a.md")
        index_file(graph, str(tmp_path), "b.md")

        # Semantic subprocess times out
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="infer", timeout=15)
        with mock.patch("subprocess.run", side_effect=_timeout):
            conflicts = detect_contradictions(graph, "b.md")

        # Even though the heuristic would have fired, the semantic failure
        # means NO edge. This is the defensive posture — infrastructure
        # breakage must not emit junk contradictions.
        edges = graph.run(
            "MATCH ()-[r:CONTRADICTS]-() RETURN count(r) AS n"
        )
        assert edges[0]["n"] == 0
        assert conflicts == []
