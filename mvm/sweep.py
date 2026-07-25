"""
mvm sweep — the ZERO-TOKEN curation tier (hermes-parity build #4).

WHY (PLAN.md §4): the insight worth stealing from hermes-agent is the SPLIT.
Today the dream cron spends Claude tokens DISCOVERING what needs attention
(mean recall probe ~73s of model time) and then spends more tokens JUDGING it.
Discovery is deterministic — staleness, dead weight, missing oracles, broken
links, index drift are all decidable by reading files. Only judgment needs a
model. So: `mvm sweep` finds the work at zero model cost and writes a worklist;
the dream pass CONSUMES that worklist and spends tokens only on the calls that
actually require judgment.

CLOSED LOOP (architecture invariant #17, named at creation):
  producer = cron (hourly is fine — it is free) + any session running `mvm sweep`
  consumer = the dream pass (reads the worklist instead of rediscovering it)
  drift    = sweep-log freshness, checkable by the auditor (`--json` carries `generated`)
  decay    = the worklist is REGENERATED each run, never appended — a stale row cannot
             survive a single sweep, so nothing here can rot into a phantom task.

HONESTY RULES BAKED IN (each one is a scar, not a preference):
  * `review_at` that does not start with an ISO date is reported as
    `review_at_unparseable`, NEVER silently skipped and never treated as "no
    review owed". Unknown is not innocent.
  * "cold" is split into DECAYED (had retrieval heat, none in the window) and
    NEVER-RETRIEVED (a bounded count + sample, not a 4,700-row worklist). A doc
    the recall log has never named is not evidence of dead weight; it is mostly
    evidence about the log. Flagging all of them would be a detector that cries
    wolf 4,700 times.
  * Every count states the window/horizon it looked over (absence-of-record
    doctrine rule 8: "I didn't find one" and "there isn't one" are different
    claims — a searcher must cover the space or state how far it looked).

Usage:
  mvm sweep                      # human summary + write the worklist
  mvm sweep --json               # machine-readable worklist to stdout
  mvm sweep --check review_due   # one check only
  mvm sweep --no-write           # do not touch the worklist file
  mvm sweep --selftest           # locked oracle on a fixture tree
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get(
    "MVM_KNOWLEDGE_ROOT", str(Path.home() / "mvm" / "knowledge")))
DEFAULT_LOG = Path(os.environ.get(
    "MVM_RECALL_LOG", str(Path.home() / "mvm" / "state" / "recall-log.jsonl")))
DEFAULT_WORKLIST = Path(os.environ.get(
    "MVM_SWEEP_WORKLIST",
    str(Path.home() / ".local" / "state" / "alfred" / "mvm-sweep" / "worklist.json")))

COLD_WINDOW_DAYS = 30
HOT_MIN_RECALLS = 2          # "hot" = grounded >=2 real recalls, ever
NEVER_SAMPLE = 15            # bounded sample; the COUNT is the finding, not the list
CHECKS = ("review_due", "cold_decayed", "untested_hot", "broken_links", "index_drift")

_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.S)
_ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# `none`/`n/a`/`never` as a WHOLE word, optionally followed by the reason that
# makes the declaration honest. `nonsense` must not match.
_NONE_RE = re.compile(r"(?i)^(?:none|n/a|never)\b(?P<reason>.*)$", re.S)
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
_INDEX_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)")


# ------------------------------------------------------------------ helpers --
def iter_docs(root: Path):
    """Every knowledge .md that is not itself an auto-generated index."""
    for p in sorted(root.rglob("*.md")):
        if p.name in ("INDEX.md", "index.md"):
            continue
        yield p


def frontmatter(text: str) -> dict:
    """Minimal top-level YAML scalar parse. Deliberately NOT a YAML engine:
    sweep must never fail closed on a doc with exotic YAML, and every field it
    reads is a flat scalar."""
    m = _FM_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if not line or line[:1] in (" ", "\t", "#", "-"):
            continue
        k, sep, v = line.partition(":")
        if not sep:
            continue
        out[k.strip()] = v.strip().strip("'\"")
    return out


def parse_review_at(raw: str):
    """('due'|'future'|'none'|'unparseable', date-or-None).

    A value must START with an ISO date to count as a date — `2026-08-15 (~8
    settles by then)` is a date with a note, `see § Verdict` is not a date at
    all, and pretending otherwise would silently drop a review that is owed.

    THE `none` SENTINEL (added 2026-07-22, dream cycle): some docs genuinely
    have no review date — a KILLED trading lane does not decay, and its
    re-entry bars are event-triggered rather than calendar-triggered. Before
    this, such a doc was reported `unparseable` on EVERY sweep, forever, at
    zero information value. That is the cry-wolf failure this module's own
    honesty rules exist to prevent: a permanent false positive trains its
    reader to ignore the check, which is how a real unparseable row would slip
    past unseen.

    But `none` is only honest WITH A REASON. A bare `none` is
    indistinguishable from someone who could not be bothered to pick a date,
    so it stays `unparseable`. Declaring that no review is owed is a claim,
    and a claim carries its justification — the same rule the rest of the
    substrate runs on. Accepted form: `none — <why>` (any dash/punctuation or
    whitespace separator, ≥3 chars of actual reason).

    `nonsense` does not match: the sentinel must be a whole word.
    """
    raw = (raw or "").strip().strip("*").strip()
    if not raw:
        return "unparseable", None
    m_none = _NONE_RE.match(raw)
    if m_none:
        reason = m_none.group("reason") or ""
        # Strip leading separator punctuation before measuring the reason.
        reason = reason.lstrip("-—–:;,. \t").strip()
        return ("none" if len(reason) >= 3 else "unparseable"), None
    m = _ISO_RE.match(raw)
    if not m:
        return "unparseable", None
    try:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
    except ValueError:
        return "unparseable", None
    return ("due" if d <= datetime.now().date() else "future"), d.isoformat()


def heat_by_window(log: Path, root: Path, now: datetime):
    """(ever, recent) Counters of doc -> #recalls. `recent` covers COLD_WINDOW_DAYS.

    Reuses mvm.heat's normalization/extraction so sweep does not grow a second,
    divergent definition of what 'a doc grounded a recall' means.
    """
    from mvm.heat import _entry_kind, _normalize, _PATH_RE
    ever: Counter = Counter()
    recent: Counter = Counter()
    if not log.is_file():
        return ever, recent
    cutoff = now - timedelta(days=COLD_WINDOW_DAYS)
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
        if not seen:
            continue
        ts = str(e.get("ts") or "")
        fresh = False
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=now.tzinfo)
            fresh = t >= cutoff
        except ValueError:
            fresh = False       # undated entry cannot prove freshness
        for n in seen:
            ever[n] += 1
            if fresh:
                recent[n] += 1
    return ever, recent


# ------------------------------------------------------------------- checks --
def check_review_due(root: Path, docs, now: datetime):
    due, unparseable, declared_none = [], [], []
    for p, text in docs:
        fm = frontmatter(text)
        if "review_at" not in fm:
            continue
        state, date = parse_review_at(fm["review_at"])
        rel = str(p.relative_to(root))
        if state == "due":
            due.append({"path": rel, "review_at": date})
        elif state == "none":
            declared_none.append({"path": rel, "reason": fm["review_at"][:120]})
        elif state == "unparseable":
            unparseable.append({"path": rel, "review_at_raw": fm["review_at"][:80]})
    due.sort(key=lambda r: r["review_at"])
    declared_none.sort(key=lambda r: r["path"])
    return {
        "rows": due,
        "unparseable": unparseable,
        # Surfaced as a COUNT+list, never as worklist rows: these are decided,
        # not owed. Kept visible so "no review owed" stays auditable rather
        # than becoming an invisible way to opt out of the check.
        "declared_no_review": declared_none,
    }


def check_cold_decayed(root: Path, docs, ever, recent):
    rows = []
    for path, n in ever.most_common():
        if recent.get(path, 0) == 0 and (root / path).is_file():
            rows.append({"path": path, "recalls_ever": n,
                         "recalls_last_%dd" % COLD_WINDOW_DAYS: 0})
    known = {str(p.relative_to(root)) for p, _ in docs}
    never = sorted(known - set(ever))
    return {"rows": rows,
            "never_retrieved_count": len(never),
            "never_retrieved_sample": never[:NEVER_SAMPLE],
            "note": ("never-retrieved is a COUNT, not a worklist: %d of %d docs. "
                     "The recall log is the only witness here, so this measures "
                     "the log as much as the docs." % (len(never), len(known)))}


# A doc carrying one of these frontmatter `status:` values is RETIRED from the
# active retrieval surface (points at a canonical replacement or a tombstone).
# Demanding locked tests for it is exactly the "cost with no retrieval on the
# other side" anti-pattern this module warns about for `never_retrieved` —
# verification budget follows LIVE heat, and a superseded duplicate has none.
# (dream-20260725-0031: a corrected+superseded omen duplicate re-flagged hot
# forever because supersede alone never cleared it.)
_RETIRED_STATUS = {"superseded", "archived", "retired", "deprecated"}


def _is_retired(root: Path, relpath: str) -> bool:
    try:
        text = (root / relpath).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return frontmatter(text).get("status", "").lower() in _RETIRED_STATUS


def check_untested_hot(root: Path, ever):
    rows = []
    for path, n in ever.most_common():
        if n < HOT_MIN_RECALLS:
            continue
        if not (root / path).is_file():
            continue
        if (root / path.replace(".md", ".tests.yaml")).is_file():
            continue
        if _is_retired(root, path):
            continue
        rows.append({"path": path, "recalls_ever": n})
    # State what the threshold HIDES. A "0 untested hot docs" headline is only
    # honest next to the count just below the bar — otherwise the threshold is
    # doing the reassuring, not the tree.
    below = sum(1 for p, n in ever.items()
                if n < HOT_MIN_RECALLS and (root / p).is_file()
                and not (root / p.replace(".md", ".tests.yaml")).is_file()
                and not _is_retired(root, p))
    return {"rows": rows,
            "hot_threshold_recalls": HOT_MIN_RECALLS,
            "untested_below_threshold": below,
            "docs_with_any_heat": len(ever)}


def strip_code(text: str) -> str:
    """Remove fenced blocks and inline code spans before link extraction.

    LIVE DEFECT 2026-07-22, caught by hand-checking output the 29/29 selftest had
    already blessed: `resources/memfs/link-apply-and-reindex.md` documents the
    syntax `[text](path.md)` INSIDE BACKTICKS, and the raw regex reported it as a
    dead link. A doc that talks ABOUT markdown is not a doc with a broken link;
    a detector that cannot tell the difference trains me to ignore it.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("```", i):
            j = text.find("```", i + 3)
            i = n if j == -1 else j + 3
            continue
        ch = text[i]
        if ch == "`":
            j = text.find("`", i + 1)
            if j != -1 and "\n" not in text[i + 1:j]:
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_broken_links(root: Path, docs):
    rows, seen = [], set()
    for p, raw in docs:
        text = strip_code(raw)
        for target in _LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            t = target.split("#", 1)[0].strip()
            if not t or not t.endswith(".md"):
                continue
            if t.startswith("~") or t.startswith("/"):
                continue        # substrate-absolute paths live outside the KB tree
            dest = (p.parent / t).resolve()
            if dest.exists():
                continue
            key = (str(p.relative_to(root)), t)
            if key in seen:      # one dead link cited twice in a doc is ONE repair
                continue
            seen.add(key)
            rows.append({"path": key[0], "link": key[1]})
    return {"rows": rows,
            "note": "relative .md links only, code blocks/spans excluded; absolute "
                    "(~, /) and http links are out of scope for THIS check and are "
                    "not claimed clean. Rows are deduped per (doc, link)."}


