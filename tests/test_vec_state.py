"""Regression: the STALE-VECTOR TRAP, and the per-invocation bound that heals it.

THE BUG (measured live 2026-07-26: 10 of 12 sampled docs stale, worst cosine
0.8518 against a fresh embed of the same file). `mvm-mirror` (cron */5) ends
each syncing tick with `mvm index --no-embed`, which rewrites the `files` row of
every edited doc — advancing files.mtime — while keeping the old files_vec row.
After that:
  * plain `mvm index` computes dirty as `_doc_mtime(p) != files.mtime`, which the
    --no-embed run already satisfied  -> the doc is never dirty again;
  * `mvm index --embed-only` selected `files_vec.path IS NULL`, which a
    stale-BUT-PRESENT vector does not satisfy  -> the 15-min heal skipped it.
So an edited doc was searched forever under the embedding of its FIRST version,
and only `--full` repaired it (measured >20 min, killed at rc=124 on this box —
i.e. the only repair path did not actually work). `mvm index --verify` cannot
see this class either: its check is also `files_vec.path IS NULL`.

The fix is vec_state(path, mtime) — the doc-mtime a stored vector was BUILT
FROM. test_embed_only_heals_a_stale_vector below FAILS on the pre-fix code (it
asserts the stored vector actually changes) and is the reason this file exists.

The bound tests are equally load-bearing: vec_state starts EMPTY on an existing
index.db, so the first heal sees the whole corpus and cannot finish inside one
cron tick. An unbounded heal there is killed mid-corpus every tick, forever,
while every guard log line reads CLEAN — the same silent-decay class the fix is
meant to end. Bounded, each tick converges a little and exits 0.

Slow (the first test loads the real embed model) — the rest fake the embedder.
"""
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from mvm import index


def _open(state: Path) -> sqlite3.Connection:
    idx = sqlite3.connect(state / "index.db")
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)
    return idx


def _vec(state: Path, rel: str) -> bytes | None:
    idx = _open(state)
    row = idx.execute("SELECT embedding FROM files_vec WHERE path = ?", (rel,)).fetchone()
    idx.close()
    return bytes(row[0]) if row else None


def _pending(state: Path) -> list[str]:
    """The tool's own dirty-for-embed definition, read straight from the module
    so the test can never drift from the query the heal actually runs."""
    idx = _open(state)
    index._ensure_vec_state(idx)
    out = [r[0] for r in idx.execute(index._PENDING_EMBED_SQL).fetchall()]
    idx.close()
    return out


def _touch_newer(p: Path, text: str) -> None:
    """Rewrite a doc and force a strictly newer mtime (tests run faster than
    filesystem mtime resolution on some hosts; a same-mtime rewrite would make
    this test pass for the wrong reason)."""
    p.write_text(text)
    future = time.time() + 10
    os.utime(p, (future, future))


