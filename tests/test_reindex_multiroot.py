"""Regression tests for multi-root reindex (2026-05-03 bug).

Bug shape (n=1 within the broader `verify-detector-premise` watch):
- ``~/.config/memfs/roots.json`` configures multiple roots (alfred-state,
  alfred-home). Each gets its own ``root_id`` tag on Node properties.
- ``cmd_reindex`` calls ``reindex(graph, mem_home)`` ONCE with default
  ``root_id="default"``.
- ``reindex`` calls ``clear_data`` (wipes ALL Node nodes regardless of
  root_id) then ``index_directory(graph, mem_home, root_id="default")``
  (only re-walks ONE filesystem and tags new nodes "default").
- Result: configured-root scopes (alfred-state, alfred-home) end up empty
  in the graph. The watcher daemon repopulates them incrementally but
  doesn't catch all deletes (symlink target removals, batch ops while
  watcher off). Ghost nodes accumulate; ``memfs reindex`` is supposed to
  sweep them but is the wrong tool because it's multi-root-unaware.

Empirical observations (alfred substrate, 2026-05-03):
- drift_findings escalated 363 → 640 → 923 → 1702 across reindex runs.
- ``memfs ls`` in alfred-state scope reported ghost files
  (resources/architecture.md, resources/backlog.md, etc.) that don't
  exist on disk.

Contract pinned by these tests:
1. ``reindex(graph, mem_home, root_id=R)`` purges ghost nodes WITHIN
   root R but does NOT wipe nodes from OTHER roots.
2. ``reindex_all_roots(graph, scopes)`` (new helper) walks every
   configured root and leaves each root's graph in sync with its
   filesystem.
"""

import json
import os

import pytest

from memfs.graph import count_nodes
from memfs.indexer import index_file


class TestReindexPreservesOtherRoots:
    """Contract: reindexing root A must not destroy root B's nodes."""

    def test_reindex_root_a_preserves_root_b_nodes(self, graph, tmp_path):
        """REGRESSION 2026-05-03: clear_data wiped all roots, leaving B
        empty until the watcher slowly repopulates it. Reindex must be
        scoped to the targeted root."""
        root_a = tmp_path / "rootA"
        root_b = tmp_path / "rootB"
        root_a.mkdir()
        root_b.mkdir()

        (root_a / "alpha.md").write_text("# Alpha (in rootA)")
        (root_b / "gamma.md").write_text("# Gamma (in rootB)")

        # Watcher daemon indexes each file under its configured root_id.
        index_file(graph, str(root_a), "alpha.md", root_id="rootA")
        index_file(graph, str(root_b), "gamma.md", root_id="rootB")

        assert count_nodes(graph) == 2

        # User runs `memfs reindex` while sitting in rootA. This should
        # rebuild rootA's graph state but leave rootB alone.
        from memfs.indexer import reindex
        reindex(graph, str(root_a), root_id="rootA")

        rootA_paths = [r["path"] for r in graph.run(
            "MATCH (n:Node) WHERE n.root_id = $rid RETURN n.path AS path",
            rid="rootA",
        )]
        rootB_paths = [r["path"] for r in graph.run(
            "MATCH (n:Node) WHERE n.root_id = $rid RETURN n.path AS path",
            rid="rootB",
        )]

        assert "alpha.md" in rootA_paths, (
            f"rootA was not re-indexed correctly. rootA paths: {rootA_paths}"
        )
        assert "gamma.md" in rootB_paths, (
            f"rootB nodes WIPED by reindex of rootA. "
            f"This is the multi-root-unaware bug. "
            f"rootB paths: {rootB_paths}"
        )


