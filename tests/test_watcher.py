"""Tests for the filesystem watcher (event handler level, no daemon fork)."""

import os
import pytest

from memfs.watcher import MemfsEventHandler
from memfs.graph import get_node, count_nodes


@pytest.fixture
def watched(graph, tmp_path):
    """Return (root_path, handler). `graph` fixture clears DB."""
    handler = MemfsEventHandler(str(tmp_path))
    return tmp_path, handler


class TestEventHandler:
    def test_on_created_indexes_new_file(self, graph, watched):
        root, handler = watched
        f = root / "new.md"
        f.write_text("# New File\nContent here")
        handler.on_created_file(str(f))
        assert get_node(graph, "new.md") is not None

    def test_on_created_ignores_non_md(self, graph, watched):
        root, handler = watched
        f = root / "readme.txt"
        f.write_text("Not markdown")
        handler.on_created_file(str(f))
        assert count_nodes(graph) == 0

    def test_on_modified_updates_index(self, graph, watched):
        root, handler = watched
        f = root / "existing.md"
        f.write_text("# Version 1")
        handler.on_created_file(str(f))
        old_hash = get_node(graph, "existing.md")["content_hash"]
        f.write_text("# Version 2 — changed")
        handler.on_modified_file(str(f))
        new_hash = get_node(graph, "existing.md")["content_hash"]
        assert old_hash != new_hash

    def test_on_deleted_removes_from_index(self, graph, watched):
        root, handler = watched
        f = root / "doomed.md"
        f.write_text("# Doomed")
        handler.on_created_file(str(f))
        os.unlink(f)
        handler.on_deleted_file(str(f))
        assert get_node(graph, "doomed.md") is None

    def test_on_created_upgrades_broken_links(self, graph, watched):
        root, handler = watched
        (root / "source.md").write_text("Link to [[target.md]]")
        handler.on_created_file(str(root / "source.md"))
        row = graph.run_one(
            "MATCH ()-[r:LINK]->(t:Node {path: 'target.md'}) RETURN r.strength AS s"
        )
        assert row["s"] == 0.0

        (root / "target.md").write_text("# Target")
        handler.on_created_file(str(root / "target.md"))
        row = graph.run_one(
            "MATCH ()-[r:LINK]->(t:Node {path: 'target.md'}) RETURN r.strength AS s"
        )
        assert row["s"] == 1.0

    def test_on_moved_updates_paths(self, graph, watched):
        root, handler = watched
        f = root / "old.md"
        f.write_text("# Old\nLink to [[other.md]]")
        (root / "other.md").write_text("# Other")
        handler.on_created_file(str(f))
        handler.on_created_file(str(root / "other.md"))

        new_path = root / "new.md"
        os.rename(f, new_path)
        handler.on_moved_file(str(f), str(new_path))

        assert get_node(graph, "old.md") is None
        assert get_node(graph, "new.md") is not None

    def test_ignores_memignore_patterns(self, graph, watched):
        root, handler = watched
        (root / ".memignore").write_text("drafts/\n")
        os.makedirs(root / "drafts")
        f = root / "drafts" / "wip.md"
        f.write_text("# WIP")
        handler.on_created_file(str(f))
        assert count_nodes(graph) == 0

    def test_on_directory_moved_updates_all_paths(self, graph, watched):
        root, handler = watched
        os.makedirs(root / "old_dir")
        (root / "old_dir" / "a.md").write_text("# A")
        (root / "old_dir" / "b.md").write_text("# B\nSee [[a.md]]")
        handler.on_created_file(str(root / "old_dir" / "a.md"))
        handler.on_created_file(str(root / "old_dir" / "b.md"))

        os.rename(root / "old_dir", root / "new_dir")
        handler.on_moved_directory(str(root / "old_dir"), str(root / "new_dir"))

        paths = [r["path"] for r in graph.run("MATCH (n:Node) RETURN n.path AS path")]
        assert "new_dir/a.md" in paths
        assert "new_dir/b.md" in paths
        assert "old_dir/a.md" not in paths
