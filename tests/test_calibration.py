"""Tests for M4 — calibration ledger."""

import json
import os
import pytest

from memfs.calibration import (
    record_claim, verify_claim, calibration_curve, rebuild_from_ledger,
    calibration_timeseries, snapshot_trend,
    _source_type, _infer_source,
)


@pytest.fixture
def clean_source_env(monkeypatch):
    """Clear env vars that `_infer_source` reads, so tests that want a true
    'no source' state aren't poisoned by the shell that launched pytest
    (CLAUDE_LOOP_NAME is set whenever pytest runs inside the karpathy loop).
    """
    monkeypatch.delenv("MEMFS_SOURCE", raising=False)
    monkeypatch.delenv("CLAUDE_LOOP_NAME", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)


class TestRecordClaim:
    def test_records_and_returns_id(self, graph, tmp_path):
        claim_id = record_claim(
            graph, text="Native CronCreate cannot persist across sessions.",
            confidence=0.9, scope="architectural", claimed_to="mike-direct",
            mem_home=str(tmp_path),
        )
        assert isinstance(claim_id, str) and len(claim_id) > 0

    def test_writes_ledger(self, graph, tmp_path):
        claim_id = record_claim(
            graph, text="X is true.", confidence=0.8, scope="factual",
            mem_home=str(tmp_path),
        )
        ledger = tmp_path / ".mem" / "calibration.jsonl"
        assert ledger.exists()
        line = ledger.read_text().strip().split("\n")[0]
        record = json.loads(line)
        assert record["event"] == "claim"
        assert record["id"] == claim_id
        assert record["confidence"] == 0.8

    def test_stores_node_in_graph(self, graph, tmp_path):
        claim_id = record_claim(
            graph, text="X is true.", confidence=0.8, scope="factual",
        )
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.text AS text, c.confidence AS conf",
            id=claim_id,
        )
        assert row["text"] == "X is true."
        assert row["conf"] == 0.8

    def test_invalid_confidence_rejected(self, graph):
        with pytest.raises(ValueError):
            record_claim(graph, text="X", confidence=1.5, scope="factual")

    def test_empty_text_rejected(self, graph):
        with pytest.raises(ValueError):
            record_claim(graph, text="", confidence=0.5, scope="factual")


class TestVerifyClaim:
    def test_verifies_correct(self, graph, tmp_path):
        cid = record_claim(graph, text="X", confidence=0.9, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.outcome AS o, c.verified_at AS v",
            id=cid,
        )
        assert row["o"] == "correct"
        assert row["v"] is not None

    def test_verifies_wrong_with_note(self, graph, tmp_path):
        cid = record_claim(graph, text="X", confidence=0.9, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="wrong",
                     note="actually Y", mem_home=str(tmp_path))
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.outcome AS o, c.note AS n",
            id=cid,
        )
        assert row["o"] == "wrong"
        assert row["n"] == "actually Y"

    def test_invalid_outcome_rejected(self, graph):
        cid = record_claim(graph, text="X", confidence=0.5, scope="s")
        with pytest.raises(ValueError):
            verify_claim(graph, claim_id=cid, outcome="maybe")

    def test_missing_claim_raises(self, graph):
        with pytest.raises(KeyError):
            verify_claim(graph, claim_id="no-such-id", outcome="correct")

    def test_appends_to_ledger(self, graph, tmp_path):
        cid = record_claim(graph, text="X", confidence=0.9, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))
        ledger = (tmp_path / ".mem" / "calibration.jsonl").read_text().strip().split("\n")
        assert len(ledger) == 2
        events = [json.loads(l)["event"] for l in ledger]
        assert events == ["claim", "verify"]


