"""Tests for M4 — calibration ledger."""

import json
import os
import pytest

from memfs.calibration import (
    record_claim, verify_claim, calibration_curve,
)


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
