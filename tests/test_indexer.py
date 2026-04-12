"""Tests for the indexer — scanning dirs, indexing files, maintaining edges."""

import os
import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_file, index_directory, reindex, update_node


@pytest.fixture
def mem_root(tmp_path):
    """Create a temp memory root with .mem/memory.db."""
    db_path = str(tmp_path / ".mem" / "memory.db")
    create_db(db_path)
    return tmp_path, db_path


class TestIndexFile:
    def test_indexes_new_file(self, mem_root):
        root, db_path = mem_root
        f = root / "hello.md"
        f.write_text("# Hello\nWorld")
        conn = connect(db_path)
        index_file(conn, str(root), "hello.md")
        node = conn.execute("SELECT * FROM nodes WHERE path='hello.md'").fetchone()
        conn.close()
        assert node is not None

    def test_indexes_into_fts(self, mem_root):
        root, db_path = mem_root
        f = root / "hello.md"
        f.write_text("# Hello\nThe quick brown fox")
        conn = connect(db_path)
        index_file(conn, str(root), "hello.md")
        results = conn.execute(
            "SELECT path FROM fts WHERE fts MATCH 'fox'"
        ).fetchall()
        conn.close()
        assert len(results) == 1
        assert results[0][0] == "hello.md"

    def test_creates_link_edges(self, mem_root):
        root, db_path = mem_root
        (root / "target.md").write_text("# Target")
        (root / "source.md").write_text("# Source\nSee [[target.md]]")
        conn = connect(db_path)
        index_file(conn, str(root), "target.md")
        index_file(conn, str(root), "source.md")
        edges = conn.execute(
            "SELECT source, target, type FROM edges WHERE type='link'"
        ).fetchall()
        conn.close()
        assert len(edges) == 1
        assert edges[0][0] == "source.md"
        assert edges[0][1] == "target.md"

    def test_broken_link_gets_zero_strength(self, mem_root):
        root, db_path = mem_root
        (root / "source.md").write_text("See [[nonexistent.md]]")
        conn = connect(db_path)
        index_file(conn, str(root), "source.md")
        edge = conn.execute(
            "SELECT strength FROM edges WHERE target='nonexistent.md'"
        ).fetchone()
        conn.close()
        assert edge is not None
        assert edge[0] == 0.0

    def test_extracts_date_hint(self, mem_root):
        root, db_path = mem_root
        f = root / "dated.md"
        f.write_text("---\ndate: 2026-04-12\n---\n# Dated note")
        conn = connect(db_path)
        index_file(conn, str(root), "dated.md")
        row = conn.execute(
            "SELECT date_hint FROM nodes WHERE path='dated.md'"
        ).fetchone()
        conn.close()
        assert row[0] == "2026-04-12"


class TestUpdateNode:
    def test_updates_on_content_change(self, mem_root):
        root, db_path = mem_root
        f = root / "changing.md"
        f.write_text("# Version 1")
        conn = connect(db_path)
        index_file(conn, str(root), "changing.md")
        old_hash = conn.execute(
            "SELECT content_hash FROM nodes WHERE path='changing.md'"
        ).fetchone()[0]
        f.write_text("# Version 2 — totally different")
        update_node(conn, str(root), "changing.md")
        new_hash = conn.execute(
            "SELECT content_hash FROM nodes WHERE path='changing.md'"
        ).fetchone()[0]
        conn.close()
        assert old_hash != new_hash

    def test_updates_fts_on_change(self, mem_root):
        root, db_path = mem_root
        f = root / "changing.md"
        f.write_text("# Alpha\nOriginal content about alpha")
        conn = connect(db_path)
        index_file(conn, str(root), "changing.md")
        f.write_text("# Beta\nNew content about beta")
        update_node(conn, str(root), "changing.md")
        alpha = conn.execute("SELECT path FROM fts WHERE fts MATCH 'alpha'").fetchall()
        beta = conn.execute("SELECT path FROM fts WHERE fts MATCH 'beta'").fetchall()
        conn.close()
        assert len(alpha) == 0
        assert len(beta) == 1


class TestIndexDirectory:
    def test_indexes_all_md_files(self, mem_root):
        root, db_path = mem_root
        (root / "a.md").write_text("# A")
        (root / "b.md").write_text("# B")
        (root / "not-md.txt").write_text("Not indexed")
        os.makedirs(root / "sub")
        (root / "sub" / "c.md").write_text("# C")
        conn = connect(db_path)
        index_directory(conn, str(root))
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert count == 3

    def test_skips_memignore_patterns(self, mem_root):
        root, db_path = mem_root
        (root / ".memignore").write_text("drafts/\n")
        os.makedirs(root / "drafts")
        (root / "drafts" / "wip.md").write_text("# WIP")
        (root / "visible.md").write_text("# Visible")
        conn = connect(db_path)
        index_directory(conn, str(root))
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert count == 1

    def test_skips_dot_mem_dir(self, mem_root):
        root, db_path = mem_root
        # .mem/ should always be ignored
        (root / "visible.md").write_text("# Visible")
        conn = connect(db_path)
        index_directory(conn, str(root))
        paths = [r[0] for r in conn.execute("SELECT path FROM nodes").fetchall()]
        conn.close()
        assert all(not p.startswith(".mem") for p in paths)


class TestReindex:
    def test_rebuilds_from_scratch(self, mem_root):
        root, db_path = mem_root
        (root / "a.md").write_text("# A\nLink to [[b.md]]")
        (root / "b.md").write_text("# B")
        conn = connect(db_path)
        index_directory(conn, str(root))
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 2
        # Delete a file and reindex
        os.unlink(root / "b.md")
        reindex(conn, str(root))
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
        # Edge to b.md should be broken (strength=0) or gone
        edges = conn.execute("SELECT * FROM edges").fetchall()
        conn.close()
        for e in edges:
            if "b.md" in str(e):
                assert e[3] == 0.0  # strength column