class TestCalibrationCurve:
    def test_empty_curve(self, graph):
        curve = calibration_curve(graph, window_days=30)
        assert curve["total_verified"] == 0
        assert curve["overall_accuracy"] == 0.0

    def test_curve_tracks_outcomes(self, graph, tmp_path):
        # 3 correct at 0.9, 1 wrong at 0.5
        ids = []
        for _ in range(3):
            cid = record_claim(graph, text=f"claim{_}", confidence=0.9,
                               scope="factual", mem_home=str(tmp_path))
            verify_claim(graph, claim_id=cid, outcome="correct",
                         mem_home=str(tmp_path))
            ids.append(cid)
        cid_wrong = record_claim(graph, text="wrong claim", confidence=0.5,
                                 scope="factual", mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid_wrong, outcome="wrong",
                     mem_home=str(tmp_path))

        curve = calibration_curve(graph, window_days=30)
        assert curve["total_verified"] == 4
        assert curve["total_correct"] == 3
        # Overall accuracy = 3/4 = 0.75
        assert curve["overall_accuracy"] == 0.75

        # 0.9 bin should have 3/3 = 1.0 accuracy
        bin_09 = next(b for b in curve["curve"] if b["bin_low"] == 0.9)
        assert bin_09["n"] == 3
        assert bin_09["observed_accuracy"] == 1.0

    def test_scope_filter(self, graph, tmp_path):
        cid1 = record_claim(graph, text="a", confidence=0.9, scope="s1",
                            mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid1, outcome="correct",
                     mem_home=str(tmp_path))
        cid2 = record_claim(graph, text="b", confidence=0.9, scope="s2",
                            mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid2, outcome="wrong",
                     mem_home=str(tmp_path))

        s1 = calibration_curve(graph, window_days=30, scope="s1")
        assert s1["total_verified"] == 1
        assert s1["total_correct"] == 1
        s2 = calibration_curve(graph, window_days=30, scope="s2")
        assert s2["total_verified"] == 1
        assert s2["total_correct"] == 0


class TestRebuildFromLedger:
    """Guards against the Apr 17 Neo4j wipe: JSONL is durable, Neo4j is cache."""

    def test_rebuild_empty_ledger(self, graph, tmp_path):
        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["claims_seen"] == 0
        assert stats["verifies_seen"] == 0

    def test_rebuild_restores_claims_after_db_wipe(self, graph, tmp_path):
        # Record a claim + verify via normal API (writes to both ledger and DB)
        cid = record_claim(graph, text="X is true", confidence=0.9,
                           scope="factual", mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))

        # Simulate a DB wipe — JSONL survives (Apr 17 conftest incident)
        graph.run("MATCH (c:Claim) DETACH DELETE c")
        assert graph.run_one("MATCH (c:Claim) RETURN count(c) AS n")["n"] == 0

        # Replay from the durable ledger
        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["claims_written"] == 1
        assert stats["verifies_applied"] == 1
        assert stats["verifies_skipped_no_claim"] == 0

        # Claim is back and verified — calibration query works again
        curve = calibration_curve(graph, window_days=30)
        assert curve["total_verified"] == 1
        assert curve["total_correct"] == 1

    def test_rebuild_is_idempotent(self, graph, tmp_path):
        cid = record_claim(graph, text="Y", confidence=0.8, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="wrong",
                     mem_home=str(tmp_path))

        s1 = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        s2 = rebuild_from_ledger(graph, mem_home=str(tmp_path))

        # Ledger events are processed both times (seen counts match), but the
        # final DB state is the same — MERGE on id keeps it a single Claim.
        assert s1 == s2
        n = graph.run_one("MATCH (c:Claim {id: $id}) RETURN count(c) AS n",
                          id=cid)["n"]
        assert n == 1

    def test_rebuild_preserves_first_verification(self, graph, tmp_path):
        """Later verify events don't overwrite an earlier outcome."""
        cid = record_claim(graph, text="Z", confidence=0.9, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))

        # Manually append a spurious second verify event to the ledger
        import json
        ledger = tmp_path / ".mem" / "calibration.jsonl"
        with open(ledger, "a") as f:
            f.write(json.dumps({
                "event": "verify", "id": cid, "outcome": "wrong",
                "verified_at": "2099-01-01T00:00:00+00:00",
            }) + "\n")

        graph.run("MATCH (c:Claim) DETACH DELETE c")
        rebuild_from_ledger(graph, mem_home=str(tmp_path))

        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.outcome AS o", id=cid,
        )
        # First verify wins (coalesce semantics); later one is ignored.
        assert row["o"] == "correct"

    def test_rebuild_skips_verify_for_unknown_claim(self, graph, tmp_path):
        """A verify event with no matching claim should be counted as skipped."""
        import json
        ledger_dir = tmp_path / ".mem"
        ledger_dir.mkdir(exist_ok=True)
        with open(ledger_dir / "calibration.jsonl", "w") as f:
            f.write(json.dumps({
                "event": "verify", "id": "ghost-id", "outcome": "correct",
                "verified_at": "2026-04-17T00:00:00+00:00",
            }) + "\n")

        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["verifies_seen"] == 1
        assert stats["verifies_applied"] == 0
        assert stats["verifies_skipped_no_claim"] == 1

    def test_rebuild_tolerates_malformed_lines(self, graph, tmp_path):
        """Garbage lines in ledger are skipped, not crash."""
        ledger_dir = tmp_path / ".mem"
        ledger_dir.mkdir(exist_ok=True)
        with open(ledger_dir / "calibration.jsonl", "w") as f:
            f.write("not json\n")
            f.write('{"event":"claim","id":"a","confidence":0.5,"scope":"s"}\n')
            f.write("\n")  # empty line
            f.write('{"event":"claim"}\n')  # missing id

        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        # Only the one well-formed claim-with-id succeeds
        assert stats["claims_written"] == 1


