"""Regression: plain incremental `mvm index` must self-heal embedding ghosts.

A doc whose embed batch silently failed once lands in files/FTS with current
mtime but NO files_vec row. The mtime diff never re-flags it, so before the
2026-06-24 fix `mvm index` reported "up to date" forever and the doc stayed
semantically unsearchable until a manual --embed-only/--full. This locks the
self-heal: missing-embedding is now treated as dirty-for-embed.

Slow (loads the embed model) — run explicitly, not in the fast unit sweep.
"""
import sqlite3
import tempfile
from pathlib import Path

from mvm import index


def _vec_count(state: Path) -> int:
    idx = sqlite3.connect(state / "index.db")
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    n = idx.execute("SELECT count(*) FROM files_vec").fetchone()[0]
    idx.close()
    return n


def test_incremental_index_self_heals_embedding_ghost():
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    (root / "doc.md").write_text("---\ntitle: Test\n---\nbody about widgets and gears\n")

    index.main(["--root", str(root), "--state", str(state), "--quiet"])
    full_n = _vec_count(state)
    assert full_n > 0, "full index should embed at least one doc"

    # Simulate the ghost: drop every vec row, leave files/FTS intact.
    idx = sqlite3.connect(state / "index.db")
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    idx.execute("DELETE FROM files_vec")
    idx.commit()
    idx.close()
    assert _vec_count(state) == 0

    # Plain incremental index (no mtime change) MUST re-embed the ghost docs.
    rc = index.main(["--root", str(root), "--state", str(state), "--quiet"])
    assert rc == 0
    assert _vec_count(state) == full_n, "incremental index must self-heal the embedding ghost"
