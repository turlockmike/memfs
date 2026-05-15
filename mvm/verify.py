"""
mvm-verify: cold-clone verification of a markdown doc against its locked tests.

Spawns `claude --print --tools ""` subprocesses for each test:
  1. retriever clone   — gets file content + question; must answer from file alone
  2. grader clone      — gets candidate + expected; judges semantic equivalence

Both subprocesses are constitutionally blind:
  - retriever sees no expected answer
  - grader sees no source file
  - --system-prompt REPLACES the default (no agent persona injected)
  - --tools "" blocks every tool — the clone can only generate
  - --no-session-persistence prevents session reuse

ISOLATION (OAuth-compatible — default path, 2026-05-10 rewrite)
---------------------------------------------------------------
Cold-clones run in a redirected $HOME (a per-call tempdir) with the OAuth
credentials file symlinked through. This severs every $HOME-rooted source of
ambient context — `~/.claude/CLAUDE.md` (user memory), `~/.claude/settings.json`
(hooks, agent= alfred, plugins), `~/.claude/agents/*.md` (agent personas) —
while preserving authentication. The subprocess also runs with cwd inside the
tempdir so the project-CLAUDE.md auto-discovery walk finds no ancestors.

Defensive flags layered on top of HOME redirect:
  - --setting-sources project,local  (skip user-level settings even if HOME
    redirect is bypassed somehow)
  - --strict-mcp-config              (no MCP server auto-load)
  - --disable-slash-commands         (skills cannot run)

Compatible with OAuth-only hosts. No ANTHROPIC_API_KEY required.

LEGACY FAST PATH (opt-in)
-------------------------
Set MVM_FORCE_BARE=1 (and ensure ANTHROPIC_API_KEY is in env) to use
`claude --bare` instead. --bare strictly requires ANTHROPIC_API_KEY (it
refuses OAuth/keychain), so this path only applies on hosts with an API key.
Off by default; the clean-cwd path is the default everywhere.

(MVM_BARE=1 is treated as a deprecated alias for MVM_FORCE_BARE=1.)

Usage:
  mvm-verify <doc.md>                 # run all tests
  mvm-verify <doc.md> --test-id 1     # single test
  mvm-verify <doc.md> --json          # machine-readable output

Test file convention: <doc>.tests.yaml, list of {id, q, a}.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

CLAUDE = os.environ.get("MVM_CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("MVM_VERIFY_MODEL", "haiku")
DEFAULT_TIMEOUT = int(os.environ.get("MVM_VERIFY_TIMEOUT", "120"))

RETRIEVER_SYSTEM = """You are a retrieval clone. You will receive a document and a question.

RULES (follow exactly):
1. Answer ONLY from the provided document content. Do not use prior knowledge.
2. UNANSWERABLE CASE — if the document does not support an answer, your ENTIRE output MUST be the literal 9-character sentinel string:
       DONT-KNOW
   Do NOT paraphrase. Forbidden alternates include (non-exhaustive):
       "I don't know"        "I do not know"
       "Not disclosed"       "Not specified"
       "Not stated"          "Not available"
       "Unknown"             "Cannot determine"
       "The document does not say"
   If you would otherwise emit any of these or similar — output DONT-KNOW instead. The literal string is a contract, not a suggestion.
3. ANSWERABLE CASE — output ONLY the answer. No preamble. No caveats. No markdown. No quotes around the answer."""

GRADER_SYSTEM = """You are a grader. You will receive two answers: a CANDIDATE and an EXPECTED.

Decide whether the CANDIDATE conveys the same factual content as EXPECTED.

CORE PRINCIPLE — match on facts, not on prose.
A "fact" is a distinct atomic claim (a number, name, date, proper noun, identity,
boolean state, or causal relation). Two pieces of text express the same fact
when one cannot be true while the other is false. Restatements, elaborations,
and explanatory rephrasings of the same atomic claim are ONE fact, not multiple.

