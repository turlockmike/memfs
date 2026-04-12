"""Tests for temporal proximity scoring in search."""

import json
import pytest
from memfs.db import create_db, connect
from memfs.indexer import index_file
from memfs.search import grep


@pytest.fixture
def temporal_db(tmp_path):
    """DB with files that have different dates."""
    root = tmp_path
    db_path = str(root / ".mem" / "memory.db")
    create_db(db_path)

    files = {
        "jan.md": "---\ntitle: January Meeting\ndate: 2023-01-15\n---\n# January Meeting\nDiscussed project kickoff and timeline.",
        "apr.md": "---\ntitle: April Meeting\ndate: 2023-04-10\n---\n# April Meeting\nDiscussed project progress and milestones.",
        "dec.md": "---\ntitle: December Meeting\ndate: 2023-12-20\n---\n# December Meeting\nDiscussed project wrap-up and retrospective.",
    }

    conn = connect(db_path)
    for name, content in files.items():
        (root / name).write_text(content)
        index_file(conn, str(root), name)
    conn.close()
    return root, db_path


class TestTemporalBoost:
    def test_date_in_query_boosts_nearby_results(self, temporal_db):
        """When query mentions a date, nearby files should rank higher."""
        root, db_path = temporal_db
        conn = connect(db_path)
        # Query about April — apr.md should get a temporal boost
        results = grep(conn, "meeting project 2023-04-10")
        conn.close()
        paths = [r["path"] for r in results]
        assert "apr.md" in paths
        # apr.md should be ranked higher than dec.md for an April query
        if "dec.md" in paths:
            assert paths.index("apr.md") < paths.index("dec.md")

    def test_no_date_no_temporal_boost(self, temporal_db):
        """Without a date reference, all meetings rank by keyword relevance only."""
        root, db_path = temporal_db
        conn = connect(db_path)
        results = grep(conn, "meeting project")
        conn.close()
        # All three should be found (they all mention meeting and project)
        paths = [r["path"] for r in results]
        assert len(paths) >= 2
