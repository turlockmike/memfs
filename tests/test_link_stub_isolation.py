"""Regression tests for ghost-node bug from upsert_link_edge stub creation.

Bug shape (n=2 within the broader `verify-detector-premise` watch):
- ``upsert_link_edge(graph, src, tgt)`` does ``MERGE (t:Node {root_id, path})``
  for the target. When the target file does not exist on disk, this creates
  a placeholder :Node entry — a stub. Stubs lack content_hash/title/layer
  (they were never parsed; they're synthesized from a wikilink string).
- The ``index_file`` flow ALWAYS calls ``upsert_link_edge`` for every
  wikilink in the file. Files with broken wikilinks therefore produce one
  stub Node per broken target.
- Net effect: ``count_nodes(graph)`` counts both real files AND stubs. After
  a clean ``reindex`` (which is ``clear_root_data`` + ``index_directory``),
  the count exceeds the on-disk file count by exactly the number of unique
  broken-link targets in the corpus.
- Sibling damage: ``_walk_indexed_dirs`` yields directories from stub paths.
  Stubs with weird paths (``~/.config/auditor/...md#frag.md``,
  ``../../areas/USER.md``, absolute ``/home/mike/...py.md``) generate bogus
  "INDEX REFERENCES MISSING FILE" drift findings against fictional
  directories. Empirically: alfred-state had 20 stubs with absolute
  ``.py.md`` / ``.jsonl.md`` paths; alfred-home had 279 ghosts dominated
  by relative-path stubs from POE2 wikilinks.

Empirical observations (alfred substrate, 2026-05-03 14:30 UTC):
- alfred-home: 4848 graph nodes vs 4570 on-disk files. +278 ghosts.
- alfred-state: 20 graph nodes vs 981 on-disk files (skewed; reindex
  hadn't repopulated, but ALL 20 had bizarre stub-paths confirming the
  pattern).
- All ghosts had ``layer IS NULL`` (real nodes set layer >= 1 always).

Contract pinned by these tests:
1. After ``reindex(graph, mem_home, root_id)``, the count of real nodes
   for ``root_id`` exactly equals the count of on-disk indexable files.
   Broken wikilinks must NOT contribute to the real-node count.
2. ``count_nodes(graph)`` excludes stubs by default. Real-node semantics
   = "has a corresponding file on disk that was successfully parsed".
3. ``upgrade_broken_links`` still works — when a file matching a previously
   stubbed target gets indexed, inbound strength=0 LINK edges flip to 1.0.
4. ``clear_root_data(graph, root_id)`` purges both real nodes AND stubs
   for the target root.
"""

import os
import pytest

from memfs.graph import (
    clear_root_data,
    count_nodes,
    upsert_link_edge,
    upsert_node,
    upgrade_broken_links,
)
from memfs.indexer import index_file, reindex


class TestReindexExcludesLinkStubsFromNodeCount:
    """Contract: after reindex, real-node count == on-disk file count.

    Stubs from broken wikilinks must not inflate the count.
    """

    def test_one_file_with_one_broken_link_is_one_real_node(self, graph, tmp_path):
        """REGRESSION 2026-05-03: alfred substrate had ~275 ghosts after
        reindex because every broken wikilink synthesized a :Node stub."""
        root = tmp_path / "rootR"
        root.mkdir()
        (root / "alpha.md").write_text(
            "# Alpha\n\nReferences [[nonexistent-target]] which does not exist.\n"
        )

        n_indexed = reindex(graph, str(root), root_id="rootR")
        assert n_indexed == 1, "exactly one file walked"

        real_count = count_nodes(graph, root_id="rootR")
        assert real_count == 1, (
            f"expected 1 real node (alpha.md); got {real_count}. "
            f"Extra is the broken-link stub for [[nonexistent-target]]."
        )

    def test_many_broken_links_do_not_inflate_count(self, graph, tmp_path):
        """One file with N broken links → 1 real node, NOT 1 + N."""
        root = tmp_path / "rootR"
        root.mkdir()
        body = "# Beta\n\n" + "\n".join(
            f"- See [[ghost-{i}]]" for i in range(20)
        )
        (root / "beta.md").write_text(body)

        reindex(graph, str(root), root_id="rootR")

        real_count = count_nodes(graph, root_id="rootR")
        assert real_count == 1, (
            f"expected 1 real node, got {real_count}. "
            f"20 broken links inflated the count."
        )

    def test_round_trip_real_node_count_equals_file_count(self, graph, tmp_path):
        """End-to-end: 5 real files with mixed valid + broken links → exactly 5 real nodes."""
        root = tmp_path / "rootR"
        root.mkdir()
        # 5 files that mutually link some valid + broken targets.
        (root / "a.md").write_text("# A\n[[b]] real, [[ghost1]] broken")
        (root / "b.md").write_text("# B\n[[c]] real, [[ghost2]] broken, [[ghost3]] broken")
        (root / "c.md").write_text("# C\n[[a]] real")
        (root / "d.md").write_text("# D\n[[e]] real, [[ghost4]] broken")
        (root / "e.md").write_text("# E\n(no links)")

        reindex(graph, str(root), root_id="rootR")

        real_count = count_nodes(graph, root_id="rootR")
        assert real_count == 5, (
            f"expected 5 real nodes (a,b,c,d,e), got {real_count}. "
            f"Broken-link stubs contaminated the count."
        )


