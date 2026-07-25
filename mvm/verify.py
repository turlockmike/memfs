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
import random
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import yaml

CLAUDE = os.environ.get("MVM_CLAUDE_BIN", "claude")

# Max bytes to pass as a single argv entry before falling back to stdin.
# Linux MAX_ARG_STRLEN is 131072 (32 pages) per argument; this sits safely under it
# so the system-prompt + flags in the same argv never push the exec over the edge.
ARGV_PROMPT_MAX_BYTES = 100_000
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

GRADER_SYSTEM = """You are a grader. You will receive the QUESTION that was asked, and two answers: a CANDIDATE and an EXPECTED.

Decide whether the CANDIDATE conveys the same factual content as EXPECTED, judged against what the QUESTION asked for.

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
  - **Grammatical number / article.** CANDIDATE and EXPECTED refer to the SAME
    noun and differ ONLY in the indefinite article (a/an), the definite article
    (the), or bare singular-vs-plural form. These are grammatical markers, not
    quantity facts. PASS — UNLESS EXPECTED states an explicit count or quantity
    word (a numeral, or one/two/single/both/all/several/exactly-N): then the
    count IS a distinct fact and a differing count FAILs.
      EXPECTED: "removes a Desecrated mod"   CANDIDATE: "removes Desecrated mods"  → PASS (article/number only)
      EXPECTED: "one Desecrated mod"          CANDIDATE: "two Desecrated mods"      → FAIL (explicit count differs)
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
  - **Incidental enumeration sub-detail omission.** EXPECTED enumerates members
    of a list as supporting detail for a broader claim; CANDIDATE conveys the
    broader claim but omits some or all of the enumerated members. PASS ONLY
    when EXPECTED's claim does not turn on the omitted member — the enumeration
    illustrates the claim rather than being the claim. If the claim IS the
    enumeration (an explicit count, or the question asks WHICH members), the
    distinct-fact-omission rule below governs and the omission FAILs.
      EXPECTED: "each class has two ascendancies (e.g. Warbringer and Titan for Warrior)"
      CANDIDATE: "each class has two ascendancies"
      → PASS — the per-class member list illustrates the count claim; the count is intact.
      EXPECTED: "the two Witch ascendancies are Infernalist and Blood Mage"
      CANDIDATE: "Infernalist"
      → FAIL — the claim IS the enumeration; each member is a distinct fact.
  - **Question-scope bounding.** EXPECTED may carry facts the QUESTION did not
    ask for (extra context the test author recorded alongside the answer).
    EXPECTED facts outside the QUESTION's scope do not count as distinct required facts.
    Facts WITHIN the question's scope are still governed by the distinct-fact-omission FAIL rule — a candidate that omits or contradicts an in-scope fact still FAILs.
      QUESTION: "On what date is the May 2026 reference-month CPI data released by the BLS?"
      EXPECTED: "June 10, 2026 (at 08:30 AM ET)."
      CANDIDATE: "June 10, 2026"
      → PASS — the time-of-day is outside the asked date scope; the in-scope date matches.
      QUESTION: "When was the current target range set, and has it changed in 2026?"
      EXPECTED: "Set 2025-12-10; HELD with no change at every 2026 meeting so far."
      CANDIDATE: "Set 2025-12-10"
      → FAIL — the question asked two things; "has it changed in 2026" is in-scope and unanswered.

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

    # Oversized prompts go via STDIN, not argv. Linux caps a SINGLE argv entry at
    # MAX_ARG_STRLEN = 32 pages = 131072 bytes; exceeding it makes execve fail with
    # E2BIG ("[Errno 7] Argument list too long") BEFORE claude ever starts. An
    # injected-verify prompt embeds the entire doc, so every doc above ~128KB was
    # structurally unverifiable — and the failure surfaced as a normal test FAIL with
    # candidate=None, i.e. it read as a CONTENT defect in the doc rather than as an
    # engine defect. Found 2026-07-22 (dream-20260722-0655) authoring locked tests for
    # areas/earning-lane-inventory.md (140,822 B): all 5 tests "failed" 3/3 attempts
    # with an empty candidate. `claude --print` reads the prompt from stdin when no
    # positional prompt is given, which has no size limit.
    use_stdin = len(user_prompt.encode("utf-8")) > ARGV_PROMPT_MAX_BYTES
    stdin_payload = user_prompt if use_stdin else None
    positional = [] if use_stdin else [user_prompt]

    if _bare_path_engaged():
        # Legacy fast path. Trust --bare to handle isolation.
        # --bare placed right after --print to match `claude --print --bare ...` docs.
        cmd = [CLAUDE, "--print", "--bare", "--no-session-persistence",
               "--tools", "", "--model", model,
               "--system-prompt", system_prompt] + positional
        result = subprocess.run(
            cmd,
            input=stdin_payload,
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
        ] + positional
        with isolated_home() as (cwd, env):
            result = subprocess.run(
                cmd,
                input=stdin_payload,
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
        f"QUESTION: {question}\n\n"
        f"CANDIDATE: {candidate}\n\n"
        f"EXPECTED: {expected}\n\n"
        "Does the CANDIDATE convey every DISTINCT fact stated in EXPECTED, without "
        "contradicting any of them? Elaborations, mechanism parentheticals, and "
        "enumerable sub-details of a single claim do not count as distinct facts "
        "(see system rubric). EXPECTED facts outside the QUESTION's scope do not "
        "count as distinct required facts; in-scope omissions and contradictions "
        "still FAIL (see system rubric). The CANDIDATE may include additional correct "
        "detail — that does not change the verdict. Output PASS or FAIL."
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


def verify_test_retry(
    doc_path: Path, test: dict, model: str, mode: str = "injected", retries: int = 2
) -> dict:
    """Run verify_test; on FAIL, re-run up to `retries` more times.

    PASS as soon as any attempt passes — this suppresses haiku first-cold-clone
    variance (the noise documented across S756–S759, where a faithful candidate
    transiently FAILs then PASSes on an isolated re-run). It does NOT rescue a
    genuinely-unsupported answer: the retriever reads the SAME doc every attempt,
    so an answer the doc cannot support stays FAIL across all attempts (the
    retriever converges to DONT-KNOW / a wrong answer that the grader keeps
    rejecting). Annotates the returned result with `attempts`.
    """
    last = None
    for attempt in range(retries + 1):
        r = verify_test(doc_path, test, model, mode=mode)
        r["attempts"] = attempt + 1
        if r["passed"]:
            return r
        last = r
    return last


def main(argv = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cold-clone verify a markdown doc against locked Q/A tests.",
        epilog=(
            "INCREMENTAL EDIT? verify the DELTA, not the whole suite. A whole-doc "
            "run executes every locked probe SEQUENTIALLY (~20-30s/probe on haiku), "
            "so a ~12-probe doc exceeds a `timeout 300` wrapper and SIGTERMs mid-run "
            "(exit 143) leaving results 'pending'. For an appended/changed probe on "
            "an immutable-prefix .tests.yaml: run `--test-id <new>` per changed id, "
            "and a git PURE-APPEND diff proves the untouched immutable ids cannot "
            "regress. Re-run the whole doc only after a structural body change."
        ),
    )
    parser.add_argument("doc", type=Path, help="Path to the markdown doc.")
    parser.add_argument("--test-id", type=str, help="Run only the test with this id (matches int or string ids). Use for incremental-edit verification — see epilog.")
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
    parser.add_argument(
        "--retries",
        type=int,
        default=int(os.environ.get("MVM_VERIFY_RETRIES", "2")),
        help="On a FAIL, re-run a test up to N more times; PASS if any attempt "
             "passes (suppresses cold-clone variance). Default 2. Use 0 for "
             "strict single-shot. Naked-baseline runs never retry.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    # Knowledge-root fallback: dream-verify-pick and the dream skill pass
    # KB-root-relative paths; resolve them from any cwd. Prefer the
    # cwd-relative path only when its tests file also exists (a mirror copy
    # of the doc without tests, e.g. ~/resources/, must not shadow the KB
    # pair). Path resolution only — grading semantics untouched.
    if not args.doc.is_absolute():
        kb_doc = Path(os.path.expanduser("~/mvm/knowledge")) / args.doc
        cwd_pair_ok = args.doc.exists() and args.doc.with_suffix(".tests.yaml").exists()
        if not cwd_pair_ok and kb_doc.exists() and kb_doc.with_suffix(".tests.yaml").exists():
            args.doc = kb_doc

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

    selected_id = None
    if args.test_id is not None and args.test_id in ("random", "any"):
        # Sentinel: the dream cycle wants ONE cheap verdict from an arbitrary
        # test, but id CONVENTIONS differ per doc ("1".."8", "id2",
        # "q3-cheap-reroll-tech"). A hand-guessed id costs a full round trip of
        # exit-2 errors before any doc is judged (dream-20260721-1242 burned a
        # 5-doc parallel launch that way; the same ambiguity nearly mislogged
        # two PASSing docs as transients in dream-20260629-0100). Resolving the
        # id from the tests file makes "pick a random test" structurally
        # always-valid instead of caller-guessed.
        if not tests_data:
            print(f"ERROR: tests file is empty: {tests_path}", file=sys.stderr)
            return 2
        chosen = random.choice(tests_data)
        selected_id = str(chosen.get("id"))
        tests_data = [chosen]
    elif args.test_id is not None:
        # Match by string so docs with string ids (e.g. "q1-base-selection")
        # and docs with integer yaml ids (e.g. 1) both resolve from the
        # str-typed CLI arg. Backward-compatible: str(1) == "1".
        tests_data = [t for t in tests_data if str(t.get("id")) == args.test_id]
        if not tests_data:
            # Always emit the human line on stderr (backward-compat).
            print(f"ERROR: no test with id={args.test_id} in {tests_path}", file=sys.stderr)
            # In --json mode ALSO emit a structured error on stdout, so a
            # stdout-only JSON consumer (e.g. the dream cycle) can distinguish
            # a bad/absent test-id (caller error -> fix the id) from genuine
            # probe-noise / an infra timeout (which yields empty stdout). The
            # silent-empty ambiguity nearly caused two PASSing docs to be
            # mislogged as transients (dream-20260629-0100).
            if args.json:
                available = [str(t.get("id")) for t in (yaml.safe_load(tests_path.read_text()) or [])]
                print(json.dumps({
                    "doc": str(args.doc),
                    "tests": str(tests_path),
                    "error": f"no test with id={args.test_id}",
                    "available_ids": available,
                }))
            return 2

    if args.lift:
        # Naked baseline must NOT retry: retrying-to-pass would understate KB
        # lift and mask weight-leakage. Injected uses the retry budget.
        naked_results = [verify_test(args.doc, t, args.model, mode="naked") for t in tests_data]
        injected_results = [verify_test_retry(args.doc, t, args.model, mode="injected", retries=args.retries) for t in tests_data]
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

    # Naked mode never retries (honest weight-prior signal); injected/default uses budget.
    eff_retries = 0 if args.mode == "naked" else args.retries
    results = [verify_test_retry(args.doc, t, args.model, mode=args.mode, retries=eff_retries) for t in tests_data]
    n_pass = sum(1 for r in results if r["passed"])
    n_total = len(results)

    if args.json:
        print(json.dumps({
            "doc": str(args.doc),
            "tests": str(tests_path),
            "model": args.model,
            "mode": args.mode,
            "test_id_selected": selected_id,
            "pass": n_pass,
            "total": n_total,
            "results": results,
        }, indent=2))
    else:
        print(f"mode: {args.mode}")
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            qsnip = (r["question"] or "")[:70]
            att = r.get("attempts", 1)
            att_note = f" (rescued on attempt {att})" if (r["passed"] and att > 1) else ""
            print(f"[{mark}] id={r['id']}: {qsnip}{att_note}")
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
