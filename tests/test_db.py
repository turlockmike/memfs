"""Tests for Neo4j schema + basic CRUD.

Replaces the old SQLite-specific tests. Exercises constraints, fulltext index,
and node CRUD via graph.py.
"""

import pytest
from neo4j.exceptions import ConstraintError

from memfs.graph import (
    add_node, get_node, remove_node, get_all_nodes, get_meta, SCHEMA_VERSION,
)


class TestSchema:
    def test_schema_version_set(self, graph):
        assert get_meta(graph, "schema_version") == SCHEMA_VERSION

    def test_node_path_constraint_exists(self, graph):
        rows = graph.run("SHOW CONSTRAINTS YIELD name RETURN name")
        names = {r["name"] for r in rows}
        assert "node_path_unique" in names

    def test_fulltext_index_exists(self, graph):
        rows = graph.run("SHOW INDEXES YIELD name RETURN name")
        names = {r["name"] for r in rows}
        assert "node_content" in names


class TestNodeCrud:
    def test_add_node(self, graph):
        add_node(graph, "projects/satori.md", title="Satori",
                 content_hash="abc123", date_hint=None)
        node = get_node(graph, "projects/satori.md")
        assert node is not None
        assert node["title"] == "Satori"
        assert node["content_hash"] == "abc123"

    def test_add_duplicate_raises(self, graph):
        add_node(graph, "a.md", title="A", content_hash="abc", date_hint=None)
        with pytest.raises(Exception):  # Neo4j ConstraintError / ClientError
            add_node(graph, "a.md", title="A2", content_hash="def", date_hint=None)

    def test_remove_node(self, graph):
        add_node(graph, "a.md", title="A", content_hash="abc", date_hint=None)
        remove_node(graph, "a.md")
        assert get_node(graph, "a.md") is None

    def test_get_all_nodes(self, graph):
        add_node(graph, "a.md", title="A", content_hash="1", date_hint=None)
        add_node(graph, "b.md", title="B", content_hash="2", date_hint=None)
        nodes = get_all_nodes(graph)
        assert len(nodes) == 2
        paths = {n["path"] for n in nodes}
        assert paths == {"a.md", "b.md"}
