"""Tests for mvm.heat — retrieval-heat ranking + hot-doc coverage."""
import json
from pathlib import Path

from mvm.heat import heat_counts, has_tests


def _mk(root: Path, rel: str, text="# doc"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_heat_counts_evidence_paths_and_legacy_rationale(tmp_path):
    root = tmp_path / "knowledge"
    _mk(root, "resources/a.md")
    _mk(root, "resources/b.md")
    log = tmp_path / "rl.jsonl"
    entries = [
        # explicit evidence_paths (new schema)
        {"question": "q1", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/a.md"]},
        # legacy rationale path mention
        {"question": "q2", "decided_answer": "a",
         "probes": {"kb": {"rationale": "grounded in resources/b.md L3"}}},
        # dream probe — must NOT count
        {"question": "q3", "decided_answer": "a",
         "topic_hint": "dream-quality-verify",
         "evidence_paths": ["resources/a.md"]},
        # same doc twice in one entry — counts once
        {"question": "q4", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/a.md"],
         "probes": {"kb": {"rationale": "see resources/a.md"}}},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    c = heat_counts(log, root)
    assert c["resources/a.md"] == 2  # q1 + q4 (q3 excluded as probe)
    assert c["resources/b.md"] == 1


def test_heat_ignores_nonexistent_and_index_files(tmp_path):
    root = tmp_path / "knowledge"
    _mk(root, "resources/real.md")
    log = tmp_path / "rl.jsonl"
    log.write_text(json.dumps({
        "question": "q", "decided_answer": "a", "kind": "recall",
        "evidence_paths": ["resources/ghost.md", "resources/INDEX.md",
                           "resources/real.md"]}) + "\n")
    c = heat_counts(log, root)
    assert list(c) == ["resources/real.md"]


def test_has_tests(tmp_path):
    root = tmp_path / "knowledge"
    _mk(root, "resources/t.md")
    assert not has_tests("resources/t.md", root)
    _mk(root, "resources/t.tests.yaml", "- id: 1")
    assert has_tests("resources/t.md", root)
