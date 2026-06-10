"""Schema-enforcement tests for the ledger CLIs (recall-log, dream-log-append).

Every bad-case fixture here is a REAL historical drift instance from the
ledgers / auditor findings — the test proves the class is now structurally
extinct (rejected at write time), not prose-deterred.

  - auditor #134 D134-V6-1: prose tilde inside a JSON numeric field
  - auditor #147 D147-META-1: pointer-valued mistakes assessment
  - auditor #173 D173-V6-1: actions key renamed 3x in 4 cycles
  - 2026-06 eval finding #2: decided_source free-text (41 distinct values)
  - 2026-06 eval finding #1: dream probes indistinguishable from recalls
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

RECALL_LOG_CLI = str(Path.home() / ".local" / "bin" / "recall-log")
DREAM_APPEND_CLI = str(Path.home() / ".local" / "bin" / "dream-log-append")


def run_recall_add(entry, log_path):
    env = dict(os.environ, RECALL_LOG=str(log_path))
    return subprocess.run(
        [RECALL_LOG_CLI, "add"], input=json.dumps(entry),
        capture_output=True, text=True, env=env)


def run_dream_append(line, log_path):
    env = dict(os.environ, DREAM_LOG=str(log_path))
    return subprocess.run(
        [DREAM_APPEND_CLI, line], capture_output=True, text=True, env=env)


# ---------------------------------------------------------------- recall-log

GOOD_RECALL = {"question": "q?", "decided_answer": "a", "decided_source": "kb"}

HISTORICAL_BAD_SOURCES = [
    # verbatim values found in the 2026-06 ledger (finding #2)
    "kb+engine",
    "belton_dossier_plus_web",
    "web_with_targeted_verification",
    "canonical-primary (poec_data.json essences.seq tiers + pcc engine oracle; Tier-1, above kb)",
    "kb-direct:~/resources/poe2/0.4/crafting/belton-deterministic-4t1.md",
    "web+deterministic-math-oracle (KB superseded — ambiguous canonical phrasing)",
]


@pytest.mark.parametrize("src", HISTORICAL_BAD_SOURCES)
def test_recall_rejects_freetext_decided_source(tmp_path, src):
    r = run_recall_add({**GOOD_RECALL, "decided_source": src},
                       tmp_path / "rl.jsonl")
    assert r.returncode == 2
    assert "decided_source" in r.stderr
    assert "source_detail" in r.stderr  # error must be actionable
    assert not (tmp_path / "rl.jsonl").exists()  # nothing appended


@pytest.mark.parametrize("src", ["kb", "web", "weights", "engine",
                                 "contested", "cache", "mixed", "none"])
def test_recall_accepts_enum_sources(tmp_path, src):
    r = run_recall_add({**GOOD_RECALL, "decided_source": src},
                       tmp_path / "rl.jsonl")
    assert r.returncode == 0, r.stderr


def test_recall_accepts_detail_alongside_enum(tmp_path):
    e = {**GOOD_RECALL, "decided_source": "mixed",
         "source_detail": "kb canonical + pcc engine oracle, web-corroborated"}
    r = run_recall_add(e, tmp_path / "rl.jsonl")
    assert r.returncode == 0, r.stderr


def test_recall_kind_default_is_recall(tmp_path):
    log = tmp_path / "rl.jsonl"
    run_recall_add(GOOD_RECALL, log)
    got = json.loads(log.read_text().strip())
    assert got["kind"] == "recall"


def test_recall_kind_inferred_for_dream_probe(tmp_path):
    log = tmp_path / "rl.jsonl"
    e = {**GOOD_RECALL, "topic_hint": "dream-quality-verify"}
    run_recall_add(e, log)
    got = json.loads(log.read_text().strip())
    assert got["kind"] == "dream-probe"


def test_recall_rejects_bad_kind(tmp_path):
    r = run_recall_add({**GOOD_RECALL, "kind": "probe"}, tmp_path / "rl.jsonl")
    assert r.returncode == 2


def test_recall_evidence_paths_validated(tmp_path):
    bad = {**GOOD_RECALL, "evidence_paths": "resources/foo.md"}  # str not list
    assert run_recall_add(bad, tmp_path / "rl.jsonl").returncode == 2
    good = {**GOOD_RECALL, "evidence_paths": ["resources/foo.md"]}
    assert run_recall_add(good, tmp_path / "rl.jsonl").returncode == 0


def test_recall_missing_required_still_rejected(tmp_path):
    r = run_recall_add({"question": "q?"}, tmp_path / "rl.jsonl")
    assert r.returncode == 2
    assert "decided_answer" in r.stderr


# ----------------------------------------------------------- dream-log-append

GOOD_DREAM = {
    "ts": "2026-06-09T20:00:00-05:00",
    "actions": {"quality_verifies": [{"doc": "x.md", "note": "PASS"}],
                "substrate_fixes": ["fixed thing"]},
    "phase_2_meta_review": {
        "mistakes_2plus_30d_assessment": "No 2+/30d clusters; root classes X and Y each occurred once.",
    },
}


def test_dream_accepts_compliant_entry(tmp_path):
    log = tmp_path / "dl.jsonl"
    r = run_dream_append(json.dumps(GOOD_DREAM), log)
    assert r.returncode == 0, r.stderr
    assert json.loads(log.read_text().strip())["ts"] == GOOD_DREAM["ts"]


@pytest.mark.parametrize("badkey", ["structural_fixes",        # #173 cycle 5/29
                                    "substrate_fixes_shipped",  # #173 cycle 5/26
                                    "quality_fixes"])
def test_dream_rejects_noncanonical_action_keys(tmp_path, badkey):
    e = json.loads(json.dumps(GOOD_DREAM))
    e["actions"][badkey] = ["x"]
    r = run_dream_append(json.dumps(e), tmp_path / "dl.jsonl")
    assert r.returncode != 0
    assert "non-canonical" in r.stderr
    assert not (tmp_path / "dl.jsonl").exists()


def test_dream_rejects_pointer_assessment(tmp_path):
    # auditor #147: entry-10 collapsed the field to a dangling pointer
    e = json.loads(json.dumps(GOOD_DREAM))
    e["phase_2_meta_review"]["mistakes_2plus_30d_assessment"] = \
        "see escalations[] and journal Session-152 entry"
    r = run_dream_append(json.dumps(e), tmp_path / "dl.jsonl")
    assert r.returncode != 0
    assert "INLINE" in r.stderr


def test_dream_rejects_misspelled_p2_key(tmp_path):
    e = {"ts": "t", "phase2_meta_review": {"mistakes_2plus_30d_assessment": "ok"}}
    r = run_dream_append(json.dumps(e), tmp_path / "dl.jsonl")
    assert r.returncode != 0
    assert "misspelling" in r.stderr


def test_dream_rejects_string_p2(tmp_path):
    e = {"ts": "t", "phase_2_meta_review": "all good"}
    r = run_dream_append(json.dumps(e), tmp_path / "dl.jsonl")
    assert r.returncode != 0


def test_dream_rejects_prose_tilde_numeric(tmp_path):
    # auditor #134: "count_7d":~11 — invalid JSON, caught at parse layer
    r = run_dream_append('{"ts":"t","count_7d":~11}', tmp_path / "dl.jsonl")
    assert r.returncode != 0


def test_dream_rejects_concatenated_objects(tmp_path):
    # 2026-06-07: two entries fused into one line
    r = run_dream_append('{"ts":"a"}{"ts":"b"}', tmp_path / "dl.jsonl")
    assert r.returncode != 0


def test_dream_newline_repair(tmp_path):
    # appending after an unterminated line must not fuse entries
    log = tmp_path / "dl.jsonl"
    log.write_text('{"ts":"old"}')  # no trailing newline
    r = run_dream_append(json.dumps(GOOD_DREAM), log)
    assert r.returncode == 0, r.stderr
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)  # both parse independently


def test_dream_escape_hatch(tmp_path):
    env = dict(os.environ, DREAM_LOG=str(tmp_path / "dl.jsonl"),
               DREAM_LOG_SKIP_SCHEMA="1")
    e = {"ts": "t", "actions": {"structural_fixes": ["x"]}}
    r = subprocess.run([DREAM_APPEND_CLI, json.dumps(e)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
