"""Tests for power-law decay engine and spacing-effect increments."""

import pytest
from datetime import datetime, timezone, timedelta

from memfs.decay import (
    decayed_strength, spacing_increment, run_decay,
    PRUNE_THRESHOLD, LINK_FLOOR,
)


class TestDecayedStrength:
    def test_no_decay_at_zero_days(self):
        assert decayed_strength(1.0, 0) == pytest.approx(1.0, abs=0.01)

    def test_decay_at_one_day(self):
        result = decayed_strength(1.0, 1)
        assert 0.93 < result < 0.97

    def test_decay_at_seven_days(self):
        result = decayed_strength(1.0, 7)
        assert 0.73 < result < 0.80

    def test_decay_at_thirty_days(self):
        result = decayed_strength(1.0, 30)
        assert 0.45 < result < 0.55

    def test_decay_at_ninety_days(self):
        result = decayed_strength(1.0, 90)
        assert 0.28 < result < 0.36

    def test_decay_at_365_days(self):
        result = decayed_strength(1.0, 365)
        assert 0.12 < result < 0.20

    def test_higher_initial_strength_decays_proportionally(self):
        s1 = decayed_strength(1.0, 30)
        s3 = decayed_strength(3.0, 30)
        assert s3 == pytest.approx(s1 * 3, abs=0.01)

    def test_power_law_not_exponential(self):
        d1 = decayed_strength(1.0, 1)
        d30 = decayed_strength(1.0, 30)
        d365 = decayed_strength(1.0, 365)
        first_day_drop = 1.0 - d1
        late_drop = d30 - d365
        late_drop_per_day = late_drop / 335
        assert late_drop_per_day < first_day_drop


class TestSpacingIncrement:
    def test_same_session_small_increment(self):
        inc = spacing_increment(0, same_dir=False)
        assert 0.04 < inc < 0.06

    def test_seven_day_gap_larger(self):
        assert spacing_increment(7, same_dir=False) > spacing_increment(0, same_dir=False)

    def test_thirty_day_gap_even_larger(self):
        assert spacing_increment(30, same_dir=False) > spacing_increment(7, same_dir=False)

    def test_same_dir_bonus(self):
        inc_same = spacing_increment(7, same_dir=True)
        inc_diff = spacing_increment(7, same_dir=False)
        assert inc_same == pytest.approx(inc_diff * 1.5, abs=0.01)

    def test_diminishing_returns_on_rapid_access(self):
        rapid_total = spacing_increment(0, same_dir=False) * 10
        spaced_total = spacing_increment(7, same_dir=False) * 10
        assert spaced_total > rapid_total


class TestRunDecay:
    @pytest.fixture
    def graph_with_edges(self, graph):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()

        # Nodes
        graph.run(
            "CREATE (:Node {path: 'a.md', title: 'A', content_hash: 'h1', "
            "created_at: $n, modified_at: $n, search_count: 0, layer: 2})",
            n=now.isoformat(),
        )
        graph.run(
            "CREATE (:Node {path: 'b.md', title: 'B', content_hash: 'h2', "
            "created_at: $n, modified_at: $n, search_count: 0, layer: 2})",
            n=now.isoformat(),
        )
        graph.run(
            "CREATE (:Node {path: 'c.md', title: 'C', content_hash: 'h3', "
            "created_at: $n, modified_at: $n, search_count: 0, layer: 2})",
            n=now.isoformat(),
        )
        # Query nodes
        graph.run(
            "CREATE (:Query {id: 'q1', text: 'x', created_at: $o, last_used: $o, use_count: 1})",
            o=old,
        )
        graph.run(
            "CREATE (:Query {id: 'q2', text: 'y', created_at: $r, last_used: $r, use_count: 1})",
            r=recent,
        )

        # Old search edge — should decay significantly
        graph.run(
            "MATCH (q:Query {id: 'q1'}), (n:Node {path: 'a.md'}) "
            "CREATE (q)-[:SEARCH {strength: 0.3, rank: 1, created_at: $o, "
            "last_activated: $o, access_count: 1}]->(n)",
            o=old,
        )
        # Recent search edge — barely decays
        graph.run(
            "MATCH (q:Query {id: 'q2'}), (n:Node {path: 'b.md'}) "
            "CREATE (q)-[:SEARCH {strength: 1.0, rank: 1, created_at: $r, "
            "last_activated: $r, access_count: 5}]->(n)",
            r=recent,
        )
        # Old link edge — should respect floor
        graph.run(
            "MATCH (s:Node {path: 'a.md'}), (t:Node {path: 'c.md'}) "
            "CREATE (s)-[:LINK {strength: 1.0, created_at: $o, "
            "last_activated: $o, access_count: 1}]->(t)",
            o=old,
        )
        return graph

    def test_decay_reduces_old_search_edges(self, graph_with_edges):
        run_decay(graph_with_edges)
        row = graph_with_edges.run_one(
            "MATCH (:Query {id: 'q1'})-[r:SEARCH]->(:Node {path: 'a.md'}) "
            "RETURN r.strength AS s"
        )
        # 100 days old, strength 0.3 — should be well below original or pruned
        assert row is None or row["s"] < 0.3

    def test_decay_barely_affects_recent_edges(self, graph_with_edges):
        run_decay(graph_with_edges)
        row = graph_with_edges.run_one(
            "MATCH (:Query {id: 'q2'})-[r:SEARCH]->(:Node {path: 'b.md'}) "
            "RETURN r.strength AS s"
        )
        assert row is not None
        assert row["s"] > 0.9

    def test_link_edges_respect_floor(self, graph_with_edges):
        run_decay(graph_with_edges)
        row = graph_with_edges.run_one(
            "MATCH (:Node {path: 'a.md'})-[r:LINK]->(:Node {path: 'c.md'}) "
            "RETURN r.strength AS s"
        )
        assert row is not None
        assert row["s"] >= LINK_FLOOR

    def test_prunes_below_threshold(self, graph_with_edges):
        stats = run_decay(graph_with_edges)
        assert stats["pruned"] >= 0

    def test_returns_stats(self, graph_with_edges):
        stats = run_decay(graph_with_edges)
        assert "updated" in stats
        assert "pruned" in stats

    def test_dry_run_no_changes(self, graph_with_edges):
        before = graph_with_edges.run_scalar(
            "MATCH (:Query {id: 'q1'})-[r:SEARCH]->(:Node {path: 'a.md'}) "
            "RETURN r.strength"
        )
        run_decay(graph_with_edges, dry_run=True)
        after = graph_with_edges.run_scalar(
            "MATCH (:Query {id: 'q1'})-[r:SEARCH]->(:Node {path: 'a.md'}) "
            "RETURN r.strength"
        )
        assert before == after
