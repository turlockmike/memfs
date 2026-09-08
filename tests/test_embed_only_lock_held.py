"""Regression: `mvm index --embed-only` must HOLD `.index.lock` for its whole
run, not just check-and-release it before starting to write.

Why this is load-bearing (2026-09-08, mvm-verify-guard lock-contention finding,
second occurrence): a 2026-09-07 fix added a lock CHECK to `_embed_only_main`
(open `.index.lock`, `flock(LOCK_EX|LOCK_NB)`, immediately `flock(LOCK_UN)`) to
stop it from racing a concurrent FULL `mvm index` rebuild. That fixed the
obvious case but left the lock held for zero time — a window in which a
DIFFERENT `mvm index` invocation (e.g. `mvm-mirror`'s `mvm index --no-embed`,
which acquires the same lock via main()'s own single-writer path and holds it
for ITS duration) could acquire the now-free lock and start writing while
embed-only's own writes (`_ensure_vec_state`'s executescript, the files_vec
INSERTs) were still in flight. That produced a real `sqlite3.OperationalError:
database is locked` on 2026-09-08, reproducing live during this fix's own
investigation.

The fix: embed-only now acquires `.index.lock` once and holds it for the
duration of the heal (released via `finally` in `_embed_only_main`), the same
contract main()'s full-rebuild path already gives itself. This test proves the
lock is actually HELD during the write phase — not just checked at entry — by
attempting a second, independent acquisition WHILE a batch is mid-flight and
asserting it is refused. A regression back to check-then-release would let
this second acquisition through and fail the test.
"""
import fcntl
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mvm import index


def _open(state: Path) -> sqlite3.Connection:
    idx = sqlite3.connect(state / "index.db")
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    return idx


def _build(tmp: Path) -> tuple[Path, Path]:
    root = tmp / "root"
    state = tmp / "state"
    root.mkdir()
    state.mkdir()
    (root / "doc.md").write_text("---\ntitle: Test\n---\nbody about widgets and gears\n")
    assert index.main(["--root", str(root), "--state", str(state), "--quiet"]) == 0
    return root, state


def _make_ghosts(state: Path) -> None:
    idx = _open(state)
    idx.execute("DELETE FROM files_vec")
    idx.commit()
    idx.close()


def test_embed_only_holds_lock_for_duration_not_just_at_entry(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root, state = _build(Path(td))
        _make_ghosts(state)

        observed = {}

        real_embed_batch = index._embed_batch

        def spy(bodies):
            # Mid-heal: a second, independent open-file-description on the same
            # lock path must NOT be able to acquire it while embed-only is
            # still writing. flock is scoped to the open-file-description, so
            # this genuinely probes cross-process-shaped exclusion even though
            # both attempts happen in this one test process.
            lock_path = state / ".index.lock"
            probe_fp = open(lock_path, "w")
            try:
                fcntl.flock(probe_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed["acquired_while_running"] = True
                fcntl.flock(probe_fp.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                observed["acquired_while_running"] = False
            finally:
                probe_fp.close()
            return real_embed_batch(bodies)

        monkeypatch.setattr(index, "_embed_batch", spy)
        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet"])

        assert rc == 0, "sanity: the heal itself must still succeed"
        assert "acquired_while_running" in observed, (
            "the spy never ran — fixture didn't actually exercise a batch"
        )
        assert observed["acquired_while_running"] is False, (
            "a second flock acquisition SUCCEEDED while embed-only was still "
            "writing -- the lock is being released too early (regressed to "
            "check-then-release), which is exactly the 2026-09-08 race"
        )


def test_embed_only_refuses_when_lock_already_held(monkeypatch):
    """The other half of the contract: embed-only must still defer (rc=3)
    immediately when something else already holds `.index.lock` at entry —
    unchanged behavior from the 2026-09-07 fix, just re-asserted against the
    refactor so the two code paths (refuse-at-entry vs. hold-for-duration)
    can't silently diverge."""
    with tempfile.TemporaryDirectory() as td:
        root, state = _build(Path(td))
        _make_ghosts(state)

        lock_path = state / ".index.lock"
        holder_fp = open(lock_path, "w")
        fcntl.flock(holder_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc = index.main(["--root", str(root), "--state", str(state),
                             "--embed-only", "--quiet"])
            assert rc == 3, "embed-only must refuse (rc=3) when the lock is held"
        finally:
            fcntl.flock(holder_fp.fileno(), fcntl.LOCK_UN)
            holder_fp.close()
