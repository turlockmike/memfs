"""mvm doc-sim — absolute cosine between two INDEXED docs (doc↔doc, not doc↔query).

WHY THIS EXISTS (gate #47, turned on my own tension gate)
---------------------------------------------------------
`/dream` step 5(b) hunts NEAR-DUPLICATE PAIRS worth probing for contradiction.
Every proxy it has used measured a DIFFERENT quantity than the one it named:

  rung 1  `score > 0.7`             UNREACHABLE — rank-1 `text` is always 1.0 and
                                    graph/hier are 0 without `--near`, so the
                                    ceiling for any step-5b query is 0.600. Every
                                    "no new tensions" it ever reported was a
                                    vacuous negative, not evidence of a clean corpus.
  rung 2  rank-2 `components.text`  REACHABLE BUT CONTENT-BLIND — RRF is purely
          >= 0.98                   rank-based, then max-normalized, so the value is
                                    a ratio of rank-reciprocals. MEASURED over 11
                                    unrelated queries (2026-07-26): the value 0.9841
                                    occurs on THREE of them — `catalyst quality
                                    amulet`, `budget monarch categories`, `french
                                    lesson subjunctive` — i.e. the metric reports
                                    them as IDENTICAL, while their absolute cosines
                                    spread 0.7820 / 0.6425 / 0.6855. It also fired on
                                    a CONFIRMED retrieval failure: two docs equally
                                    IRRELEVANT to the query, which normalization
                                    renders indistinguishable from two equally
                                    RELEVANT ones.
  rung 3  THIS                      Measures the PAIR. A pair's similarity is a
                                    property of the two DOCS; the query is not part
                                    of the question the rule asks. ⛔ BUILT, GRADED,
                                    and **REJECTED AS A TENSION GATE** the same hour
                                    (2026-07-26) — see the block below. It remains a
                                    correct similarity instrument; it is simply not
                                    the tension oracle it was built to be.

Rungs 1 and 2 both measured doc↔QUERY and called it pair similarity. That is the
whole bug, and it survived two repairs because each repair fixed the defect the
previous rung exposed (reachability, then normalization) without ever asking
whether the CHANNEL could carry the claim being made on it.

⛔ RUNG 3 FAILS ITS OWN PRE-REGISTERED DONE-TEST — DO NOT RE-PICK IT AS A TENSION GATE
--------------------------------------------------------------------------------------
Done-test, set BEFORE building (queue row L225): *a known near-duplicate pair must
fire; the pair from a known-FAILED retrieval must NOT.* Measured on the live corpus:

    TRUE  near-dup pair   poe2/0.5/crafting/jewellery-quality-system.md
                        ↔ poe2/0.4/mechanics/quality.md               → 0.9367
    FALSE pair (top-2 of the CONFIRMED retrieval failure
      "grasping mail breach modifier chaos spam")
                          areas/research-queue-cold.md
                        ↔ areas/backlog.md                             → 0.9805
    NULL (random pairs, n=210)  p50 0.6419 · p90 0.7351 · p99 0.7953 · max 0.8101

**The FALSE pair scores HIGHER than the TRUE one.** The ordering is INVERTED, so no
threshold can separate them — rung 3 is refuted by its own falsifier, not merely weak.

WHY — the durable part: `research-queue-cold.md` and `backlog.md` are both long,
heterogeneous GRAB-BAG files of my own queue prose. A whole-document embedding of a
grab-bag encodes GENRE AND VOICE, not subject matter — so two unrelated lists written
in one hand embed as near-identical, while two docs genuinely about the same mechanic
written in different eras do not. **A document that is about one thing embeds its
subject; a document about forty things embeds its author.**

⇒ **All three similarity proxies are dead** (unreachable → content-blind → inverted),
and they died of one shared cause: TENSION IS A RELATION BETWEEN CLAIMS, and no
aggregate of a whole document carries claim structure. Rung 4 must be a DIRECT
structural check (same `kind`, same declared version, overlapping title terms, neither
doc an aggregator) — not a fourth number.

⇒ COROLLARY WORTH MORE THAN THE GATE ITSELF: those two grab-bag files are ALSO the
vector leg's top-2 for a PoE2 CRAFTING query. Style-dominance is therefore a live cause
of the standing `/recall` retrieval defect — and it predicts that SPLITTING the
grab-bag files would lift retrieval. That is a far cheaper experiment than swapping the
embedding model, AND the incumbent-biased sweep can legitimately grade it, because the
CORPUS changes while the retriever is held fixed.

METRIC — verified against an independent oracle, not assumed (2026-07-26)
-------------------------------------------------------------------------
Stored embeddings are unit vectors (measured norm exactly 1.000000), so cosine is
a plain dot product. Cross-checked against sqlite-vec's own knn `distance`:
that distance is PLAIN L2 (NOT squared), so cos = 1 - d^2/2 — which reproduced
the numpy dot to 6 decimals on live rows, while search.py's display transform
`1 - d/2` did not (d=0.4646 → true cos 0.8921, display 0.7677). The display scale
is monotone in d, so it never mis-ORDERED anything; it was only ever mis-NAMED.

⚠ CALIBRATION IS MANDATORY — bge-small-en-v1.5 has a HIGH similarity floor:
unrelated documents routinely sit at 0.60-0.75. An absolute cosine here is
meaningful ONLY against a measured threshold, never against intuition about what
"0.8 sounds similar" means. Run `--calibrate` to re-derive the floor on the live
corpus before trusting any cutoff.

dup-checked 2026-07-26: this is a `mvm` SUBCOMMAND rather than a 10th top-level
`mvm-*` script because the existing family splits cleanly — `mvm-retrieval-sweep`
/ `-guard` / `-floor` / `-sweep-liveness` grade the RETRIEVER, `mvm-mirror` /
`-embed-refresh` / `-ingest-backstop` move CONTENT, and doc↔doc questions already
live INSIDE `mvm` as `relations` and `backlinks`. This is their sibling: it reads
the same index and answers "how close are these two docs", so it belongs beside
them, not in the grading family.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import struct
import sys
from pathlib import Path

DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))


def load_vec(cur, path: str):
    """Return the stored embedding for an indexed path, or None if absent."""
    cur.execute("SELECT embedding FROM files_vec WHERE path = ?", (path,))
    row = cur.fetchone()
    if row is None:
        return None
    blob = row[0]
    return struct.unpack(f"{len(blob) // 4}f", blob)


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def resolve(cur, path: str):
    """Exact match, else UNIQUE suffix match.

    Ambiguity is an ERROR, never a silent pick — resolving `quality.md` to
    whichever row sorted first is how a pair-similarity number gets attributed
    to the wrong pair of docs.
    """
    if load_vec(cur, path) is not None:
        return path, None
    cur.execute("SELECT path FROM files_vec WHERE path LIKE ?", (f"%{path}",))
    hits = [r[0] for r in cur.fetchall()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"not indexed: {path}"
    return None, f"ambiguous — {len(hits)} paths end with {path!r}: {hits[:5]}"


def calibrate(cur, n: int = 400) -> dict:
    """Measure the corpus's own similarity floor from RANDOM pairs.

    A threshold picked without this is a guess about the embedding model's
    scale. Random pairs are overwhelmingly unrelated, so their distribution IS
    the null hypothesis a near-duplicate claim must beat.
    """
    cur.execute("SELECT path FROM files_vec ORDER BY path")
    paths = [r[0] for r in cur.fetchall()]
    if len(paths) < 4:
        return {}
    # Deterministic stride-sampling (no RNG — this must reproduce exactly).
    sims = []
    stride = max(1, len(paths) // int(math.sqrt(max(n, 1)) + 1))
    picks = paths[::stride][:int(math.sqrt(max(n, 1))) + 1]
    for i, pa in enumerate(picks):
        va = load_vec(cur, pa)
        for pb in picks[i + 1:]:
            sims.append(cosine(va, load_vec(cur, pb)))
    if not sims:
        return {}
    sims.sort()

    def pct(p):
        return sims[min(len(sims) - 1, int(p * len(sims)))]

    return {
        "n_pairs": len(sims),
        "min": round(sims[0], 4),
        "p50": round(pct(0.50), 4),
        "p90": round(pct(0.90), 4),
        "p99": round(pct(0.99), 4),
        "max": round(sims[-1], 4),
        "mean": round(sum(sims) / len(sims), 4),
    }


def open_index(state: Path):
    db = state / "index.db"
    if not db.exists():
        return None, f"no index at {db}"
    conn = sqlite3.connect(db)
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
    except Exception:
        pass
    conn.enable_load_extension(False)
    return conn, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mvm doc-sim",
        description="Absolute cosine between two indexed docs (doc↔doc).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="⚠ bge-small's floor is ~0.6-0.75 for UNRELATED docs. Run "
               "--calibrate before trusting any threshold.")
    ap.add_argument("doc_a", nargs="?")
    ap.add_argument("doc_b", nargs="?")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--threshold", type=float, default=None,
                    help="Gate mode: exit 1 when cosine is BELOW this.")
    ap.add_argument("--calibrate", action="store_true",
                    help="Print the corpus's random-pair similarity distribution.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    conn, err = open_index(args.state)
    if err:
        print(f"mvm doc-sim: {err}", file=sys.stderr)
        return 4
    cur = conn.cursor()

    if args.selftest:
        return selftest(cur)

    if args.calibrate:
        stats = calibrate(cur)
        if args.json:
            print(json.dumps(stats, indent=2))
        else:
            print("random-pair cosine distribution (the NULL a near-dup must beat):")
            for k, v in stats.items():
                print(f"  {k:>8}  {v}")
        return 0

    if not args.doc_a or not args.doc_b:
        print("mvm doc-sim: need two docs (or --calibrate / --selftest)", file=sys.stderr)
        return 2

    pa, err_a = resolve(cur, args.doc_a)
    pb, err_b = resolve(cur, args.doc_b)
    for e in (err_a, err_b):
        if e:
            print(f"mvm doc-sim: {e}", file=sys.stderr)
            return 4

    c = cosine(load_vec(cur, pa), load_vec(cur, pb))
    if args.json:
        print(json.dumps({"a": pa, "b": pb, "cosine": round(c, 6)}))
    else:
        print(f"{c:.4f}  {pa}\n        {pb}")
    if args.threshold is not None:
        return 0 if c >= args.threshold else 1
    return 0


def selftest(cur) -> int:
    """Oracles WITH negative controls — a check nothing can fail is not a check."""
    fails: list[str] = []

    def ck(name, cond, detail=""):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
        if not cond:
            fails.append(name)

    cur.execute("SELECT path FROM files_vec LIMIT 2")
    rows = [r[0] for r in cur.fetchall()]
    if len(rows) < 2:
        print("  SKIP — corpus too small to self-test")
        return 0
    a, b = rows[0], rows[1]
    va, vb = load_vec(cur, a), load_vec(cur, b)

    ck("identity: cos(x,x) == 1", abs(cosine(va, va) - 1.0) < 1e-6, f"{cosine(va, va):.6f}")
    ck("symmetry: cos(a,b) == cos(b,a)", abs(cosine(va, vb) - cosine(vb, va)) < 1e-9)
    ck("range: cos within [-1,1]", -1.0 <= cosine(va, vb) <= 1.0, f"{cosine(va, vb):.4f}")
    ck("stored vectors are UNIT (the metric's stated premise)",
       abs(math.sqrt(sum(x * x for x in va)) - 1.0) < 1e-5,
       f"norm={math.sqrt(sum(x * x for x in va)):.6f}")

    # NEGATIVE CONTROL: without this, an implementation that always returned 1.0
    # would pass identity/symmetry/range and look perfectly healthy.
    ck("MUTANT(constant 1.0) would be CAUGHT — distinct docs must not read 1.0",
       cosine(va, vb) < 0.999999, f"distinct docs read {cosine(va, vb):.6f}")

    # Cross-check against sqlite-vec's INDEPENDENT distance implementation.
    blob = struct.pack(f"{len(va)}f", *va)
    ok, detail = True, ""
    try:
        cur.execute("SELECT path, distance FROM files_vec WHERE embedding MATCH ? AND k = 3",
                    (blob,))
        for pth, d in cur.fetchall():
            mine = cosine(va, load_vec(cur, pth))
            theirs = 1.0 - (d * d) / 2.0
            if abs(mine - theirs) > 1e-4:
                ok, detail = False, f"{pth}: mine={mine:.6f} sqlite-vec={theirs:.6f}"
        ck("agrees with sqlite-vec's independent distance (cos = 1 - L2^2/2)", ok, detail)
        # And the LEGACY display transform must DISAGREE — that disagreement is
        # the whole reason this module exists; if it ever agrees, the premise
        # above is wrong and the docstring is lying.
        cur.execute("SELECT distance FROM files_vec WHERE embedding MATCH ? AND k = 3", (blob,))
        ds = [r[0] for r in cur.fetchall() if r[0] > 0.01]
        if ds:
            d0 = ds[0]
            ck("legacy `1 - d/2` display scale DIFFERS from true cosine (the premise)",
               abs((1 - d0 / 2) - (1 - d0 * d0 / 2)) > 1e-3,
               f"display={1 - d0/2:.4f} true={1 - d0*d0/2:.4f}")
    except Exception as e:
        ck("agrees with sqlite-vec's independent distance", False, str(e))

    ck("unindexed path is an ERROR, never a silent 0.0",
       resolve(cur, "definitely/not/a/real/doc-xyzzy.md")[0] is None)

    stats = calibrate(cur)
    ck("calibration reports a floor (thresholds need the null distribution)",
       bool(stats) and stats.get("n_pairs", 0) > 10,
       f"n_pairs={stats.get('n_pairs')} p50={stats.get('p50')} p99={stats.get('p99')}")

    print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failure(s)")
    return 0 if not fails else 1


if __name__ == "__main__":  # `python3 -m mvm.<mod>` must RUN, never silently exit 0
    sys.exit(main())
