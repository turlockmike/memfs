"""Verify subcommand: error paths, test-file loading.

We don't exercise the live `claude` subprocess (slow + nondeterministic).
Subprocess-mocked tests live here; live integration is a separate manual run.
"""
import sys
from pathlib import Path
from unittest import mock

import pytest

from mvm import verify


def test_verify_missing_doc(tmp_path, capsys):
    rc = verify.main([str(tmp_path / "nonexistent.md")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_verify_missing_tests_file(tmp_path, capsys):
    doc = tmp_path / "doc.md"
    doc.write_text("body only, no tests")
    rc = verify.main([str(doc)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tests file not found" in err.lower()


def test_verify_with_mocked_subprocess(tmp_path):
    """Full verify flow with mocked claude subprocess. Confirms grading logic."""
    doc = tmp_path / "doc.md"
    doc.write_text("The capital of France is Paris.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: 1\n  q: 'capital of France?'\n  a: 'Paris'\n"
    )

    # Mock the claude subprocess to alternate retriever / grader responses
    responses = ["Paris", "PASS"]

    def fake_subproc(system, user, model="haiku", timeout=120):
        return responses.pop(0)

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc)])
    assert rc == 0  # all pass


def test_verify_test_id_matches_string_id(tmp_path):
    """--test-id resolves a string yaml id (e.g. 'q1-base-selection').

    Regression: --test-id was type=int, so docs whose tests use string ids
    could never be probed single-test (argparse rejected the string; an int
    never equalled a string id). dream-20260624 fix: parse as str, match by
    str(id) — backward-compatible with integer yaml ids.
    """
    doc = tmp_path / "doc.md"
    doc.write_text("The capital of France is Paris.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: q1-base\n  q: 'capital of France?'\n  a: 'Paris'\n"
        "- id: q2-other\n  q: 'unused?'\n  a: 'x'\n"
    )
    responses = ["Paris", "PASS"]

    def fake_subproc(system, user, model="haiku", timeout=120):
        return responses.pop(0)

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc), "--test-id", "q1-base"])
    assert rc == 0  # the single string-id test resolved and passed


def test_verify_test_id_matches_int_id(tmp_path):
    """--test-id still resolves an integer yaml id from a str-typed CLI arg."""
    doc = tmp_path / "doc.md"
    doc.write_text("The capital of France is Paris.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: 1\n  q: 'capital of France?'\n  a: 'Paris'\n"
    )
    responses = ["Paris", "PASS"]

    def fake_subproc(system, user, model="haiku", timeout=120):
        return responses.pop(0)

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc), "--test-id", "1"])
    assert rc == 0


def test_verify_test_id_unknown_errors(tmp_path, capsys):
    """An unmatched --test-id still errors cleanly (rc 2)."""
    doc = tmp_path / "doc.md"
    doc.write_text("body")
    (tmp_path / "doc.tests.yaml").write_text("- id: q1\n  q: 'q?'\n  a: 'a'\n")
    rc = verify.main([str(doc), "--test-id", "nope"])
    assert rc == 2
    assert "no test with id" in capsys.readouterr().err.lower()