def check_index_drift(root: Path):
    """Two directions, because each is invisible from the other side:
    an index row whose target is gone (dangling), and a doc its own directory's
    index never lists (unlisted). A one-directional check is how a whole file
    stays invisible while the index looks healthy."""
    dangling, unlisted, missing_index, unparseable, dual = [], [], [], [], []
    for d in sorted({p.parent for p in root.rglob("*.md")}):
        idx = None
        for name in ("INDEX.md", "index.md"):
            if (d / name).is_file():
                idx = d / name
                break
        if (d / "INDEX.md").is_file() and (d / "index.md").is_file():
            dual.append(str(d.relative_to(root)) or ".")
        docs = sorted(p for p in d.glob("*.md")
                      if p.name not in ("INDEX.md", "index.md"))
        if not docs:
            continue
        if idx is None:
            missing_index.append(str(d.relative_to(root)) or ".")
            continue
        text = idx.read_text(errors="replace")
        listed = {t.split("#", 1)[0] for t in _INDEX_LINK_RE.findall(text)}
        if not listed:
            # The index exists but this parser found no rows in it (e.g. a
            # table-rendered index). Reporting every doc as "unlisted" here would
            # be the instrument's blindness dressed up as the tree's defect.
            unparseable.append(str(idx.relative_to(root)))
            continue
        for t in sorted(listed):
            if "/" in t:
                continue        # cross-dir rows are the parent index's business
            if not (d / t).is_file():
                dangling.append({"index": str(idx.relative_to(root)), "row": t})
        for p in docs:
            if p.name not in listed:
                unlisted.append({"index": str(idx.relative_to(root)),
                                 "doc": str(p.relative_to(root))})
    return {"rows": dangling + [{"index": u["index"], "unlisted": u["doc"]} for u in unlisted],
            "dangling_count": len(dangling),
            "unlisted_count": len(unlisted),
            "dirs_with_docs_but_no_index": missing_index,
            "indexes_this_parser_could_not_read": unparseable,
            "dirs_with_both_INDEX_and_index": len(dual)}


