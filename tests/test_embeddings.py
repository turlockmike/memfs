"""Tests for vector embeddings and hybrid search."""

import struct
import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_file
from memfs.embeddings import embed_file, embed_query, cosine_search, embed_all
from memfs.search import grep


@pytest.fixture
def db_with_files(tmp_path):
    """Create a memory root with files for embedding tests."""
    root = tmp_path
    db_path = str(root / ".mem" / "memory.db")
    create_db(db_path)

    files = {
        "scaling.md": "# Performance Bottlenecks\nThe ingest pipeline is slow due to database lock contention.",
        "kanji.md": "# Kanji Study\nUsing spaced repetition to memorize Japanese characters.",
        "cooking.md": "# Italian Recipes\nPasta carbonara with guanciale and pecorino romano.",
    }

    conn = connect(db_path)
    for name, content in files.items():
        (root / name).write_text(content)
        index_file(conn, str(root), name)
    conn.close()
    return root, db_path


class TestEmbedFile:
    def test_stores_embedding(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_file(conn, str(root), "scaling.md")
        row = conn.execute("SELECT vector, model FROM embeddings WHERE path='scaling.md'").fetchone()
        conn.close()
        assert row is not None
        assert row[1] == "all-MiniLM-L6-v2"
        # 384 dims * 4 bytes = 1536 bytes
        assert len(row[0]) == 384 * 4

    def test_updates_embedded_at(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_file(conn, str(root), "scaling.md")
        row = conn.execute("SELECT embedded_at FROM nodes WHERE path='scaling.md'").fetchone()
        conn.close()
        assert row[0] is not None


class TestEmbedAll:
    def test_embeds_all_files(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        count = embed_all(conn, str(root))
        embedded = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        conn.close()
        assert count == 3
        assert embedded == 3

    def test_skips_already_embedded(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_all(conn, str(root))
        count2 = embed_all(conn, str(root))
        conn.close()
        assert count2 == 0  # All already embedded


class TestCosineSearch:
    def test_semantic_match(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_all(conn, str(root))
        # "scaling issues" should find "Performance Bottlenecks" semantically
        results = cosine_search(conn, "scaling issues in the system", top_k=3)
        conn.close()
        paths = [r[0] for r in results]
        assert "scaling.md" in paths

    def test_returns_scores(self, db_with_files):
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_all(conn, str(root))
        results = cosine_search(conn, "Japanese language learning", top_k=3)
        conn.close()
        assert len(results) > 0
        path, score = results[0]
        assert 0.0 <= score <= 1.0


class TestHybridSearch:
    def test_rrf_fusion_improves_semantic_queries(self, db_with_files):
        """Queries with no keyword overlap should still find results via vectors."""
        root, db_path = db_with_files
        conn = connect(db_path)
        embed_all(conn, str(root))
        # "scaling issues" has NO keyword overlap with "Performance Bottlenecks"
        # FTS5 alone would miss this. With vectors, it should find it.
        results = grep(conn, "scaling issues", use_vectors=True)
        conn.close()
        paths = [r["path"] for r in results]
        assert "scaling.md" in paths
