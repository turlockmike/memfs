"""Verify subcommand: error paths, test-file loading.

We don't exercise the live `claude` subprocess (slow + nondeterministic).
Subprocess-mocked tests live here; live integration is a separate manual run.
"""
import sys
from pathlib import Path
from unittest import mock

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
