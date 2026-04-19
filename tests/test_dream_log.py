"""Tests for memfs.dream_log — dream-pass output logging + recurrence report.

The dream trigger fires candidates at Karpathy each night. This module
records what Karpathy did with them so crystallization stays visible.

Four required cases (per roadmap item #3 spec):
  1. record a run (start + actions + finish write to graph + ledger)
  2. query back by run_id (get_run returns full state)
  3. report rolling 7d (dream_report counts runs, candidates, actions)
  4. recurring-ignored detection (same candidate deferred N nights)

Plus a rebuild-from-ledger test — the files-as-truth invariant (a Neo4j
wipe must be recoverable from dream-log.jsonl).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

import pytest

from memfs.dream_log import (
    start_run, record_action, finish_run,
    get_run, dream_report, rebuild_from_ledger,
    VALID_ACTIONS, _nodes_key,
)


class TestRecordAndQuery:
    """Record a run end-to-end and query it back."""

    def test_record_run_start_actions_finish(self, graph, tmp_path):
        run_id = "2026-04-19T03:00"
        start_run(
            graph, run_id=run_id,
            candidates_by_type={"orphan": 5, "merge": 3, "dead_weight": 2},
            mem_home=str(tmp_path),
        )
        a1 = record_action(
            graph, run_id=run_id, action="merge",
            candidate_type="merge", nodes=["a.md", "b.md"],
            claim_id="claim-1", mem_home=str(tmp_path),
        )
        a2 = record_action(
            graph, run_id=run_id, action="defer",
            candidate_type="orphan", nodes=["old.md"],
            note="not sure yet", mem_home=str(tmp_path),
        )
        finish_run(
            graph, run_id=run_id,
            status_delta={"nodes_before": 197, "nodes_after": 192,
                          "claims_logged": 1},
            mem_home=str(tmp_path),
        )

        # Graph state: one DreamRun with two DreamActions hanging off it
        row = graph.run_one(
            "MATCH (r:DreamRun {id: $id}) RETURN r.started_at AS s, "
            "r.finished_at AS f, r.total_candidates AS tc, "
            "r.candidates_by_type_json AS cbt, r.status_delta_json AS sd",
            id=run_id,
        )
        assert row["s"] is not None
        assert row["f"] is not None
        assert row["tc"] == 10
        assert json.loads(row["cbt"])["orphan"] == 5
        assert json.loads(row["sd"])["nodes_after"] == 192

        actions = graph.run(
            "MATCH (:DreamRun {id: $id})-[:HAS_ACTION]->(a:DreamAction) "
            "RETURN a.id AS id, a.action AS action, "
            "a.candidate_type AS ct ORDER BY a.recorded_at",
            id=run_id,
        )
        assert len(actions) == 2
        assert {x["id"] for x in actions} == {a1, a2}
        assert actions[0]["action"] == "merge"
        assert actions[1]["action"] == "defer"

        # Ledger got four events (start, action, action, finish)
        ledger_path = tmp_path / ".mem" / "dream-log.jsonl"
        assert ledger_path.exists()
        lines = ledger_path.read_text().strip().split("\n")
        events = [json.loads(l)["event"] for l in lines]
        assert events == ["start", "action", "action", "finish"]

    def test_get_run_returns_full_shape(self, graph, tmp_path):
        run_id = "run-getfull"
        start_run(graph, run_id=run_id,
                  candidates_by_type={"merge": 2}, mem_home=str(tmp_path))
        record_action(graph, run_id=run_id, action="merge",
                      candidate_type="merge", nodes=["a.md", "b.md"],
                      mem_home=str(tmp_path))
        finish_run(graph, run_id=run_id,
                   status_delta={"nodes": 50}, mem_home=str(tmp_path))

        run = get_run(graph, run_id)
        assert run is not None
        assert run["id"] == run_id
        assert run["candidates_by_type"] == {"merge": 2}
        assert run["total_candidates"] == 2
        assert run["status_delta"] == {"nodes": 50}
        assert run["started_at"] is not None
        assert run["finished_at"] is not None
        assert len(run["actions"]) == 1
        act = run["actions"][0]
        assert act["action"] == "merge"
        assert act["candidate_type"] == "merge"
        assert act["nodes"] == ["a.md", "b.md"]

    def test_get_run_missing_returns_none(self, graph):
        assert get_run(graph, "nope") is None

    def test_record_action_auto_creates_run(self, graph, tmp_path):
        """Agent may fire actions without calling start first — the CLI
        convenience. The run auto-merges."""
        record_action(graph, run_id="implicit", action="ignore",
                      candidate_type="stale", nodes=["x.md"],
                      mem_home=str(tmp_path))
        row = graph.run_one(
            "MATCH (r:DreamRun {id: 'implicit'}) RETURN r.id AS id",
        )
        assert row is not None

    def test_invalid_action_rejected(self, graph):
        with pytest.raises(ValueError):
            record_action(graph, run_id="r1", action="explode",
                          candidate_type="merge", nodes=["a.md"])

    def test_empty_run_id_rejected(self, graph):
        with pytest.raises(ValueError):
            start_run(graph, run_id="", candidates_by_type={})


class TestDreamReport:
    """Rolling-window summary: candidates/night, action rate."""

    def test_rolling_7d_counts(self, graph, tmp_path):
        # Seed three runs with varying candidate/action counts.
        start_run(graph, run_id="r-A",
                  candidates_by_type={"orphan": 3, "merge": 2},
                  mem_home=str(tmp_path))
        record_action(graph, run_id="r-A", action="merge",
                      candidate_type="merge", nodes=["a.md", "b.md"],
                      mem_home=str(tmp_path))

        start_run(graph, run_id="r-B",
                  candidates_by_type={"orphan": 4},
                  mem_home=str(tmp_path))
        record_action(graph, run_id="r-B", action="defer",
                      candidate_type="orphan", nodes=["o.md"],
                      mem_home=str(tmp_path))
        record_action(graph, run_id="r-B", action="ignore",
                      candidate_type="orphan", nodes=["o2.md"],
                      mem_home=str(tmp_path))

        start_run(graph, run_id="r-C",
                  candidates_by_type={"merge": 5, "link": 2},
                  mem_home=str(tmp_path))
        # no actions in r-C — a 0-action run

        report = dream_report(graph, window_days=7)
        assert report["n_runs"] == 3
        # 3+2 + 4 + 5+2 = 16
        assert report["total_candidates"] == 16
        # 1 merge + 1 defer + 1 ignore = 3
        assert report["total_actions"] == 3
        assert report["actions_by_kind"]["merge"] == 1
        assert report["actions_by_kind"]["defer"] == 1
        assert report["actions_by_kind"]["ignore"] == 1
        # action_rate = 3/16
        assert report["action_rate"] == round(3 / 16, 4)
        # candidates_by_type is summed across runs
        assert report["candidates_by_type"]["orphan"] == 7  # 3 + 4
        assert report["candidates_by_type"]["merge"] == 7  # 2 + 5
        assert report["candidates_by_type"]["link"] == 2
        # per-run n_actions
        rows_by_id = {r["run_id"]: r for r in report["runs"]}
        assert rows_by_id["r-A"]["n_actions"] == 1
        assert rows_by_id["r-B"]["n_actions"] == 2
        assert rows_by_id["r-C"]["n_actions"] == 0

    def test_empty_window_returns_zeros(self, graph):
        report = dream_report(graph, window_days=7)
        assert report["n_runs"] == 0
        assert report["total_candidates"] == 0
        assert report["total_actions"] == 0
        assert report["action_rate"] == 0.0
        assert report["actions_by_kind"] == {}
        assert report["runs"] == []
        assert report["recurring_ignored"] == []

    def test_window_excludes_old_runs(self, graph, tmp_path):
        """Runs older than window_days don't count toward the rollup."""
        start_run(graph, run_id="old", candidates_by_type={"orphan": 1},
                  mem_home=str(tmp_path))
        # Backdate
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        graph.run(
            "MATCH (r:DreamRun {id: 'old'}) SET r.started_at = $ts",
            ts=old_ts,
        )
        start_run(graph, run_id="new", candidates_by_type={"orphan": 2},
                  mem_home=str(tmp_path))

        report = dream_report(graph, window_days=7)
        assert report["n_runs"] == 1
        assert report["total_candidates"] == 2
        # 30d window catches both
        report_30 = dream_report(graph, window_days=60)
        assert report_30["n_runs"] == 2