class TestReindexPurgesGhostsInTargetRoot:
    """Contract: reindex of root R removes ghost nodes (graph nodes whose
    files no longer exist on disk) from root R."""

    def test_reindex_purges_deleted_file_node(self, graph, tmp_path):
        root_a = tmp_path / "rootA"
        root_a.mkdir()
        (root_a / "alpha.md").write_text("# Alpha")
        (root_a / "beta.md").write_text("# Beta — will be deleted")

        index_file(graph, str(root_a), "alpha.md", root_id="rootA")
        index_file(graph, str(root_a), "beta.md", root_id="rootA")

        # Simulate the watcher missing a delete (symlink-target race,
        # batch op while watcher off, etc.)
        os.unlink(root_a / "beta.md")

        from memfs.indexer import reindex
        reindex(graph, str(root_a), root_id="rootA")

        rootA_paths = [r["path"] for r in graph.run(
            "MATCH (n:Node) WHERE n.root_id = $rid RETURN n.path AS path",
            rid="rootA",
        )]
        assert "alpha.md" in rootA_paths
        assert "beta.md" not in rootA_paths, (
            f"GHOST NODE: beta.md still in graph after reindex even though "
            f"file was deleted from disk. rootA paths: {rootA_paths}"
        )


class TestReindexAllRoots:
    """Contract: reindex_all_roots walks every configured root and leaves
    each one's graph in sync with its filesystem."""

    def test_reindex_all_roots_round_trip(self, graph, tmp_path):
        """End-to-end: configure two roots, plant ghost nodes in BOTH,
        run reindex_all_roots, confirm both roots are now clean."""
        root_a = tmp_path / "rootA"
        root_b = tmp_path / "rootB"
        root_a.mkdir()
        root_b.mkdir()

        (root_a / "alpha.md").write_text("# Alpha")
        (root_a / "beta.md").write_text("# Beta — to delete")
        (root_b / "gamma.md").write_text("# Gamma")
        (root_b / "delta.md").write_text("# Delta — to delete")

        # Watcher indexed everything correctly.
        index_file(graph, str(root_a), "alpha.md", root_id="rootA")
        index_file(graph, str(root_a), "beta.md", root_id="rootA")
        index_file(graph, str(root_b), "gamma.md", root_id="rootB")
        index_file(graph, str(root_b), "delta.md", root_id="rootB")

        # Watcher missed both deletes.
        os.unlink(root_a / "beta.md")
        os.unlink(root_b / "delta.md")

        # NEW helper added by the patch — multi-root-aware reindex.
        from memfs.cli import reindex_all_roots
        reindex_all_roots(graph, [
            ("rootA", str(root_a)),
            ("rootB", str(root_b)),
        ])

        rootA_paths = sorted(r["path"] for r in graph.run(
            "MATCH (n:Node) WHERE n.root_id = $rid RETURN n.path AS path",
            rid="rootA",
        ))
        rootB_paths = sorted(r["path"] for r in graph.run(
            "MATCH (n:Node) WHERE n.root_id = $rid RETURN n.path AS path",
            rid="rootB",
        ))

        assert rootA_paths == ["alpha.md"], (
            f"rootA scope not in sync with FS. paths={rootA_paths} "
            f"(expected exactly ['alpha.md'])"
        )
        assert rootB_paths == ["gamma.md"], (
            f"rootB scope not in sync with FS. paths={rootB_paths} "
            f"(expected exactly ['gamma.md'])"
        )


class TestClearRootData:
    """Contract: clear_root_data(graph, root_id) deletes ONLY that root's
    Node/Query/edges. The indexer.reindex docstring claims this primitive
    was added 2026-05-01 but it was never implemented — adding it now."""

    def test_clears_only_target_root(self, graph, tmp_path):
        from memfs.graph import clear_root_data

        root_a = tmp_path / "rootA"
        root_b = tmp_path / "rootB"
        root_a.mkdir()
        root_b.mkdir()
        (root_a / "alpha.md").write_text("# Alpha")
        (root_b / "gamma.md").write_text("# Gamma")

        index_file(graph, str(root_a), "alpha.md", root_id="rootA")
        index_file(graph, str(root_b), "gamma.md", root_id="rootB")

        clear_root_data(graph, root_id="rootA")

        remaining = [r["path"] for r in graph.run(
            "MATCH (n:Node) RETURN n.path AS path"
        )]
        assert "alpha.md" not in remaining, (
            f"clear_root_data didn't purge rootA. remaining: {remaining}"
        )
        assert "gamma.md" in remaining, (
            f"clear_root_data wiped rootB too. remaining: {remaining}"
        )