class TestSourceType:
    """The prefix extractor that powers per-source-type calibration."""

    def test_colon_prefix(self):
        assert _source_type("file:/home/mike/.config/karpathy/playbook.md") == "file"
        assert _source_type("tool:telegram-history") == "tool"
        assert _source_type("session:abc-123") == "session"
        assert _source_type("llm:claude-opus-4-7") == "llm"

    def test_no_colon(self):
        assert _source_type("manual") == "manual"
        assert _source_type("unknown-token") == "unknown-token"

    def test_none_and_empty(self):
        assert _source_type(None) == "unknown"
        assert _source_type("") == "unknown"


class TestProvenance:
    """S3* provenance — every claim records the evidence it derives from."""

    def test_claim_stores_source_in_node(self, graph, tmp_path):
        cid = record_claim(
            graph, text="X", confidence=0.8, scope="factual",
            source="file:/tmp/evidence.md", mem_home=str(tmp_path),
        )
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.source AS s", id=cid,
        )
        assert row["s"] == "file:/tmp/evidence.md"

    def test_claim_source_optional(self, graph, tmp_path, clean_source_env):
        """Claims without a source still work (backward compatibility) when
        the environment supplies no inference hints."""
        cid = record_claim(
            graph, text="X", confidence=0.8, scope="factual",
            mem_home=str(tmp_path),
        )
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.source AS s", id=cid,
        )
        assert row["s"] is None

    def test_source_written_to_ledger(self, graph, tmp_path):
        import json as _json
        cid = record_claim(
            graph, text="X", confidence=0.8, scope="factual",
            source="tool:memfs-grep", mem_home=str(tmp_path),
        )
        line = (tmp_path / ".mem" / "calibration.jsonl").read_text().strip()
        rec = _json.loads(line)
        assert rec["source"] == "tool:memfs-grep"
        assert rec["id"] == cid

    def test_ledger_omits_source_when_none(self, graph, tmp_path, clean_source_env):
        import json as _json
        record_claim(
            graph, text="X", confidence=0.5, scope="s",
            mem_home=str(tmp_path),
        )
        line = (tmp_path / ".mem" / "calibration.jsonl").read_text().strip()
        rec = _json.loads(line)
        assert "source" not in rec

    def test_calibration_filters_by_source_type(self, graph, tmp_path):
        # 2 file-sourced correct, 1 tool-sourced wrong
        for _ in range(2):
            cid = record_claim(graph, text="file claim", confidence=0.9,
                               scope="s", source="file:/x",
                               mem_home=str(tmp_path))
            verify_claim(graph, claim_id=cid, outcome="correct",
                         mem_home=str(tmp_path))
        cid = record_claim(graph, text="tool claim", confidence=0.9,
                           scope="s", source="tool:y",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="wrong",
                     mem_home=str(tmp_path))

        file_curve = calibration_curve(graph, window_days=30,
                                       source_type="file")
        assert file_curve["total_verified"] == 2
        assert file_curve["overall_accuracy"] == 1.0

        tool_curve = calibration_curve(graph, window_days=30,
                                       source_type="tool")
        assert tool_curve["total_verified"] == 1
        assert tool_curve["overall_accuracy"] == 0.0

    def test_calibration_source_breakdown(self, graph, tmp_path):
        # 2 file correct, 1 tool wrong, 1 llm correct
        for _ in range(2):
            cid = record_claim(graph, text="f", confidence=0.9, scope="s",
                               source="file:/a", mem_home=str(tmp_path))
            verify_claim(graph, claim_id=cid, outcome="correct",
                         mem_home=str(tmp_path))
        cid = record_claim(graph, text="t", confidence=0.9, scope="s",
                           source="tool:b", mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="wrong",
                     mem_home=str(tmp_path))
        cid = record_claim(graph, text="l", confidence=0.9, scope="s",
                           source="llm:claude", mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))

        curve = calibration_curve(graph, window_days=30,
                                  include_source_breakdown=True)
        by_src = curve["by_source"]
        assert by_src["file"]["n"] == 2 and by_src["file"]["accuracy"] == 1.0
        assert by_src["tool"]["n"] == 1 and by_src["tool"]["accuracy"] == 0.0
        assert by_src["llm"]["n"] == 1 and by_src["llm"]["accuracy"] == 1.0

    def test_rebuild_preserves_source(self, graph, tmp_path):
        cid = record_claim(graph, text="X", confidence=0.8, scope="s",
                           source="file:/evidence.md", mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))

        # Wipe, replay — source should come back
        graph.run("MATCH (c:Claim) DETACH DELETE c")
        rebuild_from_ledger(graph, mem_home=str(tmp_path))

        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.source AS s", id=cid,
        )
        assert row["s"] == "file:/evidence.md"

    def test_breakdown_unknown_for_legacy_claims(self, graph, tmp_path,
                                                 clean_source_env):
        """Claims from before provenance shipped (no source field) bucket as 'unknown'."""
        cid = record_claim(graph, text="legacy", confidence=0.8, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))
        curve = calibration_curve(graph, window_days=30,
                                  include_source_breakdown=True)
        assert curve["by_source"]["unknown"]["n"] == 1
        assert curve["by_source"]["unknown"]["accuracy"] == 1.0