def test_verify_records_error_on_subprocess_failure(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("body")
    (tmp_path / "doc.tests.yaml").write_text("- id: 1\n  q: 'q?'\n  a: 'a'\n")

    def boom(*args, **kwargs):
        raise RuntimeError("subprocess failed")

    with mock.patch.object(verify, "claude_subprocess", side_effect=boom):
        rc = verify.main([str(doc)])
    # Should not crash; should return non-zero (test failed)
    assert rc != 0


# Retriever-strictness contract (heartbeat-1800 2026-05-10).
# Live haiku ignored "output exactly: DONT-KNOW" in ~50% of borderline runs
# (heartbeat-1230 finding: spacex test #7 went DONT-KNOW → "Not disclosed").
# These tests assert the prompt body itself carries the strict contract.
# They DON'T validate haiku compliance — that's a live-integration concern.

def test_retriever_system_demands_literal_sentinel():
    """RETRIEVER_SYSTEM must demand the literal DONT-KNOW string."""
    p = verify.RETRIEVER_SYSTEM
    assert "DONT-KNOW" in p
    # The literal sentinel must be presented as the entire-output contract.
    assert "ENTIRE output" in p or "entire output" in p
    # The contract framing must be explicit (not just "output exactly").
    assert "contract" in p.lower()


def test_retriever_system_lists_forbidden_alternates():
    """RETRIEVER_SYSTEM must enumerate the paraphrases haiku produces in the
    failure mode (heartbeat-1230 observation). Listing them in-prompt is the
    strictness lever: haiku is more likely to comply when alternates are
    explicitly forbidden than when only the positive contract is stated."""
    p = verify.RETRIEVER_SYSTEM
    # Three high-priority observed paraphrases from heartbeat-1230.
    assert "I don't know" in p
    assert "Not disclosed" in p
    # Generic catch-alls.
    assert "Not specified" in p or "Unknown" in p


def test_naked_retriever_system_demands_literal_sentinel():
    """NAKED_RETRIEVER_SYSTEM (used in --mode naked / Hassabis weight-leakage
    detector) must enforce the same literal DONT-KNOW contract."""
    p = verify.NAKED_RETRIEVER_SYSTEM
    assert "DONT-KNOW" in p
    assert "ENTIRE output" in p or "entire output" in p
    assert "I don't know" in p
    assert "Not disclosed" in p or "Not specified" in p


def test_naked_retriever_system_forbids_tool_use():
    """Naked mode must explicitly state no tools — defensive against haiku
    attempting to invoke web/search if it sees the question is about a recent
    event."""
    p = verify.NAKED_RETRIEVER_SYSTEM
    assert "no tools" in p.lower() or "Do not invoke tools" in p


# Grader-rubric substance/distinct-fact boundary locks (dream-20260610-0100,
# auditor APPROVE-WITH-CHANGES sign-off). These are ANTI-DELETION locks: the
# grader-pathology fix (substance-over-completeness) is only safe while the
# distinct-fact FAIL + materially-different-learner test remain in the rubric.
# A future "loosen the grader" edit that drops them must break these tests.

def test_grader_system_retains_distinct_fact_fail():
    """The distinct-fact-omission FAIL branch is the oracle's genuine-defect
    detector — its deletion is the HC#4 weakening the auditor rejected."""
    p = verify.GRADER_SYSTEM
    assert "omits a DISTINCT fact" in p
    assert "materially-different-learner" in p
    # Canonical FAIL examples stay encoded verbatim.
    assert "Memphis, Tennessee" in p          # city+state distinct-fact FAIL
    assert "explicit count differs" in p      # count-differs FAIL


def test_grader_system_retains_substance_pass_carveouts():
    """The three calibrated PASS carve-outs (elaboration / rewording /
    incidental enumeration) — the substance-over-completeness side."""
    p = verify.GRADER_SYSTEM
    assert "Elaboration-clause omission" in p
    assert "Same-fact rewording" in p
    assert "Incidental enumeration sub-detail omission" in p


def test_grader_system_enumeration_clause_is_bounded():
    """The enumeration PASS is subordinated to the learner test: it must carry
    both branches — PASS-when-incidental AND FAIL-when-the-claim-IS-the-
    enumeration. An unbounded version would let stale docs verify clean."""
    p = verify.GRADER_SYSTEM
    assert "claim IS the enumeration" in p
    assert "Infernalist" in p  # the load-bearing-member FAIL example


def test_grader_user_turn_says_distinct_fact():
    """The grader user-turn must align with the system rubric: 'every DISTINCT
    fact', not 'every fact' — the old wording contradicted the elaboration
    carve-outs and drove residual false-FAILs (auditor finding #3)."""
    import inspect
    src = inspect.getsource(verify.verify_test)
    assert "every DISTINCT fact" in src
    assert "do not count as distinct facts" in src


def test_grader_flow_passes_incidental_enumeration_omission(tmp_path):
    """Mocked end-to-end: a terse-but-correct candidate graded PASS flows
    through to rc=0 (the false-FAIL class this fix targets)."""
    doc = tmp_path / "doc.md"
    doc.write_text("Each class has two ascendancies.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: 1\n  q: 'how many ascendancies per class?'\n  a: 'two'\n"
    )
    responses = ["each class has two ascendancies", "PASS"]

    def fake_subproc(system, user, model="haiku", timeout=120):
        return responses.pop(0)

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc)])
    assert rc == 0


