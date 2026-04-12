"""Tests for neighborhood context in search results and orphan detection."""

import json
import os
import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_file, index_directory
from memfs.search import grep


@pytest.fixture
def structured_db(tmp_path):
    """Create a memory root with directory structure, index files, and links."""
    root = tmp_path
    db_path = str(root / ".mem" / "memory.db")
    create_db(db_path)

    # Create directory structure
    os.makedirs(root / "learning")
    os.makedirs(root / "projects")

    files = {
        "learning/index.md": "---\ntitle: Language Learning\n---\n# Language Learning\nMethods and progress for language acquisition.",
        "learning/kanji.md": "---\ntitle: Kanji Study\n---\n# Kanji Study\nUsing spaced repetition. See [[srs-methods]] and [[projects/satori.md]]",
        "learning/srs-methods.md": "# SRS Methods\nAnki, Wanikani, and custom tools.",
        "learning/vocabulary.md": "# Vocabulary\nCore 2000 words list.",
        "projects/satori.md": "---\ntitle: Satori App\n---\n# Satori App\nKanji curriculum app. See [[learning/kanji.md]]",
        "orphan.md": "# Orphan File\nThis file has no links and will never be searched.",
    }

    conn = connect(db_path)
    for name, content in files.items():
        (root / name).write_text(content)
    index_directory(conn, str(root))
    conn.close()
    return root, db_path


class TestNeighborhood:
    def test_grep_returns_directory(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        results = grep(conn, "kanji spaced repetition")
        conn.close()
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert kanji["directory"] == "learning"

    def test_grep_returns_siblings(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        results = grep(conn, "kanji spaced repetition")
        conn.close()
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert "learning/srs-methods.md" in kanji["siblings"]
        assert "learning/vocabulary.md" in kanji["siblings"]
        # Should not include itself
        assert "learning/kanji.md" not in kanji["siblings"]

    def test_grep_returns_index(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        results = grep(conn, "kanji spaced repetition")
        conn.close()
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert "index" in kanji
        assert kanji["index"]["path"] == "learning/index.md"
        assert kanji["index"]["title"] == "Language Learning"

    def test_grep_returns_outgoing_links(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        results = grep(conn, "kanji spaced repetition")
        conn.close()
        kanji = next((r for r in results if r["path"] == "learning/kanji.md"), None)
        assert kanji is not None
        assert "projects/satori.md" in kanji["links_to"]

    def test_grep_returns_incoming_links(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        results = grep(conn, "satori kanji app")
        conn.close()
        satori = next((r for r in results if r["path"] == "projects/satori.md"), None)
        assert satori is not None
        assert "learning/kanji.md" in satori["linked_from"]


class TestOrphans:
    def test_finds_orphan_files(self, structured_db):
        root, db_path = structured_db
        conn = connect(db_path)
        orphans = conn.execute("""
            SELECT n.path FROM nodes n
            WHERE n.path NOT IN (SELECT DISTINCT target FROM edges)
              AND n.path NOT IN (SELECT DISTINCT source FROM edges WHERE type='link')
              AND n.search_count = 0
        """).fetchall()
        conn.close()
        orphan_paths = [r[0] for r in orphans]
        assert "orphan.md" in orphan_paths
        # kanji.md has links so it shouldn't be an orphan
        assert "learning/kanji.md" not in orphan_paths
