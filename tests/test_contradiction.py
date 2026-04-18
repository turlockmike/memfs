"""Tests for M4 — contradiction detection on layer-3+ ingest."""

import pytest

from memfs.indexer import index_file
from memfs.contradiction import detect_contradictions, scan_corpus


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