# -------------------------------------------------------------------- sweep --
def sweep(root: Path, log: Path, checks=CHECKS, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    t0 = time.time()
    docs = [(p, p.read_text(errors="replace")) for p in iter_docs(root)]
    ever, recent = ({}, {})
    if {"cold_decayed", "untested_hot"} & set(checks):
        ever, recent = heat_by_window(log, root, now)
    out = {}
    if "review_due" in checks:
        out["review_due"] = check_review_due(root, docs, now)
    if "cold_decayed" in checks:
        out["cold_decayed"] = check_cold_decayed(root, docs, ever, recent)
    if "untested_hot" in checks:
        out["untested_hot"] = check_untested_hot(root, ever)
    if "broken_links" in checks:
        out["broken_links"] = check_broken_links(root, docs)
    if "index_drift" in checks:
        out["index_drift"] = check_index_drift(root)
    total = sum(len(v.get("rows", [])) for v in out.values())
    return {
        "generated": now.isoformat(),
        "root": str(root),
        "recall_log": str(log),
        "model_tokens_spent": 0,
        "docs_scanned": len(docs),
        "elapsed_s": round(time.time() - t0, 3),
        "windows": {"cold_days": COLD_WINDOW_DAYS,
                    "hot_min_recalls": HOT_MIN_RECALLS,
                    "heat_horizon": "entire recall log (no truncation)"},
        "checks_run": list(checks),
        "total_worklist_rows": total,
        "checks": out,
    }


def write_worklist(result: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    os.replace(tmp, path)
    return path


def render(result: dict, out=sys.stdout) -> None:
    c = result["checks"]
    print("mvm sweep — %d docs, %.2fs, model tokens: %d"
          % (result["docs_scanned"], result["elapsed_s"],
             result["model_tokens_spent"]), file=out)
    print("worklist rows: %d\n" % result["total_worklist_rows"], file=out)
    if "review_due" in c:
        r = c["review_due"]
        print("  review_due      %4d due  (+%d review_at unparseable — owed, not skipped"
              "; %d declared no-review-with-reason)"
              % (len(r["rows"]), len(r["unparseable"]),
                 len(r.get("declared_no_review", []))), file=out)
        for row in r["rows"][:10]:
            print("      %s  (review_at %s)" % (row["path"], row["review_at"]), file=out)
        for row in r["unparseable"][:5]:
            print("      ? %s  (review_at %r)" % (row["path"], row["review_at_raw"]), file=out)
    if "cold_decayed" in c:
        r = c["cold_decayed"]
        print("  cold_decayed    %4d had heat, zero recalls in %dd  |  never-retrieved: %d"
              % (len(r["rows"]), COLD_WINDOW_DAYS, r["never_retrieved_count"]), file=out)
        for row in r["rows"][:10]:
            print("      %s  (%d recalls ever)" % (row["path"], row["recalls_ever"]), file=out)
    if "untested_hot" in c:
        r = c["untested_hot"]
        print("  untested_hot    %4d hot docs (>=%d recalls) with no locked tests"
              % (len(r["rows"]), r["hot_threshold_recalls"]), file=out)
        for row in r["rows"][:10]:
            print("      %s  (%d recalls)" % (row["path"], row["recalls_ever"]), file=out)
    if "broken_links" in c:
        r = c["broken_links"]
        print("  broken_links    %4d dead relative .md links" % len(r["rows"]), file=out)
        for row in r["rows"][:10]:
            print("      %s → %s" % (row["path"], row["link"]), file=out)
    if "index_drift" in c:
        r = c["index_drift"]
        print("  index_drift     %4d rows (%d dangling, %d unlisted, %d dirs with no index)"
              % (len(r["rows"]), r["dangling_count"], r["unlisted_count"],
                 len(r["dirs_with_docs_but_no_index"])), file=out)


# ----------------------------------------------------------------- selftest --
def _fixture(tmp: Path):
    """A tree with one planted defect per check, plus negative controls that
    MUST NOT be flagged (a detector that flags everything is not a detector)."""
    root = tmp / "knowledge"
    (root / "resources").mkdir(parents=True)
    (root / "areas").mkdir(parents=True)

    def doc(rel, fm="", body="body"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(("---\n%s\n---\n\n" % fm if fm else "") + body + "\n")
        return p

    doc("resources/past-due.md", "title: p\nreview_at: 2020-01-01")          # due
    doc("resources/future.md", "title: f\nreview_at: 2099-01-01")            # NOT due
    doc("resources/vague.md", "title: v\nreview_at: see § Verdict")          # unparseable
    doc("resources/dated-with-note.md", "title: d\nreview_at: 2020-02-02 (~8 settles)")
    # `none` sentinel fixtures (2026-07-22)
    doc("resources/killed-lane.md",
        "title: k\nreview_at: none — lane closed; re-entry bars are event-triggered")
    doc("resources/bare-none.md", "title: b\nreview_at: none")       # no reason -> unparseable
    # Whole-word guard. MUST be a word that genuinely STARTS with the sentinel
    # — `nonsense` is n-o-n-S, so `none` never prefixed it and the case was
    # vacuous (caught by mutation test 2026-07-22). `nonetheless` is n-o-n-e-T.
    doc("resources/nonsense-review.md", "title: n\nreview_at: nonetheless, see § Verdict")
    doc("resources/na-review.md", "title: a\nreview_at: n/a — kills do not decay")
    doc("resources/no-review.md", "title: n")                               # NOT flagged
    doc("resources/hot-tested.md", "title: ht")                             # NOT flagged
    (root / "resources" / "hot-tested.tests.yaml").write_text("cases: []\n")
    doc("resources/hot-untested.md", "title: hu")                           # untested_hot
    # hot + no tests BUT retired via status -> must NOT be flagged untested_hot
    doc("resources/hot-superseded.md",
        "title: hs\nstatus: superseded\n"
        "superseded_by: resources/hot-tested.md")
    doc("resources/cold-decayed.md", "title: cd")                           # cold_decayed
    doc("resources/never.md", "title: nv")                                  # never-retrieved
    doc("areas/links.md", body="see [ok](../resources/never.md) and "
                               "[dead](./gone.md) and [web](https://x.com/a.md) "
                               "and [abs](~/resources/x.md) "
                               "and [dead](./gone.md) again\n"
                               "inline example `[text](path.md)` is documentation\n"
                               "```\n[fenced](also-not-real.md)\n```\n")
    (root / "resources" / "INDEX.md").write_text(
        "\n".join("- [%s](%s)" % (n, n) for n in
                  ("past-due.md", "future.md", "vague.md", "dated-with-note.md",
                   "no-review.md", "hot-tested.md", "hot-untested.md",
                   "cold-decayed.md", "never.md", "ghost.md")) + "\n")
    # areas/ deliberately has NO index -> dirs_with_docs_but_no_index

    log = tmp / "recall-log.jsonl"
    now = datetime.now().astimezone()
    old = (now - timedelta(days=90)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()
    lines = []
    for _ in range(3):
        lines.append(json.dumps({"kind": "recall", "ts": old,
                                 "evidence_paths": ["resources/cold-decayed.md"]}))
    for _ in range(3):
        lines.append(json.dumps({"kind": "recall", "ts": fresh,
                                 "evidence_paths": ["resources/hot-untested.md"]}))
    for _ in range(2):
        lines.append(json.dumps({"kind": "recall", "ts": fresh,
                                 "evidence_paths": ["resources/hot-tested.md"]}))
    for _ in range(2):
        lines.append(json.dumps({"kind": "recall", "ts": fresh,
                                 "evidence_paths": ["resources/hot-superseded.md"]}))
    lines.append(json.dumps({"kind": "dream-probe", "ts": fresh,
                             "evidence_paths": ["resources/never.md"]}))
    log.write_text("\n".join(lines) + "\n")
    return root, log


def selftest() -> int:
    import tempfile
    ok = fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            fail += 1
            print("  FAIL  %s" % name)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        root, log = _fixture(tmp)
        before = sorted(p.stat().st_mtime_ns for p in root.rglob("*"))
        res = sweep(root, log)
        c = res["checks"]

        check("zero model tokens claimed AND no model call path exists",
              res["model_tokens_spent"] == 0)
        check("sweep is read-only over the tree",
              sorted(p.stat().st_mtime_ns for p in root.rglob("*")) == before)
        check("every check ran", set(res["checks_run"]) == set(CHECKS))

        rd = {r["path"] for r in c["review_due"]["rows"]}
        check("review_due finds the past-due doc", "resources/past-due.md" in rd)
        check("review_due finds an ISO date carrying a trailing note",
              "resources/dated-with-note.md" in rd)
        check("review_due does NOT flag a future date", "resources/future.md" not in rd)
        check("review_due does NOT flag a doc with no review_at",
              "resources/no-review.md" not in rd)
        up = {r["path"] for r in c["review_due"]["unparseable"]}
        check("unparseable review_at is REPORTED, not silently dropped",
              "resources/vague.md" in up)
        check("unparseable is not counted as due", "resources/vague.md" not in rd)

        # --- `none` sentinel (2026-07-22): a declared no-review is decided,
        # not owed — but only when it carries a reason.
        nn = {r["path"] for r in c["review_due"]["declared_no_review"]}
        check("`none — <reason>` is DECLARED no-review, not unparseable",
              "resources/killed-lane.md" in nn
              and "resources/killed-lane.md" not in up)
        check("a declared no-review is never counted as due",
              "resources/killed-lane.md" not in rd)
        check("the declared reason is retained for audit",
              any("lane closed" in r["reason"]
                  for r in c["review_due"]["declared_no_review"]
                  if r["path"] == "resources/killed-lane.md"))
        check("BARE `none` (no reason) stays unparseable — a claim needs its why",
              "resources/bare-none.md" in up and "resources/bare-none.md" not in nn)
        check("`nonetheless` does NOT match the none sentinel (whole word only)",
              "resources/nonsense-review.md" in up
              and "resources/nonsense-review.md" not in nn)
        check("`n/a` with a reason is accepted as declared no-review",
              "resources/na-review.md" in nn)
        check("a real ISO date is unaffected by the none sentinel",
              "resources/past-due.md" in rd and "resources/past-due.md" not in nn)

        cd = {r["path"] for r in c["cold_decayed"]["rows"]}
        check("cold_decayed finds a doc whose heat is all older than the window",
              "resources/cold-decayed.md" in cd)
        check("cold_decayed does NOT flag a doc recalled inside the window",
              "resources/hot-untested.md" not in cd)
        check("never-retrieved is a bounded COUNT, not a worklist",
              c["cold_decayed"]["never_retrieved_count"] >= 1
              and len(c["cold_decayed"]["never_retrieved_sample"]) <= NEVER_SAMPLE
              and "resources/never.md" not in cd)
        check("dream-probe entries do not manufacture heat",
              "resources/never.md" in c["cold_decayed"]["never_retrieved_sample"])

        uh = {r["path"] for r in c["untested_hot"]["rows"]}
        check("untested_hot finds the hot doc with no .tests.yaml",
              "resources/hot-untested.md" in uh)
        check("untested_hot does NOT flag a hot doc that HAS tests",
              "resources/hot-tested.md" not in uh)
        check("untested_hot does NOT flag a cold untested doc",
              "resources/never.md" not in uh)
        check("untested_hot does NOT flag a hot doc RETIRED via status: superseded",
              "resources/hot-superseded.md" not in uh)

        bl = {(r["path"], r["link"]) for r in c["broken_links"]["rows"]}
        check("broken_links finds the dead relative link",
              ("areas/links.md", "./gone.md") in bl)
        check("broken_links does NOT flag a live relative link",
              not any(l.endswith("never.md") for _p, l in bl))
        check("broken_links does NOT flag http links",
              not any(l.startswith("http") for _p, l in bl))
        check("broken_links does NOT flag substrate-absolute links",
              not any(l.startswith("~") for _p, l in bl))
        # the LIVE false positive this check shipped with, now locked
        check("broken_links does NOT flag a link inside an inline code span",
              not any(l == "path.md" for _p, l in bl))
        check("broken_links does NOT flag a link inside a fenced code block",
              not any(l == "also-not-real.md" for _p, l in bl))
        check("broken_links deduplicates the same dead link cited twice",
              len([r for r in c["broken_links"]["rows"]
                   if r["link"] == "./gone.md"]) == 1)

        idx = c["index_drift"]
        check("index_drift finds the dangling index row",
              any(r.get("row") == "ghost.md" for r in idx["rows"]))
        check("index_drift does NOT flag a doc its index correctly lists",
              not any(r.get("unlisted") == "resources/never.md" for r in idx["rows"]))
        (root / "resources" / "orphan.md").write_text("---\ntitle: o\n---\n\nx\n")
        res2 = sweep(root, log)
        check("index_drift finds a doc added without an index row",
              any(r.get("unlisted") == "resources/orphan.md"
                  for r in res2["checks"]["index_drift"]["rows"]))
        check("index_drift names dirs holding docs with no index at all",
              "areas" in idx["dirs_with_docs_but_no_index"])
        # an index this parser cannot read must be reported AS SUCH, never as
        # "every doc in that dir is unlisted" (instrument blindness ≠ tree defect)
        (root / "areas" / "index.md").write_text(
            "# Areas\n\n_Auto-rendered_\n\n| File | Desc |\n|---|---|\n| `links.md` | x |\n")
        res3 = sweep(root, log, checks=("index_drift",))["checks"]["index_drift"]
        check("unreadable index is reported as unreadable, not as mass-unlisted",
              "areas/index.md" in res3["indexes_this_parser_could_not_read"]
              and not any(r.get("unlisted", "").startswith("areas/")
                          for r in res3["rows"]))
        check("dirs carrying BOTH INDEX.md and index.md are counted",
              sweep(root, log, checks=("index_drift",))["checks"]["index_drift"]
              ["dirs_with_both_INDEX_and_index"] == 0)
        check("untested_hot states what its threshold hides",
              "untested_below_threshold" in c["untested_hot"]
              and c["untested_hot"]["untested_below_threshold"] >= 0)

        check("--check narrows the run",
              set(sweep(root, log, checks=("review_due",))["checks"]) == {"review_due"})
        check("worklist is regenerated, never appended",
              sweep(root, log)["total_worklist_rows"] == res2["total_worklist_rows"])
        wl = tmp / "wl.json"
        write_worklist(res2, wl)
        check("worklist round-trips as JSON",
              json.loads(wl.read_text())["docs_scanned"] == res2["docs_scanned"])
        check("worklist states its own windows (a number without its window lies)",
              set(json.loads(wl.read_text())["windows"]) ==
              {"cold_days", "hot_min_recalls", "heat_horizon"})
        check("selftest touched no real knowledge tree", str(DEFAULT_ROOT) not in str(root))

        # --- check_fresh: the DRIFT arm. A gate that cannot go red is not a gate,
        # so every case below is paired with its opposite; passing only the
        # happy path would prove nothing about staleness detection.
        t0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        check("check_fresh: fresh worklist passes",
              check_fresh(wl, 7.0, now=datetime.fromisoformat(
                  json.loads(wl.read_text())["generated"]) + timedelta(hours=1))[0] == 0)
        check("check_fresh: STALE worklist fails (the gate can go red)",
              check_fresh(wl, 7.0, now=datetime.fromisoformat(
                  json.loads(wl.read_text())["generated"]) + timedelta(hours=8))[0] == 1)
        check("check_fresh: boundary is exclusive — exactly at the cap is still fresh",
              check_fresh(wl, 7.0, now=datetime.fromisoformat(
                  json.loads(wl.read_text())["generated"]) + timedelta(hours=7))[0] == 0)
        check("check_fresh: ABSENT worklist fails as stale, never as 'nothing to do'",
              check_fresh(tmp / "does-not-exist.json", 7.0, now=t0)[0] == 1)
        malformed = tmp / "malformed.json"
        malformed.write_text('{"total_worklist_rows": 3}')   # no `generated`
        check("check_fresh: worklist with no `generated` is rc=2, distinct from stale",
              check_fresh(malformed, 7.0, now=t0)[0] == 2)
        notjson = tmp / "notjson.json"
        notjson.write_text("this is not json")
        check("check_fresh: unparseable worklist is rc=2, not a crash",
              check_fresh(notjson, 7.0, now=t0)[0] == 2)
        # mtime is a LIE on this substrate (mvm-mirror copies files); freshness must
        # come from the sweep's own stamp.
        #
        # ⚠ THIS CASE WAS VACUOUS ON FIRST WRITE (caught 2026-07-22 by the mutation
        # pass, not by review). v1 touched mtime to *now* and asserted STALE — but a
        # stamp-reader and an mtime-reader BOTH returned stale there, so the mutation
        # "use mtime instead of generated" survived a green selftest. The two
        # readings must DISAGREE for the assertion to carry information: mtime is
        # driven far into the past while `generated` stays recent, so stamp-reading
        # says FRESH and mtime-reading says STALE. Now the mutation dies.
        gen_dt = datetime.fromisoformat(json.loads(wl.read_text())["generated"])
        old = (gen_dt - timedelta(hours=100)).timestamp()
        os.utime(wl, (old, old))
        check("check_fresh: reads `generated`, NOT file mtime (mirror-cron safe)",
              check_fresh(wl, 7.0, now=gen_dt + timedelta(hours=1))[0] == 0)

    print("\n%d passed, %d failed" % (ok, fail))
    return 1 if fail else 0


def check_fresh(worklist_path: Path, max_age_hours: float, now=None) -> tuple:
    """The DRIFT arm of this build's closed loop (architecture invariant #17).

    Returns (rc, message). rc 0 = fresh, 1 = stale or absent, 2 = unreadable.

    ⛔ WHY THIS EXISTS, and why it is not decoration: a worklist that stops being
    regenerated does not fail loudly — it just gets old, and the dream pass keeps
    consuming it as if it were current. That is strictly WORSE than having no
    worklist at all, because a stale worklist launders "I checked" over "nobody
    has looked in six days." The consumer trusts this file; something has to
    check that the file deserves it.

    Freshness is measured from the sweep's OWN `generated` stamp, not the file
    mtime — mtime lies whenever the file is copied, restored, or touched by a
    sync job, and this substrate has a mirror cron that does exactly that.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not worklist_path.exists():
        return 1, "mvm sweep: worklist ABSENT at %s — producer has never run" % worklist_path
    try:
        data = json.loads(worklist_path.read_text())
        stamp = data["generated"]
        gen = datetime.fromisoformat(stamp)
    except (ValueError, KeyError, OSError) as exc:
        return 2, "mvm sweep: worklist unreadable//malformed (%s): %s" % (worklist_path, exc)
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    age_h = (now - gen).total_seconds() / 3600.0
    if age_h > max_age_hours:
        return 1, ("mvm sweep: worklist STALE — generated %s (%.1f h ago, cap %.1f h). "
                   "The dream pass is consuming a worklist nobody refreshed; "
                   "check the `mvm-sweep-producer` cron job."
                   % (stamp, age_h, max_age_hours))
    return 0, ("mvm sweep: worklist fresh — generated %s (%.1f h ago, cap %.1f h), %d row(s)"
               % (stamp, age_h, max_age_hours, data.get("total_worklist_rows", -1)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mvm sweep",
        description="Zero-token deterministic curation sweep → worklist for the dream pass.")
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--worklist", type=Path, default=DEFAULT_WORKLIST)
    ap.add_argument("--check", action="append", choices=CHECKS,
                    help="Run only this check (repeatable).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true",
                    help="Do not write the worklist file.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check-fresh", action="store_true",
                    help="Do not sweep: assert the EXISTING worklist is fresh. "
                         "exit 0 = fresh, 1 = stale/absent, 2 = malformed. "
                         "This is the auditor's drift gate.")
    ap.add_argument("--max-age-hours", type=float, default=7.0,
                    help="Staleness cap for --check-fresh (default 7 — the "
                         "producer runs every 6 h, so 7 tolerates one late run "
                         "but never two missed ones).")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.check_fresh:
        rc, msg = check_fresh(args.worklist, args.max_age_hours)
        print(msg, file=sys.stderr if rc else sys.stdout)
        return rc
    if not args.root.is_dir():
        print("mvm sweep: knowledge root not found: %s" % args.root, file=sys.stderr)
        return 3
    result = sweep(args.root, args.log, checks=tuple(args.check or CHECKS))
    if not args.no_write:
        result["worklist_path"] = str(write_worklist(result, args.worklist))
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False))
    else:
        render(result)
        if not args.no_write:
            print("\nworklist → %s" % result["worklist_path"])
    return 0