class TestRecurringIgnored:
    """A candidate deferred/ignored across N distinct runs is surfaced.

    The signal: 'you keep punting this — either junk, or a real decision
    you're avoiding'. Counting distinct runs matters, not total actions —
    three defers on the same night is a typo, not recurrence.
    """

    def test_same_candidate_deferred_3_nights_surfaces(self, graph, tmp_path):
        # Three separate runs, same (candidate_type, nodes) each time,
        # each action = defer.
        for rid in ("n1", "n2", "n3"):
            start_run(graph, run_id=rid,
                      candidates_by_type={"orphan": 1},
                      mem_home=str(tmp_path))
            record_action(graph, run_id=rid, action="defer",
                          candidate_type="orphan",
                          nodes=["stuck.md"],
                          mem_home=str(tmp_path))

        report = dream_report(graph, window_days=7, recurrence_threshold=3)
        recurring = report["recurring_ignored"]
        assert len(recurring) == 1
        assert recurring[0]["candidate_type"] == "orphan"
        assert recurring[0]["nodes"] == ["stuck.md"]
        assert recurring[0]["n_runs"] == 3
        assert recurring[0]["last_action"] == "defer"

    def test_node_order_is_normalized_for_pairs(self, graph, tmp_path):
        """[a, b] and [b, a] collapse to the same candidate for recurrence."""
        for rid, nodes in (
            ("p1", ["a.md", "b.md"]),
            ("p2", ["b.md", "a.md"]),
            ("p3", ["a.md", "b.md"]),
        ):
            record_action(graph, run_id=rid, action="ignore",
                          candidate_type="merge", nodes=nodes,
                          mem_home=str(tmp_path))

        report = dream_report(graph, window_days=7, recurrence_threshold=3)
        # Should collapse to one recurring entry
        assert len(report["recurring_ignored"]) == 1
        assert report["recurring_ignored"][0]["n_runs"] == 3

    def test_actioned_candidate_does_not_surface_as_recurring(self, graph, tmp_path):
        """If the MOST RECENT action is merge/split/link/archive, the
        candidate isn't recurring-ignored — it got resolved."""
        for rid in ("r1", "r2"):
            record_action(graph, run_id=rid, action="defer",
                          candidate_type="orphan", nodes=["x.md"],
                          mem_home=str(tmp_path))
        # Third night, the agent finally acts on it
        record_action(graph, run_id="r3", action="archive",
                      candidate_type="orphan", nodes=["x.md"],
                      mem_home=str(tmp_path))

        report = dream_report(graph, window_days=7, recurrence_threshold=3)
        # Last action was archive → not recurring-ignored
        assert report["recurring_ignored"] == []

    def test_below_threshold_not_surfaced(self, graph, tmp_path):
        """Two deferrals (not 3+) don't surface as recurring."""
        for rid in ("r1", "r2"):
            record_action(graph, run_id=rid, action="defer",
                          candidate_type="orphan", nodes=["x.md"],
                          mem_home=str(tmp_path))
        report = dream_report(graph, window_days=7, recurrence_threshold=3)
        assert report["recurring_ignored"] == []

    def test_same_run_repeated_actions_do_not_trigger_recurrence(self, graph, tmp_path):
        """Three defers within ONE run is still only one run — not recurring."""
        for _ in range(3):
            record_action(graph, run_id="same-run", action="defer",
                          candidate_type="orphan", nodes=["x.md"],
                          mem_home=str(tmp_path))
        report = dream_report(graph, window_days=7, recurrence_threshold=3)
        assert report["recurring_ignored"] == []


