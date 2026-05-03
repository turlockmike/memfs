"""Tests for the filesystem watcher (event handler level, no daemon fork)."""

import json
import os
import pytest

from memfs.watcher import MemfsEventHandler, _resolve_watch_roots
from memfs.graph import get_node, count_nodes


# --- Pure-logic tests for root resolution (no Neo4j needed) ---------------
# These tests don't use the `graph` fixture and so are not gated by the
# production-graph wipe guard.

class TestResolveWatchRoots:
    """Regression tests for the 2026-05-03 ghost-node bug.

    Symptom: daemon was creating Node entries with root_id='alfred' (basename
    of $MEM_HOME=/home/mike/.local/state/alfred) instead of 'alfred-state'
    (the configured id for that path in ~/.config/memfs/roots.json). Reindex
    couldn't clean them up because clear_root_data('alfred-state') doesn't
    touch nodes tagged 'alfred'.

    Fix: when MEM_HOME matches a configured root's path, prefer the
    configured id over basename derivation.
    """

    def _write_roots(self, tmp_path, roots_list):
        cfg_dir = tmp_path / "config" / "memfs"
        cfg_dir.mkdir(parents=True)
        cfg = cfg_dir / "roots.json"
        cfg.write_text(json.dumps({"roots": roots_list}))
        return cfg

    @pytest.fixture
    def configured_roots(self, tmp_path, monkeypatch):
        """Writes a roots.json with two roots and monkeypatches CONFIG_PATH."""
        state_dir = tmp_path / "state" / "alfred"
        state_dir.mkdir(parents=True)
        home_dir = tmp_path / "home"
        home_dir.mkdir()
        cfg = self._write_roots(tmp_path, [
            {"id": "alfred-state", "path": str(state_dir)},
            {"id": "alfred-home", "path": str(home_dir)},
        ])
        from memfs import roots as roots_mod
        monkeypatch.setattr(roots_mod, "CONFIG_PATH", cfg)
        return state_dir, home_dir

    def test_mem_home_matching_configured_root_uses_configured_id(
        self, configured_roots, monkeypatch,
    ):
        """The bug: MEM_HOME=/path/alfred → id='alfred' (basename) instead
        of 'alfred-state' (configured). Resolver MUST prefer configured id.

        CRITICAL — set MEM_HOME in the env. Earlier draft of this test
        deleted MEM_HOME and silently bypassed the bug path because
        load_roots() short-circuits on MEM_HOME (returns basename root
        without ever reading CONFIG_PATH). Caught by end-to-end
        verification 2026-05-03. Watch: 'test-mocks-away-the-bug' n=1.
        """
        state_dir, _ = configured_roots
        monkeypatch.setenv("MEM_HOME", str(state_dir))  # exercise the bug path
        result = _resolve_watch_roots(str(state_dir), str(state_dir))
        assert len(result) == 1
        assert result[0].id == "alfred-state"  # NOT 'alfred' (basename)
        assert os.path.abspath(result[0].path) == os.path.abspath(str(state_dir))

    def test_mem_home_unset_returns_all_configured_roots(
        self, configured_roots, monkeypatch,
    ):
        """No MEM_HOME → multi-root mode (all configured roots)."""
        monkeypatch.delenv("MEM_HOME", raising=False)
        result = _resolve_watch_roots(None, "/unused")
        ids = sorted(r.id for r in result)
        assert ids == ["alfred-home", "alfred-state"]

    def test_mem_home_unmatched_falls_back_to_basename(
        self, configured_roots, tmp_path, monkeypatch,
    ):
        """Truly ad-hoc MEM_HOME (not in roots.json) → basename-derived id.

        Set MEM_HOME to exercise the env-set code path (not the unset one).
        """
        adhoc = tmp_path / "scratch-area"
        adhoc.mkdir()
        monkeypatch.setenv("MEM_HOME", str(adhoc))
        result = _resolve_watch_roots(str(adhoc), str(adhoc))
        assert len(result) == 1
        assert result[0].id == "scratch-area"  # basename-derived

    def test_mem_home_with_trailing_slash_still_matches(
        self, configured_roots, monkeypatch,
    ):
        """Path normalization: trailing slash shouldn't defeat the match."""
        state_dir, _ = configured_roots
        monkeypatch.setenv("MEM_HOME", str(state_dir) + "/")
        result = _resolve_watch_roots(str(state_dir) + "/", str(state_dir) + "/")
        assert result[0].id == "alfred-state"


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
