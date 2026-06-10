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
from pathlib import Path

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
    # strip leading home-ish prefixes
    for pre in ("home/mike/", "mvm/knowledge/", "knowledge/"):
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
    if args.untested:
        rows = [r for r in rows if not r["tested"]]
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

    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, indent=1))
    else:
        print(f"Docs with retrieval heat: {summary['docs_with_heat']}")
        print(f"Hot top-{args.top} test coverage: {covered}/{len(hot_all)} "
              f"({summary[f'hot_top{args.top}_coverage_pct']}%)")
        print()
        for r in rows:
            flag = "✓ tests" if r["tested"] else "✗ UNTESTED"
            print(f"  {r['recalls']:3d}  {flag:10s}  {r['path']}")
    return 0
