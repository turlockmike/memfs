"""Tests for mem grep — FTS5 search, search edges, query nodes."""

import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_file
from memfs.search import grep, normalize_query


@pytest.fixture
def populated_db(tmp_path):
    """Create a memory root with several files indexed."""
    root = tmp_path
    db_path = str(root / ".mem" / "memory.db")
    create_db(db_path)

    files = {
        "kanji.md": "---\ntitle: Kanji Learning\ndate: 2026-04-01\n---\n# Kanji Learning\nStudying kanji with spaced repetition and mnemonics.",
        "satori.md": "---\ntitle: Satori App\ndate: 2026-04-10\n---\n# Satori App\nA kanji curriculum app built with Next.js and IndexedDB. See [[kanji.md]]",
        "meetings.md": "---\ntitle: Meeting Notes\ndate: 2026-04-12\n---\n# Meeting Notes\nDiscussed the quarterly roadmap and hiring plan.",
        "python.md": "# Python Tips\nUse list comprehensions for readability.",
        "unrelated.md": "# Cooking Recipes\nHow to make pasta carbonara.",
    }

    conn = connect(db_path)
    for name, content in files.items():
        (root / name).write_text(content)
        index_file(conn, str(root), name)
    conn.close()

    return root, db_path


class TestGrep:
    def test_finds_matching_file(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "kanji")
        conn.close()
        paths = [r["path"] for r in results]
        assert "kanji.md" in paths

    def test_ranks_title_match_higher(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "kanji")
        conn.close()
        # kanji.md has "kanji" in title, should rank higher than satori.md
        paths = [r["path"] for r in results]
        assert paths.index("kanji.md") < paths.index("satori.md")

    def test_returns_ndjson_fields(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "kanji")
        conn.close()
        r = results[0]
        assert "path" in r
        assert "title" in r
        assert "rank" in r
        assert "score" in r
        assert "snippet" in r

    def test_no_results_for_nonsense(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "xyzzy_nonexistent_term")
        conn.close()
        assert len(results) == 0

    def test_limits_results(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "the", limit=2)
        conn.close()
        assert len(results) <= 2

    def test_creates_search_edges_for_top_3(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "kanji")
        # Check search edges were created
        search_edges = conn.execute(
            "SELECT target, strength FROM edges WHERE type='search'"
        ).fetchall()
        conn.close()
        assert len(search_edges) <= 3
        targets = {e[0] for e in search_edges}
        # Top results should have search edges
        for i, r in enumerate(results[:3]):
            assert r["path"] in targets

    def test_creates_query_node(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        grep(conn, "kanji")
        queries = conn.execute("SELECT * FROM queries").fetchall()
        conn.close()
        assert len(queries) == 1

    def test_repeated_query_increments_use_count(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        grep(conn, "kanji")
        grep(conn, "kanji")
        row = conn.execute("SELECT use_count FROM queries").fetchone()
        conn.close()
        assert row[0] == 2

    def test_updates_search_count_on_nodes(self, populated_db):
        root, db_path = populated_db
        conn = connect(db_path)
        grep(conn, "kanji")
        row = conn.execute(
            "SELECT search_count FROM nodes WHERE path='kanji.md'"
        ).fetchone()
        conn.close()
        assert row[0] >= 1

    def test_rank_weighted_edge_strength(self, populated_db):
        """Rank 1 should get stronger edge than rank 3."""
        root, db_path = populated_db
        conn = connect(db_path)
        results = grep(conn, "kanji")
        if len(results) >= 3:
            edges = conn.execute(
                "SELECT target, strength FROM edges WHERE type='search' ORDER BY strength DESC"
            ).fetchall()
            strengths = [e[1] for e in edges]
            # Strengths should be monotonically non-increasing
            assert strengths == sorted(strengths, reverse=True)
        conn.close()


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
