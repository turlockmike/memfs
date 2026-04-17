"""Tests for session ingestion — parse jsonl, write summary markdown,
idempotent on re-run, tolerant of malformed lines.
"""

import json
import os
import tempfile

import pytest

from memfs.ingest import (
    distill_session,
    ingest_session,
    render_summary_md,
    session_output_path,
)


def _write_jsonl(path: str, entries: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


@pytest.fixture
def tmp_mem_home(tmp_path):
    mh = tmp_path / "mem"
    mh.mkdir()
    return str(mh)


@pytest.fixture
def sample_jsonl(tmp_path):
    """A minimally realistic session transcript."""
    path = tmp_path / "session.jsonl"
    entries = [
        {
            "type": "agent-setting",
            "agentSetting": "karpathy",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "Hello karpathy — what is 2+2?"},
            "timestamp": "2026-04-17T02:00:00Z",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
            "cwd": "/home/mike",
            "gitBranch": "main",
            "version": "2.1.99",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "I'll use Read and Bash."},
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Read",
                        "input": {"file_path": "/home/mike/foo.md"},
                    },
                ],
            },
            "timestamp": "2026-04-17T02:00:30Z",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": "great, now delete it"},
            "timestamp": "2026-04-17T02:01:00Z",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Deleting now."},
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": "rm /home/mike/foo.md"},
                    },
                ],
            },
            "timestamp": "2026-04-17T02:02:00Z",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
        },
        # System-wrapped user message — should be filtered out
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": "Session rotated (previous exceeded max age).",
            },
            "timestamp": "2026-04-17T02:03:00Z",
            "sessionId": "abcd1234-5678-4abc-9def-0123456789ab",
        },
    ]
    _write_jsonl(str(path), entries)
    return str(path)


def test_distill_extracts_session_metadata(sample_jsonl):
    d = distill_session(sample_jsonl)
    assert d is not None
    assert d["session_id"] == "abcd1234-5678-4abc-9def-0123456789ab"
    assert d["agent_setting"] == "karpathy"
    assert d["cwd"] == "/home/mike"
    assert d["git_branch"] == "main"
    assert d["first_user_prompt"].startswith("Hello karpathy")
    # System-wrapped user message filtered
    assert len(d["user_prompts"]) == 2
    # Tool use counts
    assert d["tool_name_counts"] == {"Read": 1, "Bash": 1}
    assert d["tool_call_count"] == 2
    # Files touched (from Read input)
    assert "/home/mike/foo.md" in d["files_touched"]
    # Duration: start 02:00:00, end 02:03:00 = 3 min
    assert d["duration_minutes"] == 3


def test_render_summary_has_frontmatter_and_body(sample_jsonl):
    d = distill_session(sample_jsonl)
    body = render_summary_md(d)
    assert body.startswith("---\n")
    assert "layer: 2" in body
    assert "session_id:" in body
    assert "duration_minutes: 3" in body
    assert "Hello karpathy" in body
    assert "Read×1" in body or "Read" in body
    # Under 2KB of distilled content (frontmatter + body < 3x cap)
    assert len(body) < 6144


def test_ingest_writes_file_and_is_idempotent(sample_jsonl, tmp_mem_home):
    r1 = ingest_session(sample_jsonl, tmp_mem_home)
    assert r1["ok"] is True
    assert r1["duplicate"] is False
    assert os.path.exists(r1["node_path"])
    # File is under sessions/<date>/<short>.md
    assert "/sessions/" in r1["node_path"]
    assert r1["node_path"].endswith("/abcd1234.md")

    # Re-run → same path, duplicate=True
    r2 = ingest_session(sample_jsonl, tmp_mem_home)
    assert r2["ok"] is True
    assert r2["duplicate"] is True
    assert r2["node_path"] == r1["node_path"]

    # Still exactly one file in the sessions dir for this session
    import glob
    matches = glob.glob(os.path.join(tmp_mem_home, "sessions", "*", "abcd1234.md"))
    assert len(matches) == 1


def test_ingest_tolerates_malformed_jsonl(tmp_path, tmp_mem_home):
    bad = tmp_path / "broken.jsonl"
    with open(bad, "w") as f:
        # A valid line + two junk lines + another valid line
        f.write(json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "ok prompt"},
            "timestamp": "2026-04-17T02:00:00Z",
            "sessionId": "deadbeef-0000-4000-8000-000000000001",
        }) + "\n")
        f.write("this is not json\n")
        f.write("{not: valid}\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "ok response"},
            ]},
            "timestamp": "2026-04-17T02:00:30Z",
            "sessionId": "deadbeef-0000-4000-8000-000000000001",
        }) + "\n")

    r = ingest_session(str(bad), tmp_mem_home)
    assert r["ok"] is True
    assert r["session_id"] == "deadbeef-0000-4000-8000-000000000001"
    assert r["duplicate"] is False


def test_ingest_empty_jsonl_returns_not_ok(tmp_path, tmp_mem_home):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    r = ingest_session(str(empty), tmp_mem_home)
    assert r["ok"] is False
    assert r["reason"] == "empty_or_unparseable"
