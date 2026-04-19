"""Tests for memfs.access — access-frequency logging."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from memfs.access import (
    access_summary,
    cold_nodes,
    empty_hit_queries,
    gap_signals,
    hot_nodes,
    log_access,
)
from memfs.graph import add_node
from memfs.search import grep as do_grep


def _add(graph, path, title="t", content="hello world"):
    add_node(
        graph, path=path, title=title, content_hash=f"h-{path}",
        date_hint=None, description=None, content=content, layer=3,
    )


def _rewrite_access_ts(graph, access_id: str, iso_ts: str) -> None:
    graph.run(
        "MATCH (a:Access {id: $id}) SET a.ts = $ts",
        id=access_id, ts=iso_ts,
    )


# --- log_access basics ------------------------------------------------------


def test_log_access_hit_creates_node_and_edges(graph):
    _add(graph, "a.md")
    _add(graph, "b.md")
    aid = log_access(
        graph,
        query_text="hello",
        query_id="qid-1",
        results=[{"path": "a.md", "rank": 1},
                 {"path": "b.md", "rank": 2}],
        status="hit",
    )
    assert aid

    row = graph.run_one(
        "MATCH (a:Access {id: $id}) RETURN a.status AS s, "
        "a.result_count AS rc, a.query_text AS qt",
        id=aid,
    )
    assert row["s"] == "hit"
    assert row["rc"] == 2
    assert row["qt"] == "hello"

    # both nodes should be reachable via RETRIEVED edges
    paths = graph.run(
        "MATCH (:Access {id: $id})-[r:RETRIEVED]->(n:Node) "
        "RETURN n.path AS path, r.rank AS rank ORDER BY rank",
        id=aid,
    )
    assert [p["path"] for p in paths] == ["a.md", "b.md"]
    assert [p["rank"] for p in paths] == [1, 2]


def test_log_access_empty_hit(graph):
    aid = log_access(
        graph, query_text="nothing matches", query_id="qid-2",
        results=[], status="hit",  # caller says hit; log_access auto-downgrades
    )
    assert aid
    row = graph.run_one(
        "MATCH (a:Access {id: $id}) RETURN a.status AS s, a.result_count AS rc",
        id=aid,
    )
    assert row["s"] == "empty_hit"
    assert row["rc"] == 0


def test_log_access_disabled_env_returns_empty(graph, monkeypatch):
    monkeypatch.setenv("MEMFS_DISABLE_ACCESS_LOG", "1")
    aid = log_access(graph, "x", "qid", [])
    assert aid == ""
    count = graph.run_scalar("MATCH (a:Access) RETURN count(a)")
    assert count == 0


# --- hot_nodes --------------------------------------------------------------


def test_hot_nodes_ranks_by_hit_count(graph):
    _add(graph, "popular.md")
    _add(graph, "middle.md")
    _add(graph, "rare.md")

    # popular: 3 hits, middle: 2, rare: 1
    for i in range(3):
        log_access(graph, f"q{i}", f"qid-pop-{i}",
                   [{"path": "popular.md", "rank": 1}])
    for i in range(2):
        log_access(graph, f"q{i}", f"qid-mid-{i}",
                   [{"path": "middle.md", "rank": 1}])
    log_access(graph, "q", "qid-rare", [{"path": "rare.md", "rank": 1}])

    hot = hot_nodes(graph, window_days=7, limit=10)
    paths = [r["path"] for r in hot]
    assert paths == ["popular.md", "middle.md", "rare.md"]
    assert hot[0]["hits"] == 3


def test_hot_nodes_respects_window(graph):
    _add(graph, "old.md")
    _add(graph, "new.md")

    old_aid = log_access(graph, "q", "qid-old",
                         [{"path": "old.md", "rank": 1}])
    # backdate old access to 30 days ago
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _rewrite_access_ts(graph, old_aid, old_ts)

    log_access(graph, "q", "qid-new", [{"path": "new.md", "rank": 1}])

    hot_7 = hot_nodes(graph, window_days=7, limit=10)
    assert [r["path"] for r in hot_7] == ["new.md"]

    hot_all = hot_nodes(graph, window_days=None, limit=10)
    paths_all = {r["path"] for r in hot_all}
    assert paths_all == {"old.md", "new.md"}


# --- cold_nodes -------------------------------------------------------------


def test_cold_nodes_includes_never_retrieved(graph):
    _add(graph, "touched.md")
    _add(graph, "untouched.md")
    log_access(graph, "q", "qid", [{"path": "touched.md", "rank": 1}])

    cold = cold_nodes(graph, window_days=7, limit=10)
    paths = [r["path"] for r in cold]
    assert "untouched.md" in paths
    assert "touched.md" not in paths


def test_cold_nodes_includes_only_stale_in_window(graph):
    _add(graph, "stale.md")
    _add(graph, "fresh.md")

    old = log_access(graph, "q", "qid-old",
                     [{"path": "stale.md", "rank": 1}])
    _rewrite_access_ts(
        graph, old,
        (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
    )
    log_access(graph, "q", "qid-new", [{"path": "fresh.md", "rank": 1}])

    cold = cold_nodes(graph, window_days=7, limit=10)
    paths = [r["path"] for r in cold]
    assert "stale.md" in paths
    assert "fresh.md" not in paths


# --- empty_hit_queries ------------------------------------------------------


def test_empty_hit_queries(graph):
    _add(graph, "a.md")
    # one hit (has a result)
    log_access(graph, "hit query", "qid-hit",
               [{"path": "a.md", "rank": 1}])
    # two empty hits
    log_access(graph, "missing thing", "qid-miss-1", [])
    log_access(graph, "also missing", "qid-miss-2", [])

    rows = empty_hit_queries(graph, window_days=7, limit=10)
    qs = {r["query_text"] for r in rows}
    assert qs == {"missing thing", "also missing"}


# --- access_summary ---------------------------------------------------------


def test_access_summary_counts(graph):
    _add(graph, "a.md")
    _add(graph, "b.md")
    log_access(graph, "q1", "qid-1", [{"path": "a.md", "rank": 1}])
    log_access(graph, "q2", "qid-2",
               [{"path": "a.md", "rank": 1}, {"path": "b.md", "rank": 2}])
    log_access(graph, "q3", "qid-3", [])

    summary = access_summary(graph, window_days=7)
    assert summary["total_accesses"] == 3
    assert summary["hits"] == 2
    assert summary["empty_hits"] == 1
    assert summary["distinct_queries"] == 3
    assert summary["distinct_nodes_retrieved"] == 2


# --- grep integration -------------------------------------------------------


def test_grep_emits_access_node(graph):
    _add(graph, "one.md", content="findable unique token vermilion")
    _add(graph, "two.md", content="another doc about vermilion too")

    results = do_grep(graph, "vermilion", limit=10)
    assert len(results) >= 1

    access_count = graph.run_scalar(
        "MATCH (a:Access {status: 'hit'}) RETURN count(a)"
    )
    assert access_count == 1

    retrieved = graph.run_scalar(
        "MATCH (:Access)-[r:RETRIEVED]->(:Node) RETURN count(r)"
    )
    # should equal number of returned results (not limited to top-3)
    assert retrieved == len(results)


def test_grep_no_results_emits_empty_hit(graph):
    _add(graph, "doc.md", content="nothing related here at all")
    results = do_grep(graph, "zzzqwertyneverexistsxx", limit=10)
    assert results == []
    status = graph.run_scalar(
        "MATCH (a:Access) RETURN a.status"
    )
    assert status == "empty_hit"


# --- gap_signals (roadmap priority #1, 2026-04-19) --------------------------


def _add_with_layer(graph, path, layer, title="t", content="hello"):
    add_node(
        graph, path=path, title=title, content_hash=f"h-{path}",
        date_hint=None, description=None, content=content, layer=layer,
    )


class TestGapSignals:
    """memfs access-report --kind gap-signals — combined actionable view.

    Validates shape + the three buckets: empty_hit_queries (gap signal),
    dead_weight (indexed but never retrieved), stale_hotspots (retrieved
    heavily but content not updated).
    """

    def test_gap_signals_returns_expected_shape(self, graph):
        """Roadmap test #2: gap-signals view returns expected shape.

        The sweep in karpathy-snapshot.sh pulls this JSON and counts rows
        in the three buckets — the shape is the contract.
        """
        _add_with_layer(graph, "a.md", layer=3)
        # an empty-hit to populate the bucket
        log_access(graph, "missing thing", "qid-miss", [])

        result = gap_signals(graph, window_days=7, cold_days=30,
                             min_layer=3, limit=20)

        # Contract: keyset matches what the snapshot sweep reads
        assert set(result) == {
            "window_days", "cold_days", "min_layer",
            "summary", "empty_hit_queries", "dead_weight", "stale_hotspots",
        }
        assert isinstance(result["empty_hit_queries"], list)
        assert isinstance(result["dead_weight"], list)
        assert isinstance(result["stale_hotspots"], list)
        assert isinstance(result["summary"], dict)
        # empty-hit we logged shows up
        assert any(r.get("query_text") == "missing thing"
                   for r in result["empty_hit_queries"])

    def test_gap_signals_flags_dead_weight_never_retrieved(self, graph):
        """Layer-3 node with zero accesses appears under dead_weight;
        layer-1 node (too shallow) does NOT; retrieved node does NOT."""
        _add_with_layer(graph, "dead.md", layer=3, title="Never accessed")
        _add_with_layer(graph, "shallow.md", layer=1,
                        title="Low layer, also never accessed")
        _add_with_layer(graph, "live.md", layer=3, title="I get retrieved")
        log_access(graph, "q", "qid", [{"path": "live.md", "rank": 1}])

        result = gap_signals(graph, window_days=7, cold_days=30,
                             min_layer=3, limit=20)

        dead_paths = {r["path"] for r in result["dead_weight"]}
        assert "dead.md" in dead_paths
        assert "shallow.md" not in dead_paths  # min_layer filter
        assert "live.md" not in dead_paths     # was retrieved

    def test_hourly_sweep_runs_without_error(self, graph):
        """Roadmap test #1: hourly sweep (access-report --kind gap-signals)
        must exit cleanly and emit the JSON object the snapshot sweep
        parses. Mirrors the exact invocation used by
        karpathy-snapshot.sh::run_access_sweep.
        """
        import json
        import os
        import subprocess
        import sys

        # Seed one empty-hit so the sweep has something to report
        log_access(graph, "sweep-test query", "qid-sweep", [])

        env = os.environ.copy()
        # Propagate test DB so CLI doesn't point at 7687 prod by default
        env["MEMFS_NEO4J_URI"] = os.environ.get(
            "MEMFS_NEO4J_URI", "bolt://localhost:7688")

        result = subprocess.run(
            [sys.executable, "-m", "memfs.cli",
             "access-report", "--kind", "gap-signals",
             "--window-days", "1", "--cold-days", "30"],
            capture_output=True, text=True, env=env, timeout=15,
        )

        assert result.returncode == 0, (
            f"sweep command failed: stderr={result.stderr!r}, "
            f"stdout={result.stdout!r}"
        )
        # stdout must be a single JSON object (what the sweep parses)
        stdout = result.stdout.strip()
        assert stdout, "sweep produced no stdout"
        parsed = json.loads(stdout)
        assert "empty_hit_queries" in parsed
        assert "dead_weight" in parsed
        assert "stale_hotspots" in parsed
        # The empty-hit we seeded must round-trip through the CLI path
        assert any(r.get("query_text") == "sweep-test query"
                   for r in parsed["empty_hit_queries"])

    def test_gap_signals_schema_round_trips_cleanly(self, graph):
        """Output must be JSON-serializable in full. Roadmap explicitly
        requires schema round-trip: the sweep pipes through json.loads
        and gap_signals into a log — a non-serializable field (e.g. a
        datetime) would crash the sweep silently."""
        import json
        _add_with_layer(graph, "orphan.md", layer=3)
        log_access(graph, "missing", "qid-m", [])
        log_access(graph, "q", "qid-h",
                   [{"path": "orphan.md", "rank": 1}])

        result = gap_signals(graph, window_days=7, cold_days=30,
                             min_layer=3, limit=10)

        # Round-trip through JSON; preserves shape.
        round_tripped = json.loads(json.dumps(result))
        assert round_tripped["window_days"] == 7
        assert round_tripped["cold_days"] == 30
        assert round_tripped["min_layer"] == 3
        # summary subfields still intact
        assert "total_accesses" in round_tripped["summary"]


def test_grep_updates_node_search_tracking_for_all_returned(graph):
    """Ranks 4..N must update Node.search_count too. Previously only top-3
    were tracked. The access-log expansion ratchets that to all results."""
    # seed 5 nodes that all match a distinct token
    tokens = [f"uniqtoken{i}" for i in range(5)]
    for i, tok in enumerate(tokens):
        _add(graph, f"n{i}.md", content=f"shared-needle and private-{tok}")

    # Query the shared needle → all 5 should come back
    results = do_grep(graph, "shared-needle", limit=10)
    assert len(results) == 5

    # Every node should have search_count >= 1 (previously only top-3 did)
    for i in range(5):
        sc = graph.run_scalar(
            "MATCH (n:Node {path: $p}) RETURN n.search_count",
            p=f"n{i}.md",
        )
        assert sc >= 1, f"n{i}.md has search_count={sc}, expected >=1"
