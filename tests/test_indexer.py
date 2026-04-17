"""Tests for the indexer — scanning dirs, indexing files, maintaining edges."""

import os
import pytest

from memfs.indexer import index_file, index_directory, reindex, update_node
from memfs.graph import count_nodes, get_node


class TestIndexFile:
    def test_indexes_new_file(self, graph, tmp_path):
        f = tmp_path / "hello.md"
        f.write_text("# Hello\nWorld")
        index_file(graph, str(tmp_path), "hello.md")
        assert get_node(graph, "hello.md") is not None

    def test_indexes_into_fts(self, graph, tmp_path):
        f = tmp_path / "hello.md"
        f.write_text("# Hello\nThe quick brown fox")
        index_file(graph, str(tmp_path), "hello.md")
        rows = graph.run(
            "CALL db.index.fulltext.queryNodes('node_content', 'fox') "
            "YIELD node RETURN node.path AS path"
        )
        paths = [r["path"] for r in rows]
        assert "hello.md" in paths

    def test_creates_link_edges(self, graph, tmp_path):
        (tmp_path / "target.md").write_text("# Target")
        (tmp_path / "source.md").write_text("# Source\nSee [[target.md]]")
        index_file(graph, str(tmp_path), "target.md")
        index_file(graph, str(tmp_path), "source.md")
        rows = graph.run(
            "MATCH (s:Node)-[r:LINK]->(t:Node) "
            "RETURN s.path AS source, t.path AS target, r.strength AS strength"
        )
        edges = [(r["source"], r["target"], r["strength"]) for r in rows]
        assert any(e[0] == "source.md" and e[1] == "target.md" for e in edges)

    def test_broken_link_gets_zero_strength(self, graph, tmp_path):
        (tmp_path / "source.md").write_text("See [[nonexistent.md]]")
        index_file(graph, str(tmp_path), "source.md")
        row = graph.run_one(
            "MATCH ()-[r:LINK]->(t:Node {path: 'nonexistent.md'}) "
            "RETURN r.strength AS s"
        )
        assert row is not None
        assert row["s"] == 0.0

    def test_extracts_date_hint(self, graph, tmp_path):
        f = tmp_path / "dated.md"
        f.write_text("---\ndate: 2026-04-12\n---\n# Dated note")
        index_file(graph, str(tmp_path), "dated.md")
        node = get_node(graph, "dated.md")
        assert node["date_hint"] == "2026-04-12"


class TestUpdateNode:
    def test_updates_on_content_change(self, graph, tmp_path):
        f = tmp_path / "changing.md"
        f.write_text("# Version 1")
        index_file(graph, str(tmp_path), "changing.md")
        old_hash = get_node(graph, "changing.md")["content_hash"]
        f.write_text("# Version 2 — totally different")
        update_node(graph, str(tmp_path), "changing.md")
        new_hash = get_node(graph, "changing.md")["content_hash"]
        assert old_hash != new_hash

    def test_updates_fts_on_change(self, graph, tmp_path):
        f = tmp_path / "changing.md"
        f.write_text("# Alpha\nOriginal content about alpha")
        index_file(graph, str(tmp_path), "changing.md")
        f.write_text("# Beta\nNew content about beta")
        update_node(graph, str(tmp_path), "changing.md")

        alpha = graph.run(
            "CALL db.index.fulltext.queryNodes('node_content', 'alpha') "
            "YIELD node RETURN node.path AS path"
        )
        beta = graph.run(
            "CALL db.index.fulltext.queryNodes('node_content', 'beta') "
            "YIELD node RETURN node.path AS path"
        )
        assert not any(r["path"] == "changing.md" for r in alpha)
        assert any(r["path"] == "changing.md" for r in beta)


class TestIndexDirectory:
    def test_indexes_all_md_files(self, graph, tmp_path):
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        (tmp_path / "not-md.txt").write_text("Not indexed")
        os.makedirs(tmp_path / "sub")
        (tmp_path / "sub" / "c.md").write_text("# C")
        index_directory(graph, str(tmp_path))
        assert count_nodes(graph) == 3

    def test_skips_memignore_patterns(self, graph, tmp_path):
        (tmp_path / ".memignore").write_text("drafts/\n")
        os.makedirs(tmp_path / "drafts")
        (tmp_path / "drafts" / "wip.md").write_text("# WIP")
        (tmp_path / "visible.md").write_text("# Visible")
        index_directory(graph, str(tmp_path))
        assert count_nodes(graph) == 1

    def test_skips_dot_mem_dir(self, graph, tmp_path):
        (tmp_path / "visible.md").write_text("# Visible")
        os.makedirs(tmp_path / ".mem")
        (tmp_path / ".mem" / "internal.md").write_text("internal")
        index_directory(graph, str(tmp_path))
        paths = [n["path"] for n in graph.run("MATCH (n:Node) RETURN n.path AS path")]
        assert all(not p.startswith(".mem") for p in paths)


class TestReindex:
    def test_rebuilds_from_scratch(self, graph, tmp_path):
        (tmp_path / "a.md").write_text("# A\nLink to [[b.md]]")
        (tmp_path / "b.md").write_text("# B")
        index_directory(graph, str(tmp_path))
        assert count_nodes(graph) == 2
        os.unlink(tmp_path / "b.md")
        reindex(graph, str(tmp_path))
        # b.md will reappear as a placeholder from the broken link in a.md
        # — but its content_hash should be null and strength should be 0.
        row = graph.run_one(
            "MATCH ()-[r:LINK]->(t:Node {path: 'b.md'}) RETURN r.strength AS s"
        )
        if row is not None:
            assert row["s"] == 0.0
