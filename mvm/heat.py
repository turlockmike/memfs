"""
mvm-heat: retrieval-heat ranking + hot-doc test coverage.

WHY (2026-06-09 eval finding #3): only 37 of ~4.2K KB docs carry locked tests
(<1%), and verification effort was spread by age, not by which docs actually
answer questions. Heat = how often a doc grounded a real recall. Verification
budget should follow heat: a hot untested doc is a bigger risk than a cold
tested one.

Heat sources (per recall-log entry, kind=recall only):
  - evidence_paths[]            (explicit, post 2026-06-09 schema)
  - probes.kb.rationale         (legacy: parse *.md path mentions)

Usage:
  mvm heat                      # top-20 hot docs with test-coverage flags
  mvm heat --top 50
  mvm heat --untested           # only hot docs lacking <doc>.tests.yaml
  mvm heat --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path, PurePosixPath

DEFAULT_LOG = Path(os.environ.get(
    "MVM_RECALL_LOG", str(Path.home() / "mvm" / "state" / "recall-log.jsonl")))
DEFAULT_ROOT = Path(os.environ.get(
    "MVM_KNOWLEDGE_ROOT", str(Path.home() / "mvm" / "knowledge")))

_PATH_RE = re.compile(r"[\w~/.\-]+?\.md\b")


def _entry_kind(e: dict) -> str:
    k = e.get("kind")
    if k in ("recall", "dream-probe"):
        return k
    h = str(e.get("topic_hint") or "")
    if "dream-quality-verify" in h or "dream-staleness-verify" in h:
        return "dream-probe"
    return "recall"


def _normalize(p: str, root: Path) -> str | None:
    """Map a mentioned path to a knowledge-root-relative path if it exists."""
    p = p.strip().lstrip("~").lstrip("/")
    # strip leading home-ish prefixes (home dir derived, not hardcoded)
    home_pre = str(Path.home()).lstrip("/") + "/"
    for pre in (home_pre, "mvm/knowledge/", "knowledge/"):
        if p.startswith(pre):
            p = p[len(pre):]
    if p.endswith("INDEX.md") or p.endswith("index.md"):
        return None
    cand = root / p
    if cand.is_file():
        return p
    # try under resources/ (rationales often cite ~/resources/... which is
    # mirrored to knowledge/resources/)
    cand = root / "resources" / p
    if cand.is_file():
        return f"resources/{p}"
    return None


def heat_counts(log: Path, root: Path) -> Counter:
    counts: Counter = Counter()
    if not log.is_file():
        return counts
    for line in log.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if _entry_kind(e) != "recall":
            continue
        seen = set()
        for p in (e.get("evidence_paths") or []):
            n = _normalize(str(p), root)
            if n:
                seen.add(n)
        kb = ((e.get("probes") or {}).get("kb") or {})
        for m in _PATH_RE.findall(str(kb.get("rationale") or "")):
            n = _normalize(m, root)
            if n:
                seen.add(n)
        for n in seen:  # count each doc once per recall
            counts[n] += 1
    return counts


def has_tests(relpath: str, root: Path) -> bool:
    return (root / relpath.replace(".md", ".tests.yaml")).is_file()


# Retired-status set, kept identical to sweep.py's _RETIRED_STATUS so the two
# tools cannot disagree about the same doc (2026-07-25, dream-20260725-0647).
# WHY: `mvm heat --untested` is the worklist the /dream step-5 protocol reads to
# pick "the SINGLE hottest untested doc" to author locked tests for. Without this
# filter it happily nominates a doc whose own frontmatter says `status: superseded`
# — i.e. it directs verification budget at content already declared dead. Found
# live: resources/poe2/facts/_unverified/crafting-omens/
# sinestral-crystallization-prefix-remove.md, heat 2, UNTESTED, status superseded,
# sat at the TOP of --untested while `mvm sweep`'s untested_hot correctly reported
# 0 rows. The two tools looked like they contradicted each other; they didn't —
# sweep applied this filter and heat did not.
# Scope is deliberately narrow: retired docs stay in the plain heat RANKING,
# because a superseded doc still being retrieved is itself a real signal (the
# supersede didn't reach retrieval). Only the test-authoring worklist is filtered.
_RETIRED_STATUS = {"superseded", "archived", "retired", "deprecated"}


def is_retired(relpath: str, root: Path) -> bool:
    try:
        text = (root / relpath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    fm = text[3:end] if end > 0 else text[3:]
    for line in fm.splitlines():
        if line.lower().startswith("status:"):
            return line.split(":", 1)[1].strip().strip("\"'").lower() \
                in _RETIRED_STATUS
    return False


# Staging-tree exclusion — the SAME defect as _RETIRED_STATUS above, in the
# opposite polarity (2026-08-09, dream cycle).
# WHY: `--untested` is the worklist /dream step-5 reads to pick a doc to author
# LOCKED TESTS for. A doc under `_unverified/` is self-declared staging: it has
# not been distilled or cross-checked, and 1,240 of the 2,445 such docs still
# carry a literal "Verification TODO" section telling you so. Authoring a locked
# test against one does not merely waste budget the way a retired doc does — it
# is actively harmful, because the test would be written from the SAME
# unverified single-source claim as the doc, so it PASSES FOREVER and launders
# an unverified claim into a green oracle. That is exactly the class
# `contamination-scan` exists to catch ("a doc self-consistent with its OWN
# tests but contradicting a canonical allowlist"), manufactured on purpose.
# Measured when added: 11 of the 50 rows on the untested worklist were staging
# docs, and the single top-ranked nomination
# (facts/_unverified/build-warrior/amulet-melee-skills-rolls.md) had a body
# reading "Canonical: (to be distilled)" + "[ ] Cross-check top consensus value
# against poe2db.tw" — i.e. the tool was pointing verification budget at a doc
# whose own text says it is not yet verifiable.
# Scope is deliberately narrow, matching is_retired(): staging docs stay in the
# plain heat RANKING, because heat on unverified content is itself a real signal
# (staging material is reaching retrieval and grounding recalls) — it is only
# barred from the test-AUTHORING worklist, and it is REPORTED, never dropped
# silently.
_STAGING_SEGMENT = "_unverified"


def is_staging(relpath: str) -> bool:
    """True if the doc lives under an `_unverified/` staging directory.

    Matches a whole PATH SEGMENT, never a substring: `notes_unverified.md` and
    `_unverified-summary.md` are ordinary docs that merely mention the word, and
    excluding them would be the same over-reach this module's rule-3 cousins in
    /dream have degenerated into repeatedly.
    """
    return _STAGING_SEGMENT in PurePosixPath(relpath).parts[:-1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Retrieval-heat ranking + hot-doc test coverage.")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--untested", action="store_true",
                    help="Only hot docs lacking locked tests.")
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    counts = heat_counts(args.log, args.root)
    rows = [{"path": p, "recalls": c, "tested": has_tests(p, args.root)}
            for p, c in counts.most_common()]
    retired_skipped = []
    if args.untested:
        rows = [r for r in rows if not r["tested"]]
        # Never nominate already-dead content for test authoring; report what was
        # withheld so a shrunken worklist can't quietly read as "nothing to do".
        retired_skipped = [r["path"] for r in rows
                           if is_retired(r["path"], args.root)]
        rows = [r for r in rows if r["path"] not in set(retired_skipped)]
        # Never nominate self-declared staging content for test authoring; a
        # test authored from an unverified doc launders it into a green oracle.
        staging_skipped = [r["path"] for r in rows if is_staging(r["path"])]
        rows = [r for r in rows if r["path"] not in set(staging_skipped)]
    rows = rows[:args.top]

    hot_all = [{"path": p, "tested": has_tests(p, args.root)}
               for p, _ in counts.most_common(args.top)]
    covered = sum(1 for r in hot_all if r["tested"])
    summary = {
        "docs_with_heat": len(counts),
        f"hot_top{args.top}_tested": covered,
        f"hot_top{args.top}_coverage_pct":
            round(100 * covered / len(hot_all), 1) if hot_all else 0.0,
    }

    if args.untested:
        summary["retired_excluded"] = len(retired_skipped)
        summary["retired_excluded_paths"] = retired_skipped
        summary["staging_excluded"] = len(staging_skipped)
        summary["staging_excluded_paths"] = staging_skipped

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=1))
    else:
        print(f"Docs with retrieval heat: {summary['docs_with_heat']}")
        print(f"Hot top-{args.top} test coverage: {covered}/{len(hot_all)} "
              f"({summary[f'hot_top{args.top}_coverage_pct']}%)")
        if args.untested and retired_skipped:
            print(f"Excluded {len(retired_skipped)} retired/superseded doc(s) "
                  f"from the test-authoring worklist "
                  f"(matches mvm sweep untested_hot):")
            for p in retired_skipped:
                print(f"    - {p}")
        if args.untested and staging_skipped:
            print(f"Excluded {len(staging_skipped)} _unverified/ staging doc(s) "
                  f"from the test-authoring worklist (a locked test authored "
                  f"from an unverified doc passes forever and launders it):")
            for p in staging_skipped:
                print(f"    - {p}")
        print()
        for r in rows:
            flag = "✓ tests" if r["tested"] else "✗ UNTESTED"
            print(f"  {r['recalls']:3d}  {flag:10s}  {r['path']}")
    return 0


if __name__ == "__main__":  # `python3 -m mvm.<mod>` must RUN, never silently exit 0
    import sys
    sys.exit(main())