class TestUpgradeBrokenLinkStillWorks:
    """Contract: when a file matching a previously-stubbed target gets indexed,
    the inbound LINK edge gets upgraded from strength=0 to strength=1.0.

    This must keep working post-fix — broken-link tracking shouldn't break.
    """

    def test_upgrade_after_stub_promoted_to_real(self, graph, tmp_path):
        root = tmp_path / "rootR"
        root.mkdir()
        # First: source file with broken link to "target".
        (root / "src.md").write_text("# Src\n[[target]] not yet here")

        index_file(graph, str(root), "src.md", root_id="rootR")

        # Inbound edge to nonexistent 'target' should exist with strength=0.
        edge_strength = graph.run_scalar(
            "MATCH (:Node {root_id:$rid, path:'src.md'})-[r:LINK]->"
            "({root_id:$rid, path:'target.md'}) "
            "RETURN r.strength",
            rid="rootR",
        )
        assert edge_strength == 0.0, (
            f"broken link should have strength=0; got {edge_strength}"
        )

        # Now create target.md and index it.
        (root / "target.md").write_text("# Target\nnow exists")
        index_file(graph, str(root), "target.md", root_id="rootR")
        upgrade_broken_links(graph, "target.md", root_id="rootR")

        # Edge to target should now be strength=1.0
        edge_strength = graph.run_scalar(
            "MATCH (:Node {root_id:$rid, path:'src.md'})-[r:LINK]->"
            "(:Node {root_id:$rid, path:'target.md'}) "
            "RETURN r.strength",
            rid="rootR",
        )
        assert edge_strength == 1.0, (
            f"after target.md indexed and upgrade_broken_links called, "
            f"strength should be 1.0; got {edge_strength}"
        )

        # And target.md should be a real :Node (not a stub).
        real_count = count_nodes(graph, root_id="rootR")
        assert real_count == 2, (
            f"expected 2 real nodes (src + target); got {real_count}"
        )


class TestClearRootDataPurgesStubs:
    """Contract: clear_root_data deletes both real nodes AND stubs for the
    target root. Otherwise stubs accumulate forever across reindex cycles."""

    def test_clears_stubs(self, graph, tmp_path):
        root = tmp_path / "rootR"
        root.mkdir()
        (root / "src.md").write_text("# Src\n[[ghost1]] [[ghost2]] [[ghost3]]")

        index_file(graph, str(root), "src.md", root_id="rootR")

        # Confirm stubs exist (they're somewhere, by whatever label).
        # The test is: after clear_root_data, NOTHING for this root remains.
        clear_root_data(graph, root_id="rootR")

        any_remaining = graph.run_scalar(
            "MATCH (n) WHERE n.root_id = $rid RETURN count(n)",
            rid="rootR",
        )
        assert any_remaining == 0, (
            f"clear_root_data left {any_remaining} nodes (real or stub) "
            f"under root_id=rootR. Stubs leaked across reindex."
        )
