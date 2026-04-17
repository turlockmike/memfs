"""Tests for M2 — layer typing + mandatory provenance."""

import os
import subprocess
import sys
import json
import pytest

from memfs.indexer import index_file
from memfs.search import grep
from memfs.graph import get_node, count_nodes
from memfs.parser import parse_file


class TestParserLayerExtraction:
    def test_extracts_layer(self, tmp_path):
        f = tmp_path / "note.md"
        f.write_text("---\nlayer: 2\n---\n# Note\nBody")
        parsed = parse_file(str(f))
        assert parsed["layer"] == 2

    def test_extracts_source(self, tmp_path):
        f = tmp_path / "summary.md"
        f.write_text(
            "---\nlayer: 3\nsource: sessions/2026-04-16.md\n---\n"
            "# Summary\nBody"
        )
        parsed = parse_file(str(f))
        assert parsed["layer"] == 3
        assert parsed["source"] == "sessions/2026-04-16.md"


class TestLayerValidation:
    def test_layer_2_no_source_ok(self, graph, tmp_path, capsys):
        f = tmp_path / "kb.md"
        f.write_text("---\nlayer: 2\n---\n# KB entry\nBody")
        ok = index_file(graph, str(tmp_path), "kb.md")
        assert ok is True
        node = get_node(graph, "kb.md")
        assert node is not None
        assert node["layer"] == 2

    def test_layer_3_without_source_rejected(self, graph, tmp_path, capsys):
        f = tmp_path / "bad-summary.md"
        f.write_text("---\nlayer: 3\n---\n# Summary with no source\nBody")
        ok = index_file(graph, str(tmp_path), "bad-summary.md")
        assert ok is False
        assert get_node(graph, "bad-summary.md") is None
        captured = capsys.readouterr()
        err_lines = [json.loads(l) for l in captured.err.strip().split("\n") if l]
        assert any(e.get("event") == "quarantine" for e in err_lines)

    def test_layer_3_with_source_ok(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Source file")
        f = tmp_path / "summary.md"
        f.write_text(
            "---\nlayer: 3\nsource: src.md\n---\n# Summary\nBody"
        )
        ok = index_file(graph, str(tmp_path), "summary.md")
        assert ok is True
        node = get_node(graph, "summary.md")
        assert node["layer"] == 3
        assert node["source"] == "src.md"

    def test_layer_5_identity_without_source_rejected(self, graph, tmp_path):
        f = tmp_path / "identity.md"
        f.write_text("---\nlayer: 5\n---\n# Identity\nBody")
        ok = index_file(graph, str(tmp_path), "identity.md")
        assert ok is False
        assert get_node(graph, "identity.md") is None

    def test_layer_out_of_range_rejected(self, graph, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nlayer: 7\n---\n# Bad layer\nBody")
        ok = index_file(graph, str(tmp_path), "bad.md")
        assert ok is False

    def test_layer_zero_rejected(self, graph, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nlayer: 0\n---\n# Bad\nBody")
        ok = index_file(graph, str(tmp_path), "bad.md")
        assert ok is False

    def test_non_integer_layer_rejected(self, graph, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("---\nlayer: foo\n---\n# Bad\nBody")
        ok = index_file(graph, str(tmp_path), "bad.md")
        assert ok is False

    def test_no_layer_defaults_to_2(self, graph, tmp_path):
        f = tmp_path / "plain.md"
        f.write_text("# Just a note\nBody")
        ok = index_file(graph, str(tmp_path), "plain.md")
        assert ok is True
        node = get_node(graph, "plain.md")
        assert node["layer"] == 2


class TestLayerSearchFilter:
    @pytest.fixture
    def layered_graph(self, graph, tmp_path):
        (tmp_path / "raw.md").write_text(
            "---\nlayer: 2\n---\n# Raw KB\nMeeting notes about kanji study."
        )
        (tmp_path / "src.md").write_text("# Source doc\nAbout kanji study.")
        (tmp_path / "summary.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Kanji summary\nKanji study strategies synthesized."
        )
        (tmp_path / "opinion.md").write_text(
            "---\nlayer: 4\nsource: summary.md\n---\n"
            "# Opinion on kanji\nMy take on kanji study strategies."
        )
        from memfs.indexer import index_directory
        index_directory(graph, str(tmp_path))
        return graph

    def test_grep_without_layer_returns_all(self, layered_graph):
        results = grep(layered_graph, "kanji")
        layers = {r["layer"] for r in results}
        assert len(layers) >= 2

    def test_grep_layer_3_only(self, layered_graph):
        results = grep(layered_graph, "kanji", layer=3)
        layers = {r["layer"] for r in results}
        assert layers == {3}

    def test_grep_layer_2_only(self, layered_graph):
        results = grep(layered_graph, "kanji", layer=2)
        for r in results:
            assert r["layer"] == 2


class TestCliLayerFlag:
    def test_grep_layer_flag(self, graph, tmp_path):
        (tmp_path / "src.md").write_text("# Src")
        (tmp_path / "a.md").write_text(
            "---\nlayer: 2\n---\n# Raw\nAlpha beta gamma content."
        )
        (tmp_path / "b.md").write_text(
            "---\nlayer: 3\nsource: src.md\n---\n"
            "# Summary\nAlpha beta gamma content."
        )
        # Init
        env = os.environ.copy()
        env["MEM_HOME"] = str(tmp_path)
        subprocess.run(
            [sys.executable, "-m", "memfs.cli", "init", str(tmp_path)],
            env=env, capture_output=True, check=True,
        )

        result = subprocess.run(
            [sys.executable, "-m", "memfs.cli", "grep", "alpha", "--layer", "3"],
            env=env, capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        lines = [json.loads(l) for l in result.stdout.strip().split("\n") if l]
        layers = {l["layer"] for l in lines}
        assert layers == {3}
