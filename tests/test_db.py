"""Tests for SQLite schema and database operations."""

import sqlite3
import pytest
from memfs.db import create_db, connect, add_node, get_node, remove_node, get_all_nodes


@pytest.fixture
def db_path(tmp_path):
    """Create a fresh database in a temp directory."""
    path = tmp_path / ".mem" / "memory.db"
    create_db(str(path))
    return str(path)


class TestCreateDb:
    def test_creates_file(self, tmp_path):
        path = tmp_path / ".mem" / "memory.db"
        create_db(str(path))
        assert path.exists()

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / ".mem" / "memory.db"
        create_db(str(path))
        assert (tmp_path / ".mem").is_dir()

    def test_has_wal_mode(self, db_path):
        conn = connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_has_nodes_table(self, db_path):
        conn = connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "nodes" in tables

    def test_has_edges_table(self, db_path):
        conn = connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "edges" in tables

    def test_has_queries_table(self, db_path):
        conn = connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "queries" in tables

    def test_has_fts_table(self, db_path):
        conn = connect(db_path)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "fts" in tables

    def test_has_meta_with_schema_version(self, db_path):
        conn = connect(db_path)
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        conn.close()
        assert version == "1"


class TestNodeCrud:
    def test_add_node(self, db_path):
        conn = connect(db_path)
        add_node(conn, "projects/satori.md", title="Satori",
                 content_hash="abc123", date_hint=None)
        node = get_node(conn, "projects/satori.md")
        conn.close()
        assert node is not None
        assert node["title"] == "Satori"
        assert node["content_hash"] == "abc123"

    def test_add_duplicate_raises(self, db_path):
        conn = connect(db_path)
        add_node(conn, "a.md", title="A", content_hash="abc", date_hint=None)
        with pytest.raises(sqlite3.IntegrityError):
            add_node(conn, "a.md", title="A2", content_hash="def", date_hint=None)
        conn.close()

    def test_remove_node(self, db_path):
        conn = connect(db_path)
        add_node(conn, "a.md", title="A", content_hash="abc", date_hint=None)
        remove_node(conn, "a.md")
        assert get_node(conn, "a.md") is None
        conn.close()

    def test_get_all_nodes(self, db_path):
        conn = connect(db_path)
        add_node(conn, "a.md", title="A", content_hash="1", date_hint=None)
        add_node(conn, "b.md", title="B", content_hash="2", date_hint=None)
        nodes = get_all_nodes(conn)
        conn.close()
        assert len(nodes) == 2
        paths = {n["path"] for n in nodes}
        assert paths == {"a.md", "b.md"}