# Question-scope grader locks (dream-20260610, auditor APPROVE-WITH-CHANGES;
# successor to e223738). The grader was question-blind: candidates that fully
# answered the asked question FAILed against EXPECTEDs carrying out-of-scope
# facts (8/11 engine FAILs in the 2026-06-10T01:17 cycle were this artifact:
# ff4, cpi1, cpi6, fomc5, mtm1, mtm4, mtm5, mtm8). Fix: QUESTION is passed to
# the grader, plus a bounded question-scope clause subordinated to the
# distinct-fact-omission FAIL rule. These are ANTI-DELETION locks in the same
# style as the e223738 block above.

def test_grader_prompt_carries_question(tmp_path):
    """(i) The QUESTION must be passed into the constructed grader prompt —
    both the system rubric and the grader user-turn."""
    assert "QUESTION" in verify.GRADER_SYSTEM

    doc = tmp_path / "doc.md"
    doc.write_text("The May 2026 CPI is released June 10, 2026 at 08:30 AM ET.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: 1\n"
        "  q: 'On what date is the May 2026 reference-month CPI data released by the BLS?'\n"
        "  a: 'June 10, 2026 (at 08:30 AM ET).'\n"
    )
    calls = []

    def fake_subproc(system, user, model="haiku", timeout=120):
        calls.append((system, user))
        return "June 10, 2026" if len(calls) == 1 else "PASS"

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc)])
    assert rc == 0
    grader_system, grader_user = calls[1]
    assert grader_system == verify.GRADER_SYSTEM
    assert "QUESTION: On what date is the May 2026 reference-month CPI data" in grader_user
    # QUESTION precedes CANDIDATE/EXPECTED — it is rubric context, not an answer.
    assert grader_user.index("QUESTION:") < grader_user.index("CANDIDATE:")


def test_grader_system_question_scope_clause_locked():
    """Anti-deletion lock on the auditor-mandated bounding clause — BOTH
    sentences, verbatim. Sentence 2 is the positive-fire bound: dropping it
    would let the scope carve-out swallow genuine in-scope omissions."""
    p = verify.GRADER_SYSTEM
    assert ("EXPECTED facts outside the QUESTION's scope do not count as "
            "distinct required facts.") in p
    assert ("Facts WITHIN the question's scope are still governed by the "
            "distinct-fact-omission FAIL rule — a candidate that omits or "
            "contradicts an in-scope fact still FAILs.") in p


def test_grader_system_in_scope_omission_still_fails():
    """(ii) positive-fire: the rubric mandates FAIL when the question asks for
    two facts and the candidate gives only one in-scope fact. The two-part
    set-date/has-it-changed example must survive with its FAIL verdict."""
    p = verify.GRADER_SYSTEM
    assert "When was the current target range set, and has it changed in 2026?" in p
    assert 'the question asked two things; "has it changed in 2026" is in-scope and unanswered' in p
    # The FAIL branch itself stays intact (e223738 lock reasserted here: the
    # scope clause is subordinated to it, never a replacement for it).
    assert "omits a DISTINCT fact" in p
    assert "materially-different-learner" in p


def test_grader_user_turn_bounds_scope_clause():
    """The grader user-turn must carry the scope clause WITH its in-scope
    bound, aligned with the system rubric (same both-directions pattern as
    the 'every DISTINCT fact' alignment lock above)."""
    import inspect
    src = inspect.getsource(verify.verify_test)
    assert 'f"QUESTION: {question}' in src
    assert "outside the QUESTION's scope do not" in src
    assert "in-scope omissions and contradictions" in src


