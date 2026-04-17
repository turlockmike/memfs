"""Tests for M4 — contradiction detection on layer-3+ ingest."""

import pytest

from memfs.indexer import index_file
from memfs.contradiction import detect_contradictions


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
