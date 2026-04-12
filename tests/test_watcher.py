"""Tests for the filesystem watcher daemon."""

import os
import time
import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_directory
from memfs.watcher import MemfsEventHandler


@pytest.fixture
def watched_root(tmp_path):
    """Create a memory root with index and event handler."""
    db_path = str(tmp_path / ".mem" / "memory.db")
    create_db(db_path)
    conn = connect(db_path)
    handler = MemfsEventHandler(str(tmp_path), db_path)
    return tmp_path, db_path, handler


class TestEventHandler:
    def test_on_created_indexes_new_file(self, watched_root):
        root, db_path, handler = watched_root
        f = root / "new.md"
        f.write_text("# New File\nContent here")
        handler.on_created_file(str(f))
        conn = connect(db_path)
        node = conn.execute("SELECT * FROM nodes WHERE path='new.md'").fetchone()
        conn.close()
        assert node is not None

    def test_on_created_ignores_non_md(self, watched_root):
        root, db_path, handler = watched_root
        f = root / "readme.txt"
        f.write_text("Not markdown")
        handler.on_created_file(str(f))
        conn = connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert count == 0

    def test_on_modified_updates_index(self, watched_root):
        root, db_path, handler = watched_root
        f = root / "existing.md"
        f.write_text("# Version 1")
        handler.on_created_file(str(f))
        conn = connect(db_path)
        old_hash = conn.execute(
            "SELECT content_hash FROM nodes WHERE path='existing.md'"
        ).fetchone()[0]
        conn.close()

        f.write_text("# Version 2 — changed")
        handler.on_modified_file(str(f))
        conn = connect(db_path)
        new_hash = conn.execute(
            "SELECT content_hash FROM nodes WHERE path='existing.md'"
        ).fetchone()[0]
        conn.close()
        assert old_hash != new_hash

    def test_on_deleted_removes_from_index(self, watched_root):
        root, db_path, handler = watched_root
        f = root / "doomed.md"
        f.write_text("# Doomed")
        handler.on_created_file(str(f))
        os.unlink(f)
        handler.on_deleted_file(str(f))
        conn = connect(db_path)
        node = conn.execute("SELECT * FROM nodes WHERE path='doomed.md'").fetchone()
        conn.close()
        assert node is None

    def test_on_created_upgrades_broken_links(self, watched_root):
        root, db_path, handler = watched_root
        # Create source with broken link
        (root / "source.md").write_text("Link to [[target.md]]")
        handler.on_created_file(str(root / "source.md"))
        conn = connect(db_path)
        edge = conn.execute(
            "SELECT strength FROM edges WHERE target='target.md'"
        ).fetchone()
        assert edge[0] == 0.0  # broken
        conn.close()

        # Now create the target file
        (root / "target.md").write_text("# Target")
        handler.on_created_file(str(root / "target.md"))
        conn = connect(db_path)
        edge = conn.execute(
            "SELECT strength FROM edges WHERE target='target.md' AND type='link'"
        ).fetchone()
        conn.close()
        assert edge[0] == 1.0  # upgraded

    def test_on_moved_updates_paths(self, watched_root):
        root, db_path, handler = watched_root
        f = root / "old.md"
        f.write_text("# Old\nLink to [[other.md]]")
        (root / "other.md").write_text("# Other")
        handler.on_created_file(str(f))
        handler.on_created_file(str(root / "other.md"))

        # Move the file
        new_path = root / "new.md"
        os.rename(f, new_path)
        handler.on_moved_file(str(f), str(new_path))

        conn = connect(db_path)
        old_node = conn.execute("SELECT * FROM nodes WHERE path='old.md'").fetchone()
        new_node = conn.execute("SELECT * FROM nodes WHERE path='new.md'").fetchone()
        conn.close()
        assert old_node is None
        assert new_node is not None

    def test_ignores_memignore_patterns(self, watched_root):
        root, db_path, handler = watched_root
        (root / ".memignore").write_text("drafts/\n")
        os.makedirs(root / "drafts")
        f = root / "drafts" / "wip.md"
        f.write_text("# WIP")
        handler.on_created_file(str(f))
        conn = connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        assert count == 0

    def test_on_directory_moved_updates_all_paths(self, watched_root):
        root, db_path, handler = watched_root
        os.makedirs(root / "old_dir")
        (root / "old_dir" / "a.md").write_text("# A")
        (root / "old_dir" / "b.md").write_text("# B\nSee [[a.md]]")
        handler.on_created_file(str(root / "old_dir" / "a.md"))
        handler.on_created_file(str(root / "old_dir" / "b.md"))

        # Move directory
        os.rename(root / "old_dir", root / "new_dir")
        handler.on_moved_directory(str(root / "old_dir"), str(root / "new_dir"))

        conn = connect(db_path)
        paths = [r[0] for r in conn.execute("SELECT path FROM nodes").fetchall()]
        conn.close()
        assert "new_dir/a.md" in paths
        assert "new_dir/b.md" in paths
        assert "old_dir/a.md" not in paths