def test_embed_only_heals_a_stale_vector():
    """END-TO-END, real embedder: reproduce the mirror trap, then heal it."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        state = Path(td) / "state"
        root.mkdir()
        state.mkdir()
        doc = root / "doc.md"
        doc.write_text("---\ntitle: Test\n---\nsteam locomotives and railway gauges\n")

        assert index.main(["--root", str(root), "--state", str(state), "--quiet"]) == 0
        v0 = _vec(state, "doc.md")
        assert v0 is not None, "fixture sanity: the first index must embed the doc"
        assert _pending(state) == [], "a freshly indexed corpus has nothing dirty"

        # The trap: the doc changes to a SEMANTICALLY UNRELATED body, and the
        # mirror's --no-embed tick advances files.mtime without re-embedding.
        _touch_newer(doc, "---\ntitle: Test\n---\ntropical reef fish and coral spawning\n")
        assert index.main(["--root", str(root), "--state", str(state),
                           "--no-embed", "--quiet"]) == 0
        assert _vec(state, "doc.md") == v0, (
            "fixture sanity: --no-embed must LEAVE the old vector in place "
            "(that preservation is deliberate; it is what makes the vector stale)"
        )

        # PRE-FIX CRITERION: 'missing vector' finds nothing here. This is why the
        # 15-min heal skipped the doc forever.
        idx = _open(state)
        old_criterion = idx.execute(
            "SELECT f.path FROM files f LEFT JOIN files_vec v ON v.path = f.path "
            "WHERE v.path IS NULL").fetchall()
        idx.close()
        assert old_criterion == [], (
            "the pre-fix criterion is supposed to be blind here — if it fires, "
            "this test is no longer reproducing the stale-vector trap"
        )
        # NEW CRITERION: sees it.
        assert _pending(state) == ["doc.md"], "stale-by-mtime must read as dirty-for-embed"

        # The cheap heal — not --full — must now repair it.
        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet"])
        assert rc == 0
        v1 = _vec(state, "doc.md")
        assert v1 is not None and v1 != v0, (
            "--embed-only reported success but the stored vector is byte-identical "
            "to the one built from the OLD body — the doc is still searched under "
            "its first version"
        )
        assert _pending(state) == [], "the heal must converge to zero dirty docs"

        # Idempotent: a second heal is a no-op, not a re-embed loop.
        assert index.main(["--root", str(root), "--state", str(state),
                           "--embed-only", "--quiet"]) == 0
        assert _vec(state, "doc.md") == v1


def _fake_corpus(td: Path, n: int) -> tuple[Path, Path]:
    root = td / "root"
    state = td / "state"
    root.mkdir()
    state.mkdir()
    for i in range(n):
        (root / f"doc{i}.md").write_text(f"---\ntitle: D{i}\n---\nbody number {i}\n")
    return root, state


def _fake_embed(monkeypatch, delay: float = 0.0):
    """Deterministic 384-float blobs — the bound is about scheduling, not vectors."""
    import struct

    def fake(bodies):
        if delay:
            time.sleep(delay)
        return [struct.pack("384f", *([float(len(b) % 7) / 7.0] * 384)) for b in bodies]

    monkeypatch.setattr(index, "_embed_batch", fake)


def test_embed_only_max_docs_bounds_the_run_and_converges(monkeypatch, capsys):
    """A capped invocation exits 0, embeds exactly its cap, and REPORTS the rest.

    Deferred is not failed: rc must stay 0 (mvm-verify-guard reads only rc, and a
    nonzero here would log EMBED_HEAL_FAIL every tick during a normal heal). But
    the deferral must reach stderr even under --quiet, or a heal that never
    converges is indistinguishable from a clean corpus.
    """
    with tempfile.TemporaryDirectory() as td:
        root, state = _fake_corpus(Path(td), 3)
        _fake_embed(monkeypatch)
        assert index.main(["--root", str(root), "--state", str(state),
                           "--quiet", "--no-embed"]) == 0
        # 3 docs + the auto-generated INDEX.md. Read the count off the tool rather
        # than hardcoding it, so index-generation changes can't fail this test for
        # an unrelated reason.
        start = len(_pending(state))
        assert start >= 3, "fixture: --no-embed must leave every doc unembedded"

        seen = []
        for tick in range(start):
            rc = index.main(["--root", str(root), "--state", str(state),
                             "--embed-only", "--quiet", "--max-docs", "1"])
            assert rc == 0, f"tick {tick}: a bounded (deferred) heal is success, not failure"
            left = len(_pending(state))
            seen.append(left)
            if left:
                err = capsys.readouterr().err
                assert "deferred" in err, (
                    f"tick {tick}: {left} doc(s) left unembedded and the run said "
                    f"nothing — a silent non-converging heal reads as CLEAN"
                )
        assert seen == list(range(start - 1, -1, -1)), (
            f"the heal must converge exactly one doc per capped tick "
            f"(start={start}); saw backlog {seen}"
        )


def test_embed_only_stops_on_the_time_budget(monkeypatch, capsys):
    """Wall-clock bound: the run ends ITSELF under budget rather than being killed.

    This is the rc=124 the `--full` heal died of on 2026-07-26, converted into a
    clean rc=0 plus an honest deferral count.
    """
    with tempfile.TemporaryDirectory() as td:
        # 130 docs = 3 batches of 64; the fake embedder makes each batch ~0.4 s.
        root, state = _fake_corpus(Path(td), 130)
        _fake_embed(monkeypatch, delay=0.4)
        assert index.main(["--root", str(root), "--state", str(state),
                           "--quiet", "--no-embed"]) == 0
        start = len(_pending(state))
        assert start >= 130

        t0 = time.time()
        rc = index.main(["--root", str(root), "--state", str(state),
                         "--embed-only", "--quiet", "--budget-seconds", "0.5"])
        elapsed = time.time() - t0
        assert rc == 0, "stopping on budget is a completed run, not a failure"
        assert elapsed < 3.0, f"the budget did not bite: run took {elapsed:.1f}s"
        left = len(_pending(state))
        assert 0 < left < start, (
            f"expected partial progress under the budget, got {start - left} embedded"
        )
        err = capsys.readouterr().err
        assert "deferred" in err and "time budget" in err, (
            "a budget-stopped run must name the deferral and its cause on stderr"
        )


def test_deleted_doc_drops_its_vec_state(monkeypatch):
    """No orphan vec_state row may survive a deletion — a later doc reusing that
    path would inherit a freshness claim for a vector it never built."""
    with tempfile.TemporaryDirectory() as td:
        root, state = _fake_corpus(Path(td), 2)
        _fake_embed(monkeypatch)
        assert index.main(["--root", str(root), "--state", str(state), "--quiet"]) == 0
        idx = _open(state)
        rows = {r[0] for r in idx.execute("SELECT path FROM vec_state").fetchall()}
        idx.close()
        assert {"doc0.md", "doc1.md"} <= rows, (
            f"every embedded doc must be stamped in vec_state; got {sorted(rows)}")

        (root / "doc1.md").unlink()
        assert index.main(["--root", str(root), "--state", str(state), "--quiet"]) == 0
        idx = _open(state)
        rows = {r[0] for r in idx.execute("SELECT path FROM vec_state").fetchall()}
        idx.close()
        assert "doc1.md" not in rows, f"vec_state kept an orphan row: {sorted(rows)}"
        assert "doc0.md" in rows, "the surviving doc lost its freshness stamp"
