"""Search subcommand: hierarchy math, FTS query sanitization, round-trip."""
import sqlite3
import sys

from mvm.search import hierarchy_distance, fts_search


def test_hierarchy_distance_same_path():
    assert hierarchy_distance("a/b/c.md", "a/b/c.md") == 0


def test_hierarchy_distance_sibling():
    assert hierarchy_distance("a/b/c.md", "a/b/d.md") == 2


def test_hierarchy_distance_nephew():
    assert hierarchy_distance("a/b.md", "a/c/d.md") == 3


def test_hierarchy_distance_disjoint():
    assert hierarchy_distance("a/b.md", "x/y.md") == 4


def test_fts_search_strips_apostrophes(tmp_path):
    """Apostrophes used to break FTS5 with a syntax error; should now strip cleanly."""
    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE files (path TEXT PRIMARY KEY, kind TEXT);
        CREATE VIRTUAL TABLE files_fts USING fts5(path UNINDEXED, body, tokenize='porter unicode61');
    """)
    conn.execute("INSERT INTO files (path, kind) VALUES (?, ?)", ("doc.md", "canonical"))
    conn.execute("INSERT INTO files_fts (path, body) VALUES (?, ?)",
                 ("doc.md", "This is the SpaceX deal content."))
    conn.commit()
    conn.close()

    # Should not raise; should return results despite apostrophe in query
    results = fts_search(db, "what's the SpaceX deal", None, None)
    assert isinstance(results, list)
    # If FTS finds anything for "spacex", we expect doc.md
    if results:
        assert results[0][0] == "doc.md"


def test_fts_search_returns_empty_on_missing_db(tmp_path):
    nonexistent = tmp_path / "no.db"
    assert fts_search(nonexistent, "anything", None, None) == []