class TestSourceInference:
    """_infer_source reads env when the caller doesn't pass `--source`."""

    def test_no_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("MEMFS_SOURCE", raising=False)
        monkeypatch.delenv("CLAUDE_LOOP_NAME", raising=False)
        monkeypatch.delenv("CLAUDECODE", raising=False)
        assert _infer_source() is None

    def test_explicit_memfs_source_wins(self, monkeypatch):
        monkeypatch.setenv("MEMFS_SOURCE", "tool:explicit-override")
        monkeypatch.setenv("CLAUDE_LOOP_NAME", "karpathy")
        monkeypatch.setenv("CLAUDECODE", "1")
        assert _infer_source() == "tool:explicit-override"

    def test_claude_loop_name_becomes_session(self, monkeypatch):
        monkeypatch.delenv("MEMFS_SOURCE", raising=False)
        monkeypatch.setenv("CLAUDE_LOOP_NAME", "karpathy")
        monkeypatch.setenv("CLAUDECODE", "1")
        assert _infer_source() == "session:karpathy"

    def test_claudecode_without_loop_name_becomes_llm(self, monkeypatch):
        monkeypatch.delenv("MEMFS_SOURCE", raising=False)
        monkeypatch.delenv("CLAUDE_LOOP_NAME", raising=False)
        monkeypatch.setenv("CLAUDECODE", "1")
        assert _infer_source() == "llm:claude"

    def test_claudecode_false_is_ignored(self, monkeypatch):
        monkeypatch.delenv("MEMFS_SOURCE", raising=False)
        monkeypatch.delenv("CLAUDE_LOOP_NAME", raising=False)
        monkeypatch.setenv("CLAUDECODE", "0")
        assert _infer_source() is None

    def test_empty_env_values_are_ignored(self, monkeypatch):
        monkeypatch.setenv("MEMFS_SOURCE", "   ")
        monkeypatch.setenv("CLAUDE_LOOP_NAME", "")
        monkeypatch.delenv("CLAUDECODE", raising=False)
        assert _infer_source() is None

    def test_record_claim_uses_inferred_source(self, graph, tmp_path, monkeypatch):
        """When caller omits source, env-inferred source populates the node."""
        monkeypatch.delenv("MEMFS_SOURCE", raising=False)
        monkeypatch.setenv("CLAUDE_LOOP_NAME", "karpathy")
        cid = record_claim(
            graph, text="inferred", confidence=0.7, scope="s",
            mem_home=str(tmp_path),
        )
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.source AS s", id=cid,
        )
        assert row["s"] == "session:karpathy"

    def test_explicit_source_arg_beats_env(self, graph, tmp_path, monkeypatch):
        """Caller-provided source is not overridden by inference."""
        monkeypatch.setenv("CLAUDE_LOOP_NAME", "karpathy")
        cid = record_claim(
            graph, text="explicit", confidence=0.7, scope="s",
            source="file:/evidence.md", mem_home=str(tmp_path),
        )
        row = graph.run_one(
            "MATCH (c:Claim {id: $id}) RETURN c.source AS s", id=cid,
        )
        assert row["s"] == "file:/evidence.md"


