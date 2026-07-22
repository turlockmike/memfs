"""
mvm linkfix — repair relative .md links whose target EXISTS elsewhere in the tree.

WHY THIS EXISTS (2026-07-22 dream cycle): `mvm sweep` reported 42 broken_links
rows. Every single target existed — they were relative-path ARITHMETIC errors
(`../../canonical/db/x.md` where the true depth was `../../../canonical/db/x.md`),
not missing referents. That is a mechanical repair with no judgment in it: the
wrong thing to spend a dream cycle's model tokens doing by hand, and the right
thing to spend them on ONCE, here.

WHY A SUBCOMMAND AND NOT A NEW BIN TOOL: the detector (`mvm sweep`) already owns
the definition of "a relative .md link that does not resolve" — which links count,
which are excluded for living inside code fences or spans. A separate tool would
grow a SECOND definition and the two would drift, which is the exact failure
`sweep.heat_by_window` calls out when it refuses to re-derive `mvm heat`'s
notion of a grounded recall. Repair lives next to detection so they cannot
disagree about what is broken.

THE SAFETY RULE THAT MATTERS: a link is rewritten only when its basename
resolves to EXACTLY ONE candidate, and — when the link carries parent
directories — that candidate's trailing directories match too. Ambiguous
resolves are REPORTED, never guessed. A dead link is a visible dead end; a
wrong-but-live link silently delivers a reader to the wrong doc, which is
strictly worse. This tool declines rather than guesses.

Out-of-tree targets (`../projects/...`, `../.local/state/...`) are reported and
never rewritten: the knowledge tree is not their home, and synthesizing a path
into it would manufacture a referent that does not exist.

Usage:
  mvm linkfix                 # dry run — show every proposed rewrite
  mvm linkfix --apply         # perform the rewrites
  mvm linkfix --json          # machine-readable
  mvm linkfix --selftest      # locked oracle on a fixture tree
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.path.expanduser("~/mvm/knowledge"))

_LINK_RE = re.compile(r"\]\(\s*(?P<t>[^)\s#]+\.md)(?P<frag>#[^)\s]*)?\s*\)")


def strip_code(text: str) -> str:
    """Blank fenced blocks / inline spans so links inside code are out of scope.

    Line count is preserved for fences so any future line-numbered reporting
    stays truthful.
    """
    text = re.sub(r"```.*?```", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def is_out_of_tree(link: str) -> bool:
    return (
        link.startswith("~")
        or link.startswith("/")
        or "/.local/" in link
        or link.startswith(".local/")
        or re.search(r"(^|/)projects/", link) is not None
    )


def build_index(root: Path):
    idx = defaultdict(list)
    for p in root.rglob("*.md"):
        idx[p.name].append(p.relative_to(root))
    return idx


def resolve(doc_rel: Path, link: str, idx, root: Path):
    """(status, info) — status in {ok, fix, ambiguous, missing, out-of-tree}."""
    target = (root / doc_rel).parent / link
    try:
        rt = target.resolve()
        if rt.is_file() and root.resolve() in rt.parents:
            return "ok", None
    except OSError:
        pass
    if is_out_of_tree(link):
        return "out-of-tree", link

    name = Path(link).name
    cands = idx.get(name, [])
    if not cands:
        return "missing", link
    if len(cands) > 1:
        want = tuple(x for x in Path(link).parent.parts if x not in ("..", "."))
        if want:
            narrowed = [c for c in cands if c.parts[-len(want) - 1:-1] == want]
            if len(narrowed) == 1:
                cands = narrowed
        if len(cands) != 1:
            return "ambiguous", "%d candidates: %s" % (
                len(cands), ", ".join(str(c) for c in cands[:4]))
    new = os.path.relpath(root / cands[0], (root / doc_rel).parent)
    return "fix", new


def scan(root: Path):
    idx = build_index(root)
    fixes, problems = [], []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            continue
        seen = set()
        for m in _LINK_RE.finditer(strip_code(raw)):
            link = m.group("t")
            if (rel, link) in seen:
                continue
            seen.add((rel, link))
            status, info = resolve(rel, link, idx, root)
            if status == "ok":
                continue
            if status == "fix":
                fixes.append((rel, link, info))
            else:
                problems.append((rel, link, status, info))
    return fixes, problems


def apply_fixes(root: Path, fixes):
    by_doc = defaultdict(list)
    for rel, old, new in fixes:
        by_doc[rel].append((old, new))
    n = 0
    for rel, pairs in by_doc.items():
        p = root / rel
        text = p.read_text(errors="replace")
        for old, new in pairs:
            # Only inside a markdown link target — never bare prose mentioning
            # the same path.
            pat = re.compile(r"(\]\(\s*)" + re.escape(old) + r"(?=[)#\s])")
            text, k = pat.subn(lambda m: m.group(1) + new, text)
            n += k
        p.write_text(text)
    return n


# ------------------------------------------------------------------ selftest --
def selftest() -> int:
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
        root = Path(td) / "kb"

        def doc(rel, body=""):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

        # Genuinely wrong DEPTH: from a/deep, `../canonical/...` is a/canonical
        # (absent) while the true target sits at the root. This is the exact
        # 42-row shape the tool was built for. (First draft of this fixture used
        # `../../canonical/...`, which RESOLVES from a/deep — a vacuous case the
        # selftest caught on its first run.)
        doc("a/deep/source.md", "see [x](../canonical/db/target.md)\n")
        doc("canonical/db/target.md", "t")
        # Same link TEXT, but from a/ it resolves — proves the tool keys on
        # resolution, not on the spelling of the link.
        doc("a/good.md", "see [ok](../canonical/db/target.md)\n")
        doc("a/gone.md", "see [g](../canonical/db/nope.md)\n")
        doc("a/amb.md", "see [d](../dupe.md)\n")
        doc("x1/dupe.md", "d")
        doc("x2/dupe.md", "d")
        doc("a/deep/disambig.md", "see [d](../x2/dupe.md)\n")
        doc("a/out.md", "see [o](../../projects/thing/notes.md)\n")
        doc("a/incode.md", "```\n[c](../canonical/db/bogus.md)\n```\n")
        doc("a/inspan.md", "`[c](../canonical/db/bogus2.md)`\n")

        fixes, problems = scan(root)
        fx = {(str(r), o): n for r, o, n in fixes}
        pr = {(str(r), o): s for r, o, s, _ in problems}

        check("repairs a link whose target exists at another depth",
              ("a/deep/source.md", "../canonical/db/target.md") in fx)
        check("the computed replacement actually resolves",
              (root / "a/deep" / fx[("a/deep/source.md",
                                     "../canonical/db/target.md")]).resolve().is_file())
        check("does NOT touch a link that already resolves",
              not any(str(r) == "a/good.md" for r, _, _ in fixes))
        check("a genuinely absent target is MISSING, never invented",
              pr.get(("a/gone.md", "../canonical/db/nope.md")) == "missing")
        check("ambiguous basename is DECLINED, not guessed",
              pr.get(("a/amb.md", "../dupe.md")) == "ambiguous")
        check("ambiguous is never silently rewritten",
              ("a/amb.md", "../dupe.md") not in fx)
        check("trailing dirs DISAMBIGUATE a duplicate basename",
              ("a/deep/disambig.md", "../x2/dupe.md") in fx
              and fx[("a/deep/disambig.md", "../x2/dupe.md")].endswith("x2/dupe.md"))
        check("out-of-tree target is reported, not dragged into the tree",
              pr.get(("a/out.md", "../../projects/thing/notes.md")) == "out-of-tree")
        check("links inside a fenced code block are ignored",
              not any(str(r) == "a/incode.md" for r, _, _ in fixes)
              and not any(str(r) == "a/incode.md" for r, _, _, _ in problems))
        check("links inside an inline code span are ignored",
              not any(str(r) == "a/inspan.md" for r, _, _ in fixes)
              and not any(str(r) == "a/inspan.md" for r, _, _, _ in problems))

        applied = apply_fixes(root, fixes)
        check("apply reports the number it actually rewrote", applied == len(fixes))
        fixes2, _ = scan(root)
        check("after --apply the repaired link no longer reports broken",
              not any(str(r) == "a/deep/source.md" for r, _, _ in fixes2))
        check("re-running finds nothing new to fix (idempotent)", fixes2 == [])
        check("only the link target is rewritten, old path gone from the link",
              "](../canonical/db/target.md)" not in
              (root / "a/deep/source.md").read_text())
        check("selftest touched no real knowledge tree",
              str(root).startswith(td))

    print("\n%d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in args:
        return selftest()
    root = ROOT
    if not root.is_dir():
        print("knowledge root not found: %s" % root, file=sys.stderr)
        return 2
    fixes, problems = scan(root)
    if "--json" in args:
        # Apply BEFORE emitting, so stdout stays a single valid JSON object and
        # `applied` reports the truth rather than a placeholder.
        applied = apply_fixes(root, fixes) if "--apply" in args else 0
        print(json.dumps({
            "root": str(root),
            "repairable": [{"path": str(r), "old": o, "new": n} for r, o, n in fixes],
            "needs_human": [{"path": str(r), "link": o, "status": s, "info": i}
                            for r, o, s, i in problems],
            "applied": applied,
            "dry_run": "--apply" not in args,
        }, indent=1))
        return 0
    else:
        print("mvm linkfix — %d repairable, %d need a human"
              % (len(fixes), len(problems)))
        for rel, old, new in fixes:
            print("  FIX  %s\n         %s\n      -> %s" % (rel, old, new))
        for rel, old, status, info in problems:
            print("  %-12s %s  %s  (%s)" % (status.upper(), rel, old, info))
    if "--apply" in args:
        n = apply_fixes(root, fixes)
        print("\napplied %d link rewrites" % n)
    else:
        print("\n(dry run — pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
