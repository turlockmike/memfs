"""Regression: `mvm index --verify` must DETECT structural index corruption.

The retrieval index is the oracle the /ingest gate depends on. A partial index
(a doc in `files`/FTS but missing its embedding, or with a body that isn't
FTS-matchable, or n_tests out of sync with its .tests.yaml sidecar) silently
demotes a doc from search — the class that bit twice on 2026-06-23 before any
detector existed. The ghost self-heal (test_index_ghost_heal) FIXES embeddings
on the next run; --verify ASSERTS the state so drift is caught the moment it
appears. A verifier that always says "clean" is worthless, so this locks that
it returns 1 + names the offender for EACH inconsistency class, and 0 when clean.

Slow (the index build loads the embed model) — run explicitly.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from mvm import index


def _conn(state: Path) -> sqlite3.Connection:
    idx = sqlite3.connect(state / "index.db")
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    return idx


def _build(root: Path, state: Path):
    (root / "alpha.md").write_text("---\ntitle: Alpha\n---\nbody about widgets and gears\n")
    (root / "beta.md").write_text("---\ntitle: Beta\n---\nbody about levers and cogs\n")
    # beta has a 3-test sidecar so n_tests drift is testable
    (root / "beta.tests.yaml").write_text("- q: one\n- q: two\n- q: three\n")
    index.main(["--root", str(root), "--state", str(state), "--quiet"])


def _verify(root: Path, state: Path, capsys):
    rc = index.main(["--root", str(root), "--state", str(state), "--verify", "--json"])
    report = json.loads(capsys.readouterr().out)
    return rc, report


def test_verify_clean_index_passes(capsys):
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    _build(root, state)
    rc, report = _verify(root, state, capsys)
    assert rc == 0
    assert report["clean"] is True
    # alpha.md + beta.md + the auto-generated root INDEX.md
    assert report["files"] >= 2
    assert report["embedding_ghosts"] == []
    assert report["fts_missing"] == []
    assert report["n_tests_drift"] == []


def test_verify_catches_embedding_ghost(capsys):
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    _build(root, state)
    idx = _conn(state)
    idx.execute("DELETE FROM files_vec WHERE path = 'alpha.md'")
    idx.commit()
    idx.close()
    rc, report = _verify(root, state, capsys)
    assert rc == 1
    assert report["clean"] is False
    assert "alpha.md" in report["embedding_ghosts"]
    assert "beta.md" not in report["embedding_ghosts"]


def test_verify_catches_fts_missing(capsys):
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    _build(root, state)
    idx = _conn(state)
    idx.execute("DELETE FROM files_fts WHERE path = 'alpha.md'")
    idx.commit()
    idx.close()
    rc, report = _verify(root, state, capsys)
    assert rc == 1
    assert "alpha.md" in report["fts_missing"]


def test_verify_catches_n_tests_drift(capsys):
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    _build(root, state)
    # Mutate the sidecar to 1 test WITHOUT re-indexing — db still records 3.
    (root / "beta.tests.yaml").write_text("- q: only-one\n")
    rc, report = _verify(root, state, capsys)
    assert rc == 1
    drift_paths = {d["path"] for d in report["n_tests_drift"]}
    assert "beta.md" in drift_paths
    drow = next(d for d in report["n_tests_drift"] if d["path"] == "beta.md")
    assert drow["db_n_tests"] == 3
    assert drow["sidecar"] == 1
