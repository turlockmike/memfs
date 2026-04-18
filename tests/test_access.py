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