PASS if the candidate states the same atomic facts as the expected. PASS cases:
  - Paraphrasing, synonyms, different word order.
  - Different capitalization, punctuation, quoting, whitespace.
  - The candidate restates the question's subject noun in the answer
    (e.g., expected "more than 220,000", candidate "more than 220,000 Nvidia
    processors" — PASS, since the noun is just question-context being repeated).
  - Hedge or approximation words are present on one side but absent on the other
    ("approximately", "around", "about", "roughly", "~", "approx.", "nearly",
    "almost") — these are epistemic markers, not facts. Dropping or adding a
    hedge does not change PASS/FAIL.
  - The candidate is more verbose but contains all the expected facts intact.
  - The candidate adds a trivially-true contextual detail that does not contradict.
  - **Elaboration-clause omission.** EXPECTED contains a parenthetical, em-dash,
    colon-introduced, or "i.e./e.g." clause that elaborates the preceding claim
    (restates it, gives an example, or names its mechanism). CANDIDATE conveys
    the core claim but omits the elaboration. PASS. Examples:
      EXPECTED: "single-source: only the biggest single hit lands, no second/third hits"
      CANDIDATE: "single-source, biggest-hit only"
      → PASS — the colon-clause and "no second/third hits" both elaborate "single-source".
      EXPECTED: "Companion-tagged, not minion-tagged (so minion-damage modifiers don't apply)"
      CANDIDATE: "Companion-tagged, not minion-tagged"
      → PASS — the parenthetical elaborates the mechanism, doesn't add a fact.
  - **Same-fact rewording.** EXPECTED uses verbose phrasing; CANDIDATE uses terse
    phrasing that conveys the same atomic claim.
      EXPECTED: "The biggest single hit that landed across all sources is what counts"
      CANDIDATE: "The biggest single hit landed"
      → PASS — same atomic claim about which hit counts.

FAIL if:
  - The candidate gives a different number, name, date, or proper noun.
  - The candidate contradicts the expected.
  - The candidate omits a DISTINCT fact the expected explicitly states. A
    distinct fact is one whose truth value is independent of the others —
    omitting it changes what is being asserted. Test: would a reader who
    received only CANDIDATE learn a materially different thing than a reader
    who received EXPECTED? If no, the omission is elaboration (PASS). If yes,
    it's a distinct fact (FAIL).
      EXPECTED: "Memphis, Tennessee" (city and state are distinct facts)
      CANDIDATE: "Memphis"
      → FAIL — state is independent of city.
      EXPECTED: "Born 1942 in Memphis, Tennessee" (year, city, state — three facts)
      CANDIDATE: "Born in Memphis"
      → FAIL — year and state are distinct facts being omitted.
  - The candidate substitutes a different factual claim for the expected.

When in doubt between elaboration-omission (PASS) and distinct-fact-omission (FAIL),
apply the **materially-different-learner** test above.

Output ONLY one of:
  PASS
  FAIL
No other text. No preamble. No explanation."""


@contextmanager
def isolated_home():
    """Yield (cwd, env) for a contamination-free claude subprocess.

    Strategy: redirect $HOME to a per-call tempdir. Symlink only
    `~/.claude/.credentials.json` through so OAuth still authenticates.
    Everything else under $HOME (CLAUDE.md, settings.json, agents/, sessions/,
    plugins/, hooks/, …) effectively does not exist for the subprocess.

    Project-CLAUDE.md auto-discovery walks up from cwd; setting cwd to the
    tempdir (which lives under /tmp) ensures no CLAUDE.md ancestors exist.
    """
    with tempfile.TemporaryDirectory(prefix="mvm-verify-") as tmphome:
        claude_dir = Path(tmphome) / ".claude"
        claude_dir.mkdir()
        real_creds = Path.home() / ".claude" / ".credentials.json"
        if real_creds.exists():
            (claude_dir / ".credentials.json").symlink_to(real_creds)
        env = {**os.environ, "HOME": tmphome}
        # Strip vars that could re-introduce ambient context.
        for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_AGENT", "CLAUDE_CODE_AGENT"):
            env.pop(k, None)
        yield tmphome, env


def _bare_path_engaged() -> bool:
    """Return True iff the legacy --bare fast path should be used.

    Requires explicit opt-in (MVM_FORCE_BARE=1 or legacy MVM_BARE=1) AND
    a usable ANTHROPIC_API_KEY in env (since --bare refuses OAuth).
    """
    forced = (os.environ.get("MVM_FORCE_BARE") == "1"
              or os.environ.get("MVM_BARE") == "1")
    return forced and bool(os.environ.get("ANTHROPIC_API_KEY"))


def claude_subprocess(
    system_prompt: str,
    user_prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Spawn an isolated cold-clone subprocess. Return the response text.

    See module docstring for the isolation strategy. Default path is HOME
    redirect + cwd in tempdir + defensive flags; works on OAuth-only hosts.
    Fast path (--bare) is opt-in via MVM_FORCE_BARE=1 and requires
    ANTHROPIC_API_KEY.
    """
    base_cmd = [
        CLAUDE,
        "--print",
        "--no-session-persistence",
        "--tools", "",
        "--model", model,
        "--system-prompt", system_prompt,
    ]

    if _bare_path_engaged():
        # Legacy fast path. Trust --bare to handle isolation.
        # --bare placed right after --print to match `claude --print --bare ...` docs.
        cmd = [CLAUDE, "--print", "--bare", "--no-session-persistence",
               "--tools", "", "--model", model,
               "--system-prompt", system_prompt, user_prompt]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    else:
        # Default path: clean-HOME + clean-cwd + defensive flags.
        cmd = base_cmd + [
            "--setting-sources", "project,local",
            "--strict-mcp-config",
            "--disable-slash-commands",
            user_prompt,
        ]
        with isolated_home() as (cwd, env):
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
                env=env,
            )

    if result.returncode != 0:
        raise RuntimeError(
            f"claude subprocess failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


NAKED_RETRIEVER_SYSTEM = """You are answering from your prior knowledge alone.
You have no document, no tools, no internet.

RULES (follow exactly):
1. Answer ONLY from your prior knowledge. Do not invoke tools (you have none).
2. UNANSWERABLE CASE — if you do not know the answer, your ENTIRE output MUST be the literal 9-character sentinel string:
       DONT-KNOW
   Do NOT paraphrase. Forbidden alternates include (non-exhaustive):
       "I don't know"        "I do not know"
       "Not disclosed"       "Not specified"
       "Not stated"          "Not available"
       "Unknown"             "Cannot determine"
       "I am not sure"       "I cannot answer"
   If you would otherwise emit any of these or similar — output DONT-KNOW instead. The literal string is a contract, not a suggestion.
3. ANSWERABLE CASE — output ONLY the answer. No preamble. No caveats. No markdown. No quotes around the answer."""


def verify_test(doc_path: Path, test: dict, model: str, mode: str = "injected") -> dict:
    """Run one test against one doc.

    mode='injected'  — file content in prompt, no tools (cold-clone)
    mode='naked'     — no file content, no tools (weight-prior baseline)
    """
    question = test["q"]
    expected = test["a"]

    if mode == "naked":
        retriever_system = NAKED_RETRIEVER_SYSTEM
        retriever_prompt = f"QUESTION: {question}\n\nAnswer from prior knowledge only."
    else:
        doc_content = doc_path.read_text()
        retriever_system = RETRIEVER_SYSTEM
        retriever_prompt = (
            "DOCUMENT:\n"
            "---\n"
            f"{doc_content}\n"
            "---\n\n"
            f"QUESTION: {question}\n\n"
            "Answer from the document only."
        )
    try:
        candidate = claude_subprocess(retriever_system, retriever_prompt, model=model)
    except Exception as e:
        return {
            "id": test.get("id"),
            "mode": mode,
            "question": question,
            "expected": expected,
            "candidate": None,
            "grade": None,
            "passed": False,
            "error": f"retriever-stage: {e}",
        }

    grader_prompt = (
        f"CANDIDATE: {candidate}\n\n"
        f"EXPECTED: {expected}\n\n"
        "Are they semantically equivalent?"
    )
    try:
        grade = claude_subprocess(GRADER_SYSTEM, grader_prompt, model=model)
    except Exception as e:
        return {
            "id": test.get("id"),
            "mode": mode,
            "question": question,
            "expected": expected,
            "candidate": candidate,
            "grade": None,
            "passed": False,
            "error": f"grader-stage: {e}",
        }

    grade_upper = grade.upper()
    passed = "PASS" in grade_upper and "FAIL" not in grade_upper

    return {
        "id": test.get("id"),
        "mode": mode,
        "question": question,
        "expected": expected,
        "candidate": candidate,
        "grade": grade,
        "passed": passed,
    }


def main(argv = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cold-clone verify a markdown doc against locked Q/A tests."
    )
    parser.add_argument("doc", type=Path, help="Path to the markdown doc.")
    parser.add_argument("--test-id", type=int, help="Run only the test with this id.")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model for subprocesses (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--mode",
        default="injected",
        choices=["injected", "naked"],
        help="injected: file content + question (cold-clone). naked: question only (weight prior).",
    )
    parser.add_argument(
        "--lift",
        action="store_true",
        help="Run BOTH naked and injected; report KB lift (Hassabis weight-leakage detector).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    if not args.doc.exists():
        print(f"ERROR: doc not found: {args.doc}", file=sys.stderr)
        return 2

    tests_path = args.doc.with_suffix(".tests.yaml")
    if not tests_path.exists():
        print(f"ERROR: tests file not found: {tests_path}", file=sys.stderr)
        return 2

    tests_data = yaml.safe_load(tests_path.read_text()) or []
    if not isinstance(tests_data, list):
        print(f"ERROR: tests file must be a YAML list: {tests_path}", file=sys.stderr)
        return 2

    if args.test_id is not None:
        tests_data = [t for t in tests_data if t.get("id") == args.test_id]
        if not tests_data:
            print(f"ERROR: no test with id={args.test_id} in {tests_path}", file=sys.stderr)
            return 2

    if args.lift:
        naked_results = [verify_test(args.doc, t, args.model, mode="naked") for t in tests_data]
        injected_results = [verify_test(args.doc, t, args.model, mode="injected") for t in tests_data]
        n_naked = sum(1 for r in naked_results if r["passed"])
        n_injected = sum(1 for r in injected_results if r["passed"])
        n_total = len(tests_data)
        kb_lift = n_injected - n_naked

        if args.json:
            print(json.dumps({
                "doc": str(args.doc),
                "tests": str(tests_path),
                "model": args.model,
                "naked_pass": n_naked,
                "injected_pass": n_injected,
                "total": n_total,
                "kb_lift": kb_lift,
                "naked": naked_results,
                "injected": injected_results,
            }, indent=2))
        else:
            print("=== KB LIFT REPORT ===")
            print(f"Naked    (weights only):       {n_naked}/{n_total}")
            print(f"Injected (file in prompt):     {n_injected}/{n_total}")
            print(f"KB lift  (delta):              {kb_lift:+d}")
            print()
            print("Per-test breakdown:")
            for nr, ir in zip(naked_results, injected_results):
                naked_mark = "PASS" if nr["passed"] else "FAIL"
                inj_mark = "PASS" if ir["passed"] else "FAIL"
                qsnip = (nr["question"] or "")[:60]
                print(f"  id={nr['id']:<3} naked={naked_mark} injected={inj_mark}  {qsnip}")
            print()
            if kb_lift == 0 and n_injected == n_total:
                print("WARNING: kb_lift = 0. Either tests are answerable from weights, or the KB"
                      " adds no information. Review test difficulty.")
            elif kb_lift < 0:
                print("ALERT: injected scored LOWER than naked. KB content may be misleading"
                      " the retriever.")
        return 0 if n_injected == n_total else 1

    results = [verify_test(args.doc, t, args.model, mode=args.mode) for t in tests_data]
    n_pass = sum(1 for r in results if r["passed"])
    n_total = len(results)

    if args.json:
        print(json.dumps({
            "doc": str(args.doc),
            "tests": str(tests_path),
            "model": args.model,
            "mode": args.mode,
            "pass": n_pass,
            "total": n_total,
            "results": results,
        }, indent=2))
    else:
        print(f"mode: {args.mode}")
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            qsnip = (r["question"] or "")[:70]
            print(f"[{mark}] id={r['id']}: {qsnip}")
            if not r["passed"]:
                if r.get("error"):
                    print(f"        error:     {r['error']}")
                else:
                    cand = (r.get("candidate") or "")[:200]
                    exp = (r.get("expected") or "")[:200]
                    print(f"        candidate: {cand}")
                    print(f"        expected:  {exp}")
        print(f"\n{n_pass}/{n_total} passed  ({args.doc}, mode={args.mode})")

    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    # Direct invocation: `python3 mvm/verify.py ...` for diagnostic use.
    # Production entry point is the `mvm verify` dispatcher (~/.local/bin/mvm),
    # which auto-loads ~/.config/mvm/env. The default path here works on
    # OAuth-only hosts; --bare fast path is opt-in via MVM_FORCE_BARE=1.
    sys.exit(main())
