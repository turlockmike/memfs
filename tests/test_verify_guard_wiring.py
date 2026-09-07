"""Regression: the `mvm-verify-guard` WIRING (not just the `--verify` binary).

The binary is covered by test_index_verify.py. This locks the GUARD's behavior —
the heal-then-verify sequence that makes a periodic verify cron safe:

  1. An embedding ghost (doc in files/FTS, no vector) is HEALED by the guard's
     `mvm index --embed-only` step → guard exits 0 (clean). This is the class
     mvm-mirror's `--no-embed` pass creates; a verify-ONLY cron would false-alarm
     on it, the guard must not.
  2. An FTS-missing inconsistency CANNOT be fixed by embed-only, so it SURVIVES
     the heal → guard exits 1 and names the offender. Real corruption is surfaced,
     not silenced.

The guard shells out to `mvm index`, which honors MVM_KNOWLEDGE/MVM_STATE; the
guard's own log/lock dir honors MVM_GUARD_STATE. So the whole script runs
hermetically against temp dirs.

Slow (index build loads the embed model) — run explicitly.
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import pytest

from mvm import index

GUARD = Path.home() / ".local/bin/mvm-verify-guard"


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
    index.main(["--root", str(root), "--state", str(state), "--quiet"])


def _run_guard(root: Path, state: Path, guard_state: Path):
    env = dict(os.environ)
    env["MVM_KNOWLEDGE"] = str(root)
    env["MVM_STATE"] = str(state)
    env["MVM_GUARD_STATE"] = str(guard_state)
    return subprocess.run([str(GUARD)], env=env, capture_output=True, text=True)


@pytest.fixture
def dirs():
    root = Path(tempfile.mkdtemp())
    state = Path(tempfile.mkdtemp())
    guard_state = Path(tempfile.mkdtemp())
    yield root, state, guard_state
    for d in (root, state, guard_state):
        shutil.rmtree(d, ignore_errors=True)


def test_guard_exists_executable():
    assert GUARD.exists(), f"{GUARD} missing"
    assert os.access(GUARD, os.X_OK), f"{GUARD} not executable"


def test_guard_heals_embedding_ghost_to_clean(dirs):
    root, state, guard_state = dirs
    _build(root, state)
    # Simulate mvm-mirror --no-embed landing a doc with no vector row.
    idx = _conn(state)
    idx.execute("DELETE FROM files_vec WHERE path = 'alpha.md'")
    idx.commit()
    idx.close()
    r = _run_guard(root, state, guard_state)
    assert r.returncode == 0, f"guard should heal the ghost and pass; got {r.returncode}\n{r.stderr}"
    # The embedding must actually be back.
    idx = _conn(state)
    rows = idx.execute("SELECT path FROM files_vec WHERE path = 'alpha.md'").fetchall()
    idx.close()
    assert rows, "guard did not restore the missing embedding"


def test_guard_surfaces_unhealable_inconsistency(dirs):
    root, state, guard_state = dirs
    _build(root, state)
    # FTS-missing is NOT fixable by embed-only → must survive the heal.
    idx = _conn(state)
    idx.execute("DELETE FROM files_fts WHERE path = 'alpha.md'")
    idx.commit()
    idx.close()
    r = _run_guard(root, state, guard_state)
    assert r.returncode == 1, f"guard should surface unhealable corruption; got {r.returncode}\n{r.stdout}\n{r.stderr}"
    assert "alpha.md" in r.stderr, "guard did not name the offender"
    # And it must have written the ledger record for the sweep.
    ledger = guard_state / "mvm-verify-guard.jsonl"
    assert ledger.exists() and "alpha.md" in ledger.read_text()


def test_embed_only_skips_cleanly_when_index_locked(dirs):
    """Regression (2026-09-07, mvm-verify-guard lock-contention finding —
    7 fires 2026-07-22..09-07): `mvm index --embed-only` used to connect
    straight to sqlite with no lock check, so a concurrent full `mvm index`
    holding a write transaction past the 30s busy_timeout made the connect
    itself raise `sqlite3.OperationalError: database is locked`, logged by
    the guard as a scary EMBED_HEAL_FAIL traceback for a race that always
    self-healed by the next tick anyway. --embed-only now checks
    `.index.lock` non-blocking first, same as the default full-rebuild path,
    and exits 3 with the same clean "already running" message instead.
    """
    root, state = dirs[0], dirs[1]
    _build(root, state)
    import fcntl
    lock_fp = open(state / ".index.lock", "w")
    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
    try:
        rc = index.main(["--state", str(state), "--embed-only", "--quiet"])
    finally:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()
    assert rc == 3, f"expected clean lock-contention exit 3, got {rc}"


def test_guard_logs_skip_not_fail_when_index_locked(dirs):
    root, state, guard_state = dirs
    _build(root, state)
    import fcntl
    lock_fp = open(state / ".index.lock", "w")
    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
    try:
        r = _run_guard(root, state, guard_state)
    finally:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        lock_fp.close()
    log = (guard_state / "mvm-verify-guard.log").read_text()
    assert "EMBED_HEAL_SKIP" in log, f"expected a non-alarming skip line; log:\n{log}"
    assert "EMBED_HEAL_FAIL" not in log, f"lock contention must not log as FAIL; log:\n{log}"
    assert "OperationalError" not in log, f"raw sqlite traceback leaked into the log:\n{log}"