class TestTimeseries:
    """calibration_timeseries composes calibration_curve over rolling buckets —
    answers 'is my ECE improving?' rather than 'what is it right now?'.

    Three tests cover: (1) timeseries composition — bucket count, monotonic
    timestamps, empty buckets; (2) `--trend` CLI argument; (3) snapshot
    write to calibration-trend.jsonl.
    """

    def test_composition_buckets_timestamps_and_empty_window(self, graph, tmp_path):
        """Timeseries composition: bucket count matches window/bucket math,
        bucket_end timestamps increase monotonically, every bucket spans
        exactly bucket_days, claims fall in exactly one bucket (half-open
        boundaries — no double counting), and empty buckets return n=0
        without crashing."""
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)

        # (a) Empty-window case: no claims in DB yet.
        empty = calibration_timeseries(graph, window_days=30, bucket_days=7)
        # ceil(30/7) = 5 buckets
        assert len(empty) == 5
        for s in empty:
            assert s["n_verified"] == 0
            assert s["ece"] == 0.0
            assert s["accuracy"] == 0.0
            for key in ("bucket_start", "bucket_end", "n_verified",
                        "accuracy", "ece"):
                assert key in s

        # (b) Seed 1 correct + 1 wrong claim per each 10-day slot going back
        # 60d. Override verified_at via Cypher so claims land in specific
        # historical buckets (record/verify API stamps _now()).
        for days_ago in (5, 15, 25, 35, 45, 55):
            for outcome, conf in (("correct", 0.9), ("wrong", 0.5)):
                cid = record_claim(graph, text=f"c{days_ago}{outcome}",
                                   confidence=conf, scope="factual",
                                   mem_home=str(tmp_path))
                verified_at = (now - timedelta(days=days_ago)).isoformat()
                graph.run(
                    "MATCH (c:Claim {id: $id}) "
                    "SET c.verified_at = $va, c.outcome = $o",
                    id=cid, va=verified_at, o=outcome,
                )

        # 90-day window, 10-day buckets → 9 buckets.
        series = calibration_timeseries(graph, window_days=90, bucket_days=10)
        assert len(series) == 9

        # Monotonic bucket_end timestamps, oldest first.
        ends = [s["bucket_end"] for s in series]
        assert ends == sorted(ends)

        # Each bucket spans exactly bucket_days.
        for s in series:
            start = datetime.fromisoformat(s["bucket_start"])
            end = datetime.fromisoformat(s["bucket_end"])
            assert (end - start).days == 10

        # Populated buckets match seeded count (half-open, no double count).
        # We seeded 12 claims total (6 days × 2 outcomes). All 12 should be
        # within the 90-day window — total n_verified across all buckets == 12.
        total = sum(s["n_verified"] for s in series)
        assert total == 12

        # At least half the buckets carry seeded content.
        populated = [s for s in series if s["n_verified"] > 0]
        assert len(populated) >= 5

    def test_trend_arg_via_cli(self, graph, tmp_path, monkeypatch):
        """`memfs calibration --trend` emits one NDJSON line per bucket
        instead of a single curve JSON. Exercises the CLI plumbing:
        --trend flag, --trend-window, --trend-bucket, cmd_calibration
        branching."""
        import io
        import sys
        from memfs import cli as cli_mod

        # Seed a couple of verified claims so at least one bucket is non-empty.
        cid = record_claim(graph, text="x", confidence=0.9, scope="s",
                           mem_home=str(tmp_path))
        verify_claim(graph, claim_id=cid, outcome="correct",
                     mem_home=str(tmp_path))

        # Point MEM_HOME at tmp so get_mem_home returns it.
        monkeypatch.setenv("MEM_HOME", str(tmp_path))

        argv = [
            "memfs", "calibration",
            "--trend",
            "--trend-window", "30",
            "--trend-bucket", "10",
        ]
        monkeypatch.setattr(sys, "argv", argv)

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        cli_mod.main()
        output = buf.getvalue().strip().split("\n")

        # ceil(30/10) = 3 buckets → 3 NDJSON lines, each a valid bucket.
        assert len(output) == 3
        for line in output:
            rec = json.loads(line)
            assert "bucket_end" in rec
            assert "ece" in rec
            assert "n_verified" in rec

        # Total across all buckets should include the one seeded verified claim.
        total = sum(json.loads(line)["n_verified"] for line in output)
        assert total >= 1

    def test_snapshot_write_appends_durable_ledger(self, graph, tmp_path):
        """snapshot_trend appends a single JSON line to
        .mem/calibration-trend.jsonl with a recorded_at timestamp — the
        durable record cron writes daily, independent of Neo4j."""
        snapshot = {
            "bucket_end": "2026-04-19T00:00:00+00:00",
            "bucket_start": "2026-04-12T00:00:00+00:00",
            "n_verified": 7,
            "accuracy": 0.71,
            "ece": 0.19,
        }
        snapshot_trend(str(tmp_path), snapshot)
        snapshot_trend(str(tmp_path), snapshot)

        trend_path = tmp_path / ".mem" / "calibration-trend.jsonl"
        assert trend_path.exists()
        lines = trend_path.read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            rec = json.loads(line)
            assert rec["ece"] == 0.19
            assert rec["n_verified"] == 7
            # snapshot_trend injects recorded_at if missing.
            assert "recorded_at" in rec