# Regression fixtures — real transients from the 2026-06-10T01:17 dream cycle,
# q/expected verbatim from the locked test files under knowledge/. Each
# candidate fully answers the asked question; EXPECTED's surplus is outside the
# question's scope, so under the new clause these are locked PASS cases.
QUESTION_SCOPE_TRANSIENTS = [
    pytest.param(
        # areas/kalshi/cpi-release-schedule-2026.tests.yaml id=1 ("cpi1",
        # time-of-day surplus)
        "On what date is the May 2026 reference-month CPI data released by the BLS?",
        "June 10, 2026 (at 08:30 AM ET).",
        "June 10, 2026",
        id="cpi1",
    ),
    pytest.param(
        # areas/kalshi/fed-funds-rate-2026.tests.yaml id=4 ("ff4", set-date
        # surplus)
        "What was the target range right before the December 2025 cut to 3.50-3.75%?",
        "3.75-4.00% (set Oct 29, 2025).",
        "3.75-4.00%",
        id="ff4",
    ),
    pytest.param(
        # areas/kalshi/fomc-schedule-2026.tests.yaml id=5 ("fomc5", press-conf
        # surplus)
        "When is the FOMC rate decision / statement released relative to the two-day meeting, and what time?",
        "On the second (final) day of the meeting, at 2:00 PM ET, followed by the Chair's press conference at 2:30 PM ET.",
        "On the second (final) day of the meeting, at 2:00 PM ET.",
        id="fomc5",
    ),
]


@pytest.mark.parametrize("question,expected,candidate", QUESTION_SCOPE_TRANSIENTS)
def test_grader_flow_passes_out_of_scope_expected_surplus(
    tmp_path, question, expected, candidate
):
    """(iii) Mocked end-to-end on the exact motivating artifacts: candidate
    answers the asked question fully; EXPECTED's out-of-scope surplus must not
    block PASS. Also asserts the grader call received the QUESTION."""
    import yaml as _yaml

    doc = tmp_path / "doc.md"
    doc.write_text(f"Q context: {question}\nA: {expected}\n")
    (tmp_path / "doc.tests.yaml").write_text(
        _yaml.safe_dump([{"id": 1, "q": question, "a": expected}])
    )
    calls = []

    def fake_subproc(system, user, model="haiku", timeout=120):
        calls.append((system, user))
        return candidate if len(calls) == 1 else "PASS"

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc)])
    assert rc == 0
    grader_user = calls[1][1]
    assert f"QUESTION: {question}" in grader_user
    assert f"CANDIDATE: {candidate}" in grader_user
    assert f"EXPECTED: {expected}" in grader_user


def test_grader_flow_fails_in_scope_omission(tmp_path):
    """Negative control at the flow level: a grader FAIL on an in-scope
    omission still propagates to rc=1 — the scope clause changed the rubric,
    not the FAIL plumbing. (Live-haiku negative control is the manual
    integration run; see file header.)"""
    doc = tmp_path / "doc.md"
    doc.write_text("Set 2025-12-10; held with no change at every 2026 meeting.")
    (tmp_path / "doc.tests.yaml").write_text(
        "- id: 1\n"
        "  q: 'When was the current target range set, and has it changed in 2026?'\n"
        "  a: 'Set 2025-12-10; HELD with no change at every 2026 meeting so far.'\n"
    )

    def fake_subproc(system, user, model="haiku", timeout=120):
        # Retriever returns one-of-two in-scope facts; grader (per rubric's
        # in-scope bound) returns FAIL on every attempt.
        return "Set 2025-12-10" if user.startswith("DOCUMENT:") else "FAIL"

    with mock.patch.object(verify, "claude_subprocess", side_effect=fake_subproc):
        rc = verify.main([str(doc)])
    assert rc == 1
