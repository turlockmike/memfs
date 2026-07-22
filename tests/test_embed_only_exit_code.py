"""Regression: `mvm index --embed-only` must EXIT NONZERO when embedding failed.

Why this is load-bearing (2026-07-22, act-stage SIGTERM investigation):
`mvm-verify-guard` runs the embed heal every 15 min and reads ONLY its exit code
to decide whether to log `EMBED_HEAL_FAIL` / `EMBED_HEAL_OOM`. Before this fix
`_embed_only_main` caught a failed batch, printed a `warn:` line to stderr,
`continue`d, and **returned 0 regardless** — so a heal in which every single
batch died would surface to the guard as success, the guard would log a serene
`CLEAN files=N`, and the embedding index (the whole `mvm search` surface) would
decay silently. The absence of alarm was not evidence of health.

The two cases below disagree under the buggy code (both return 0) and agree with
the guard's reading under the fixed code (fail -> nonzero, clean -> 0). A test
with only the failure case would pass on a `return 1` stub; a test with only the
clean case carries zero bits. Both are required.

Slow (loads the embed model) — run explicitly, not in the fast unit sweep.
"""
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
    """Drop every vec row, leaving files/FTS intact -> a real embed backlog."""
    idx = _open(state)
    idx.execute("DELETE FROM files_vec")
    idx.commit()
    idx.close()


def test_embed_only_exits_nonzero_when_every_batch_fails(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root, state = _build(Path(td))
        _make_ghosts(state)

        def boom(_bodies):
            raise MemoryError("simulated OOM in the ONNX inference batch")

        monkeypatch.setattr(index, "_embed_batch", boom)
        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet"])
        assert rc != 0, (
            "embed-only swallowed a total embed failure and reported success; "
            "mvm-verify-guard reads this exit code as 'heal OK'"
        )

        idx = _open(state)
        remaining = idx.execute("SELECT count(*) FROM files_vec").fetchone()[0]
        idx.close()
        assert remaining == 0, "fixture sanity: nothing should have been embedded"


def test_embed_only_exits_zero_on_a_real_heal():
    """The counterweight: a heal that actually works must still be rc=0."""
    with tempfile.TemporaryDirectory() as td:
        root, state = _build(Path(td))
        _make_ghosts(state)

        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet"])
        assert rc == 0, "a successful backfill must not report failure"

        idx = _open(state)
        n = idx.execute("SELECT count(*) FROM files_vec").fetchone()[0]
        idx.close()
        assert n > 0, "the backfill should have re-embedded the ghost doc"


def test_embed_only_exits_zero_when_there_is_nothing_to_do():
    """No backlog at all (today's live state: 4800/4800) is success, not failure."""
    with tempfile.TemporaryDirectory() as td:
        root, state = _build(Path(td))
        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet"])
        assert rc == 0