class TestRebuildFromLedger:
    """Files-as-truth: .mem/dream-log.jsonl is durable; Neo4j is cache.

    A wipe + replay must restore run/action history — this is the same
    invariant the calibration ledger guards.
    """

    def test_rebuild_restores_after_wipe(self, graph, tmp_path):
        run_id = "rebuild-test"
        start_run(graph, run_id=run_id,
                  candidates_by_type={"merge": 2, "orphan": 1},
                  mem_home=str(tmp_path))
        record_action(graph, run_id=run_id, action="merge",
                      candidate_type="merge", nodes=["a.md", "b.md"],
                      mem_home=str(tmp_path))
        record_action(graph, run_id=run_id, action="defer",
                      candidate_type="orphan", nodes=["o.md"],
                      mem_home=str(tmp_path))
        finish_run(graph, run_id=run_id,
                   status_delta={"nodes": 50}, mem_home=str(tmp_path))

        # Wipe the Neo4j side only — ledger survives
        graph.run("MATCH (n) WHERE n:DreamRun OR n:DreamAction "
                  "DETACH DELETE n")
        assert graph.run_scalar(
            "MATCH (r:DreamRun) RETURN count(r)") == 0

        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["starts_applied"] == 1
        assert stats["actions_applied"] == 2
        assert stats["finishes_applied"] == 1

        # Run + actions came back, full state intact
        run = get_run(graph, run_id)
        assert run is not None
        assert run["total_candidates"] == 3
        assert run["status_delta"] == {"nodes": 50}
        assert len(run["actions"]) == 2

    def test_rebuild_idempotent(self, graph, tmp_path):
        run_id = "idemp"
        start_run(graph, run_id=run_id,
                  candidates_by_type={"merge": 1}, mem_home=str(tmp_path))
        record_action(graph, run_id=run_id, action="merge",
                      candidate_type="merge", nodes=["a.md", "b.md"],
                      mem_home=str(tmp_path))

        s1 = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        s2 = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert s1 == s2
        # Still exactly one run, one action
        n_runs = graph.run_scalar("MATCH (r:DreamRun) RETURN count(r)")
        n_actions = graph.run_scalar(
            "MATCH (a:DreamAction) RETURN count(a)")
        assert n_runs == 1
        assert n_actions == 1

    def test_rebuild_empty_ledger(self, graph, tmp_path):
        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["starts_seen"] == 0
        assert stats["actions_seen"] == 0
        assert stats["finishes_seen"] == 0

    def test_rebuild_tolerates_garbage_lines(self, graph, tmp_path):
        ledger_dir = tmp_path / ".mem"
        ledger_dir.mkdir(exist_ok=True)
        with open(ledger_dir / "dream-log.jsonl", "w") as f:
            f.write("not json\n")
            f.write("\n")  # empty
            f.write(json.dumps({
                "event": "start", "run_id": "good", "started_at": "2026-04-19T03:00:00+00:00",
                "candidates_by_type": {"merge": 2},
            }) + "\n")
            f.write(json.dumps({"event": "action"}) + "\n")  # missing run_id
        stats = rebuild_from_ledger(graph, mem_home=str(tmp_path))
        assert stats["starts_applied"] == 1


class TestNodeKey:
    def test_normalizes_order(self):
        assert _nodes_key(["a", "b"]) == _nodes_key(["b", "a"])

    def test_empty(self):
        assert _nodes_key([]) == ""
        assert _nodes_key(None) == ""

    def test_single(self):
        assert _nodes_key(["x.md"]) == "x.md"
