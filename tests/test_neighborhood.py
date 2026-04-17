"""Tests for neighborhood context in search results and orphan detection."""

import os
import pytest

from memfs.indexer import index_directory
from memfs.search import grep
from memfs.graph import get_orphans


@pytest.fixture
def structured_graph(graph, tmp_path):
    """Directory structure, index files, links."""
    os.makedirs(tmp_path / "learning")
    os.makedirs(tmp_path / "projects")

    files = {
        "learning/index.md": "---\ntitle: Language Learning\n---\n# Language Learning\nMethods and progress for language acquisition.",
        "learning/kanji.md": "---\ntitle: Kanji Study\n---\n# Kanji Study\nUsing spaced repetition. See [[srs-methods]] and [[projects/satori.md]]",
        "learning/srs-methods.md": "# SRS Methods\nAnki, Wanikani, and custom tools.",
        "learning/vocabulary.md": "# Vocabulary\nCore 2000 words list.",
        "projects/satori.md": "---\ntitle: Satori App\n---\n# Satori App\nKanji curriculum app. See [[learning/kanji.md]]",
        "orphan.md": "# Orphan File\nThis file has no links and will never be searched.",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    index_directory(graph, str(tmp_path))
    return graph


class TestNeighborhood:
    def test_grep_returns_directory(self, structured_graph):
        results = grep(structured_graph, "kanji spaced repetition")
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert kanji["directory"] == "learning"

    def test_grep_returns_siblings(self, structured_graph):
        results = grep(structured_graph, "kanji spaced repetition")
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        sibling_paths = [s["path"] for s in kanji["siblings"]]
        assert "learning/srs-methods.md" in sibling_paths
        assert "learning/vocabulary.md" in sibling_paths
        assert "learning/kanji.md" not in sibling_paths
        srs = next(s for s in kanji["siblings"] if s["path"] == "learning/srs-methods.md")
        assert srs["title"] == "SRS Methods"

    def test_grep_returns_index(self, structured_graph):
        results = grep(structured_graph, "kanji spaced repetition")
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert "index" in kanji
        assert kanji["index"]["path"] == "learning/index.md"
        assert kanji["index"]["title"] == "Language Learning"

    def test_grep_returns_outgoing_links(self, structured_graph):
        results = grep(structured_graph, "kanji spaced repetition")
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert "projects/satori.md" in kanji["links_to"]

    def test_grep_returns_incoming_links(self, structured_graph):
        results = grep(structured_graph, "satori kanji app")
        satori = next((r for r in results if r["path"] == "projects/satori.md"), None)
        assert satori is not None
        assert "learning/kanji.md" in satori["linked_from"]


class TestOrphans:
    def test_finds_orphan_files(self, structured_graph):
        orphans = get_orphans(structured_graph)
        paths = [r["path"] for r in orphans]
        assert "orphan.md" in paths
        assert "learning/kanji.md" not in paths
