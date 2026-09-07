"""Tests for mvm.heat — retrieval-heat ranking + hot-doc coverage."""
import json
from pathlib import Path

from mvm.heat import heat_counts, has_tests, is_retired, is_staging, main


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


def test_is_retired_reads_frontmatter_status(tmp_path):
    root = tmp_path / "knowledge"
    _mk(root, "resources/live.md", "---\ntitle: x\nstatus: current\n---\nbody")
    _mk(root, "resources/sup.md", "---\ntitle: x\nstatus: superseded\n---\nbody")
    _mk(root, "resources/quoted.md", '---\nstatus: "Retired"\n---\nbody')
    _mk(root, "resources/nofm.md", "# no frontmatter")
    assert not is_retired("resources/live.md", root)
    assert is_retired("resources/sup.md", root)
    assert is_retired("resources/quoted.md", root)      # quoted + mixed case
    assert not is_retired("resources/nofm.md", root)
    assert not is_retired("resources/ghost.md", root)   # missing file != retired


def test_is_retired_reads_frontmatter_behind_strata_stamp(tmp_path):
    """Regression lock (2026-09-07): a strata metadata stamp (the
    `<!--strata\\n{...}\\n-->` comment the terminology-cutover started
    prepending 2026-08-26) sits at byte 0 ahead of the doc's own YAML.
    Before this fix `is_retired()` tested `text.startswith("---")` directly
    and always returned False for a stamped doc, so an already-dispositioned
    (status: superseded) doc kept re-surfacing on the untested-hot worklist
    as if it were still live -- same root cause as sweep.py's `frontmatter()`
    fix, locked here for heat.py's independent copy of the check."""
    root = tmp_path / "knowledge"
    _mk(root, "resources/stamped-sup.md",
        '<!--strata\n{"stamped": "2026-08-26", "tier": "t2"}\n-->\n'
        '---\ntitle: x\nstatus: superseded\n---\nbody')
    _mk(root, "resources/stamped-live.md",
        '<!--strata\n{"stamped": "2026-08-26", "tier": "t2"}\n-->\n'
        '---\ntitle: x\nstatus: current\n---\nbody')
    assert is_retired("resources/stamped-sup.md", root)
    assert not is_retired("resources/stamped-live.md", root)


def test_untested_worklist_excludes_retired_but_ranking_keeps_it(
        tmp_path, capsys):
    """A superseded doc must never be nominated for test authoring.

    Regression lock for the 2026-07-25 finding: `mvm heat --untested` put a
    `status: superseded` doc at the TOP of the worklist that /dream step 5 reads
    to pick a doc to author locked tests for, while `mvm sweep`'s untested_hot
    correctly excluded it. Verification budget aimed at already-dead content.
    """
    root = tmp_path / "knowledge"
    _mk(root, "resources/dead.md", "---\nstatus: superseded\n---\nbody")
    _mk(root, "resources/alive.md", "---\nstatus: current\n---\nbody")
    log = tmp_path / "rl.jsonl"
    entries = [
        # dead.md is HOTTER, so absent the filter it sorts first
        {"question": "q1", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/dead.md"]},
        {"question": "q2", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/dead.md"]},
        {"question": "q3", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/alive.md"]},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    main(["--untested", "--json", "--log", str(log), "--root", str(root)])
    out = json.loads(capsys.readouterr().out)
    paths = [r["path"] for r in out["rows"]]
    assert "resources/dead.md" not in paths, "retired doc leaked into worklist"
    assert "resources/alive.md" in paths
    # exclusion is REPORTED, so a shrunken worklist can't read as "nothing to do"
    assert out["summary"]["retired_excluded"] == 1
    assert out["summary"]["retired_excluded_paths"] == ["resources/dead.md"]

    # ...but the plain ranking still shows it: a superseded doc that is STILL
    # being retrieved is real signal that the supersede didn't reach retrieval.
    main(["--json", "--log", str(log), "--root", str(root)])
    ranked = [r["path"] for r in json.loads(capsys.readouterr().out)["rows"]]
    assert "resources/dead.md" in ranked


def test_is_staging_matches_path_segment_only():
    """MUST-FIRE and MUST-NOT-FIRE, both directions, by name.

    `_unverified` is a directory convention. Matching it as a substring would
    also swallow ordinary docs that merely mention the word in a filename —
    the over-reach that /dream's rule-3 cousins have degenerated into five
    separate times. Absence of the segment is not evidence of staging.
    """
    # must fire — a real staging path, at any depth
    assert is_staging("resources/poe2/facts/_unverified/build-warrior/x.md")
    assert is_staging("resources/_unverified/x.md")
    assert is_staging("resources/poe2/opinions/_unverified/a/b/c.md")
    # must NOT fire — substring lookalikes and ordinary docs
    assert not is_staging("resources/poe2/facts/notes_unverified.md")
    assert not is_staging("resources/poe2/_unverified-summary/x.md")
    assert not is_staging("resources/poe2/canonical/essences.md")
    # the segment must be a DIRECTORY, never the basename itself
    assert not is_staging("resources/poe2/_unverified.md")


def test_untested_worklist_excludes_staging_but_ranking_keeps_it(
        tmp_path, capsys):
    """A doc under `_unverified/` must never be nominated for test authoring.

    Regression lock for the 2026-08-09 finding: 11 of 50 rows on the untested
    worklist were self-declared staging docs, and the TOP nomination's own body
    read "Canonical: (to be distilled)" + "[ ] Cross-check against poe2db.tw".
    A locked test authored from such a doc is written from the same unverified
    source as the doc, so it passes forever — laundering an unverified claim
    into a green oracle. Strictly worse than the retired case above, which
    merely wastes budget.
    """
    root = tmp_path / "knowledge"
    _mk(root, "resources/facts/_unverified/staged.md", "raw claim, 1 video")
    _mk(root, "resources/facts/distilled.md", "canonical body")
    log = tmp_path / "rl.jsonl"
    entries = [
        # the staging doc is HOTTER, so absent the filter it sorts first
        {"question": "q1", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/facts/_unverified/staged.md"]},
        {"question": "q2", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/facts/_unverified/staged.md"]},
        {"question": "q3", "decided_answer": "a", "kind": "recall",
         "evidence_paths": ["resources/facts/distilled.md"]},
    ]
    log.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    main(["--untested", "--json", "--log", str(log), "--root", str(root)])
    out = json.loads(capsys.readouterr().out)
    paths = [r["path"] for r in out["rows"]]
    assert "resources/facts/_unverified/staged.md" not in paths, \
        "staging doc leaked into the test-authoring worklist"
    # must-still-convict direction: the fix is an exclusion, never an amnesty —
    # a legitimate untested doc must still be nominated.
    assert "resources/facts/distilled.md" in paths
    # exclusion is REPORTED, never silent
    assert out["summary"]["staging_excluded"] == 1
    assert out["summary"]["staging_excluded_paths"] == [
        "resources/facts/_unverified/staged.md"]

    # ...but the plain ranking still shows it: staging content that IS grounding
    # recalls is itself a real signal, so it must not vanish from the ranking.
    main(["--json", "--log", str(log), "--root", str(root)])
    ranked = [r["path"] for r in json.loads(capsys.readouterr().out)["rows"]]
    assert "resources/facts/_unverified/staged.md" in ranked
