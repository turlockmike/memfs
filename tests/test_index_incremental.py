"""Incremental indexing (2026-06-10): default mode diffs disk vs the
files.mtime manifest and touches only added/changed/deleted docs.
--full forces the old wipe-everything path. All tests run --no-embed
so no embedding model is downloaded/loaded.
"""
import os
import sqlite3
from pathlib import Path

from mvm.index import main as index_main


def _run(tmp_path, *extra):
    argv = ["--root", str(tmp_path / "knowledge"),
            "--state", str(tmp_path / "state"),
            "--no-embed", "--quiet", *extra]
    return index_main(argv)


def _db(tmp_path):
    return sqlite3.connect(tmp_path / "state" / "index.db")


def _graph(tmp_path):
    return sqlite3.connect(tmp_path / "state" / "graph.db")


def _paths(tmp_path):
    return {r[0] for r in _db(tmp_path).execute("SELECT path FROM files")}


def _fts_body(tmp_path, rel):
    rows = _db(tmp_path).execute(
        "SELECT body FROM files_fts WHERE path = ?", (rel,)).fetchall()
    assert len(rows) == 1, f"expected exactly 1 FTS row for {rel}, got {len(rows)}"
    return rows[0][0]


def _bump_mtime(p: Path, delta: float = 10.0):
    """Force a visibly different mtime — sub-test-runtime granularity safety."""
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + delta))


def test_first_run_is_full_then_up_to_date(tmp_kb, capsys):
    assert _run(tmp_kb) == 0
    assert _paths(tmp_kb) >= {"topic-a.md", "topic-b.md"}
    # Second run with zero changes: early-exit, index intact
    argv = ["--root", str(tmp_kb / "knowledge"), "--state", str(tmp_kb / "state"),
            "--no-embed"]
    assert index_main(argv) == 0
    assert "up to date" in capsys.readouterr().out
    assert _paths(tmp_kb) >= {"topic-a.md", "topic-b.md"}


def test_added_doc_picked_up(tmp_kb):
    _run(tmp_kb)
    (tmp_kb / "knowledge" / "topic-c.md").write_text(
        "---\nkind: canonical\nsummary: \"C.\"\n---\n\n# Topic C\n\nlinks [A](topic-a.md)\n")
    assert _run(tmp_kb) == 0
    assert "topic-c.md" in _paths(tmp_kb)
    assert "Topic C" in _fts_body(tmp_kb, "topic-c.md")
    edges = _graph(tmp_kb).execute(
        "SELECT dst FROM edges WHERE src = 'topic-c.md' AND edge_type = 'md_link'").fetchall()
    assert ("topic-a.md",) in edges


def test_changed_doc_reindexed_no_duplicates(tmp_kb):
    _run(tmp_kb)
    doc = tmp_kb / "knowledge" / "topic-b.md"
    doc.write_text(doc.read_text().replace("This is topic B.",
                                           "REWRITTEN body with [new link](topic-a.md)."))
    _bump_mtime(doc)
    assert _run(tmp_kb) == 0
    body = _fts_body(tmp_kb, "topic-b.md")  # asserts exactly 1 row (no dup)
    assert "REWRITTEN" in body
    n = _db(tmp_kb).execute(
        "SELECT COUNT(*) FROM files WHERE path = 'topic-b.md'").fetchone()[0]
    assert n == 1
    edges = _graph(tmp_kb).execute(
        "SELECT dst, edge_type FROM edges WHERE src = 'topic-b.md'").fetchall()
    assert ("topic-a.md", "md_link") in edges


def test_deleted_doc_purged(tmp_kb):
    _run(tmp_kb)
    (tmp_kb / "knowledge" / "topic-b.md").unlink()
    (tmp_kb / "knowledge" / "topic-b.tests.yaml").unlink()
    assert _run(tmp_kb) == 0
    assert "topic-b.md" not in _paths(tmp_kb)
    assert _db(tmp_kb).execute(
        "SELECT COUNT(*) FROM files_fts WHERE path = 'topic-b.md'").fetchone()[0] == 0
    assert _graph(tmp_kb).execute(
        "SELECT COUNT(*) FROM edges WHERE src = 'topic-b.md'").fetchone()[0] == 0


def test_tests_yaml_only_change_refreshes_n_tests(tmp_kb):
    _run(tmp_kb)
    ty = tmp_kb / "knowledge" / "topic-a.tests.yaml"
    ty.write_text(ty.read_text() + "- id: 2\n  q: \"second?\"\n  a: \"yes\"\n")
    _bump_mtime(ty)
    assert _run(tmp_kb) == 0
    n_tests = _db(tmp_kb).execute(
        "SELECT n_tests FROM files WHERE path = 'topic-a.md'").fetchone()[0]
    assert n_tests == 2


def test_index_md_write_if_changed_keeps_mtime_stable(tmp_kb):
    _run(tmp_kb)
    idx_md = tmp_kb / "knowledge" / "INDEX.md"
    assert idx_md.exists()
    before = idx_md.stat().st_mtime
    assert _run(tmp_kb) == 0  # no content change -> no rewrite -> no mtime bump
    assert idx_md.stat().st_mtime == before


def test_full_flag_forces_rebuild(tmp_kb, capsys):
    _run(tmp_kb)
    argv = ["--root", str(tmp_kb / "knowledge"), "--state", str(tmp_kb / "state"),
            "--no-embed", "--full"]
    assert index_main(argv) == 0
    assert "[full]" in capsys.readouterr().out
    assert _paths(tmp_kb) >= {"topic-a.md", "topic-b.md"}
