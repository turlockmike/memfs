"""Tests for memfs grep — full-text search via Neo4j."""

import pytest

from memfs.indexer import index_file
from memfs.search import grep, normalize_query
from memfs.graph import count_queries


@pytest.fixture
def populated_graph(graph, tmp_path):
    files = {
        "kanji.md": "---\ntitle: Kanji Learning\ndate: 2026-04-01\n---\n# Kanji Learning\nStudying kanji with spaced repetition and mnemonics.",
        "satori.md": "---\ntitle: Satori App\ndate: 2026-04-10\n---\n# Satori App\nA kanji curriculum app built with Next.js and IndexedDB. See [[kanji.md]]",
        "meetings.md": "---\ntitle: Meeting Notes\ndate: 2026-04-12\n---\n# Meeting Notes\nDiscussed the quarterly roadmap and hiring plan.",
        "python.md": "# Python Tips\nUse list comprehensions for readability.",
        "unrelated.md": "# Cooking Recipes\nHow to make pasta carbonara.",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
        index_file(graph, str(tmp_path), name)
    return graph, tmp_path


class TestGrep:
    def test_finds_matching_file(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji")
        paths = [r["path"] for r in results]
        assert "kanji.md" in paths

    def test_ranks_title_match_higher(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji")
        paths = [r["path"] for r in results]
        # kanji.md has "kanji" in title, should rank higher than satori.md
        assert paths.index("kanji.md") < paths.index("satori.md")

    def test_returns_ndjson_fields(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji")
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "rank" in r
        assert "score" in r
        assert "snippet" in r

    def test_no_results_for_nonsense(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "xyzzy_nonexistent_term")
        assert len(results) == 0

    def test_limits_results(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji satori meetings python cooking", limit=2)
        assert len(results) <= 2

    def test_creates_search_edges_for_top_3(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji")
        rows = graph.run(
            "MATCH (:Query)-[r:SEARCH]->(n:Node) "
            "RETURN n.path AS path, r.strength AS strength"
        )
        edges = list(rows)
        assert len(edges) <= 3
        targets = {e["path"] for e in edges}
        for r in results[:3]:
            assert r["path"] in targets

    def test_creates_query_node(self, populated_graph):
        graph, _ = populated_graph
        grep(graph, "kanji")
        assert count_queries(graph) == 1

    def test_repeated_query_increments_use_count(self, populated_graph):
        graph, _ = populated_graph
        grep(graph, "kanji")
        grep(graph, "kanji")
        count = graph.run_scalar("MATCH (q:Query) RETURN q.use_count")
        assert count == 2

    def test_updates_search_count_on_nodes(self, populated_graph):
        graph, _ = populated_graph
        grep(graph, "kanji")
        count = graph.run_scalar(
            "MATCH (n:Node {path: 'kanji.md'}) RETURN n.search_count"
        )
        assert count >= 1

    def test_rank_weighted_edge_strength(self, populated_graph):
        graph, _ = populated_graph
        results = grep(graph, "kanji")
        if len(results) >= 3:
            rows = graph.run(
                "MATCH (:Query)-[r:SEARCH]->(n:Node) "
                "RETURN r.strength AS strength ORDER BY r.strength DESC"
            )
            strengths = [r["strength"] for r in rows]
            assert strengths == sorted(strengths, reverse=True)


class TestNormalizeQuery:
    def test_case_insensitive(self):
        assert normalize_query("KANJI") == normalize_query("kanji")

    def test_strips_punctuation(self):
        assert normalize_query("kanji!") == normalize_query("kanji")

    def test_order_independent(self):
        assert normalize_query("kanji satori") == normalize_query("satori kanji")

    def test_consistent_hash(self):
        h1 = normalize_query("hello world")
        h2 = normalize_query("hello world")
        assert h1 == h2

    def test_different_queries_different_hash(self):
        assert normalize_query("kanji") != normalize_query("pasta")
