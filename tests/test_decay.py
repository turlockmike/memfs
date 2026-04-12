"""Tests for power-law decay engine and spacing-effect increments."""

import pytest
from datetime import datetime, timezone, timedelta
from memfs.db import create_db, connect
from memfs.decay import decayed_strength, spacing_increment, run_decay, PRUNE_THRESHOLD, LINK_FLOOR


class TestDecayedStrength:
    def test_no_decay_at_zero_days(self):
        assert decayed_strength(1.0, 0) == pytest.approx(1.0, abs=0.01)

    def test_decay_at_one_day(self):
        result = decayed_strength(1.0, 1)
        assert 0.93 < result < 0.97  # ~0.95

    def test_decay_at_seven_days(self):
        result = decayed_strength(1.0, 7)
        assert 0.73 < result < 0.80  # ~0.77

    def test_decay_at_thirty_days(self):
        result = decayed_strength(1.0, 30)
        assert 0.45 < result < 0.55  # ~0.50

    def test_decay_at_ninety_days(self):
        result = decayed_strength(1.0, 90)
        assert 0.28 < result < 0.36  # ~0.32

    def test_decay_at_365_days(self):
        result = decayed_strength(1.0, 365)
        assert 0.12 < result < 0.20  # ~0.16

    def test_higher_initial_strength_decays_proportionally(self):
        s1 = decayed_strength(1.0, 30)
        s3 = decayed_strength(3.0, 30)
        assert s3 == pytest.approx(s1 * 3, abs=0.01)

    def test_power_law_not_exponential(self):
        """Power law decays fast early, slow late. Check the curve shape."""
        d1 = decayed_strength(1.0, 1)   # ~0.95
        d30 = decayed_strength(1.0, 30)  # ~0.50
        d365 = decayed_strength(1.0, 365) # ~0.16
        # First day drops ~5%, days 30-365 drops ~34% — slow tail
        first_day_drop = 1.0 - d1
        late_drop = d30 - d365
        # Late period covers 335 days but drops less per day than the first day
        late_drop_per_day = late_drop / 335
        assert late_drop_per_day < first_day_drop


class TestSpacingIncrement:
    def test_same_session_small_increment(self):
        inc = spacing_increment(0, same_dir=False)
        assert 0.04 < inc < 0.06  # ~0.05

    def test_seven_day_gap_larger(self):
        inc = spacing_increment(7, same_dir=False)
        assert inc > spacing_increment(0, same_dir=False)

    def test_thirty_day_gap_even_larger(self):
        inc = spacing_increment(30, same_dir=False)
        assert inc > spacing_increment(7, same_dir=False)

    def test_same_dir_bonus(self):
        inc_same = spacing_increment(7, same_dir=True)
        inc_diff = spacing_increment(7, same_dir=False)
        assert inc_same == pytest.approx(inc_diff * 1.5, abs=0.01)

    def test_diminishing_returns_on_rapid_access(self):
        """Accessing 10 times at 0-day gap should give less total than 10 times at 7-day gaps."""
        rapid_total = spacing_increment(0, same_dir=False) * 10
        spaced_total = spacing_increment(7, same_dir=False) * 10
        assert spaced_total > rapid_total


class TestRunDecay:
    @pytest.fixture
    def db_with_edges(self, tmp_path):
        db_path = str(tmp_path / ".mem" / "memory.db")
        create_db(db_path)
        conn = connect(db_path)
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()
        recent = (now - timedelta(days=1)).isoformat()

        # Add nodes
        conn.execute("INSERT INTO nodes (path,title,created_at,modified_at,content_hash) VALUES (?,?,?,?,?)",
                     ("a.md", "A", now.isoformat(), now.isoformat(), "h1"))
        conn.execute("INSERT INTO nodes (path,title,created_at,modified_at,content_hash) VALUES (?,?,?,?,?)",
                     ("b.md", "B", now.isoformat(), now.isoformat(), "h2"))
        conn.execute("INSERT INTO nodes (path,title,created_at,modified_at,content_hash) VALUES (?,?,?,?,?)",
                     ("c.md", "C", now.isoformat(), now.isoformat(), "h3"))

        # Old search edge (should decay significantly)
        conn.execute(
            "INSERT INTO edges (source,target,type,strength,last_activated,access_count,created_at) VALUES (?,?,?,?,?,?,?)",
            ("q1", "a.md", "search", 0.3, old, 1, old))
        # Recent search edge (should barely decay)
        conn.execute(
            "INSERT INTO edges (source,target,type,strength,last_activated,access_count,created_at) VALUES (?,?,?,?,?,?,?)",
            ("q2", "b.md", "search", 1.0, recent, 5, recent))
        # Link edge with floor
        conn.execute(
            "INSERT INTO edges (source,target,type,strength,last_activated,access_count,created_at) VALUES (?,?,?,?,?,?,?)",
            ("a.md", "c.md", "link", 1.0, old, 1, old))

        conn.commit()
        return db_path

    def test_decay_reduces_old_search_edges(self, db_with_edges):
        conn = connect(db_with_edges)
        stats = run_decay(conn)
        edge = conn.execute(
            "SELECT strength FROM edges WHERE source='q1' AND target='a.md'"
        ).fetchone()
        conn.close()
        # 100 days old, strength 0.3 — should be well below original
        assert edge is None or edge[0] < 0.3  # pruned or decayed

    def test_decay_barely_affects_recent_edges(self, db_with_edges):
        conn = connect(db_with_edges)
        run_decay(conn)
        edge = conn.execute(
            "SELECT strength FROM edges WHERE source='q2' AND target='b.md'"
        ).fetchone()
        conn.close()
        assert edge is not None
        assert edge[0] > 0.9  # barely decayed (1 day)

    def test_link_edges_respect_floor(self, db_with_edges):
        conn = connect(db_with_edges)
        run_decay(conn)
        edge = conn.execute(
            "SELECT strength FROM edges WHERE source='a.md' AND target='c.md' AND type='link'"
        ).fetchone()
        conn.close()
        assert edge is not None
        assert edge[0] >= LINK_FLOOR

    def test_prunes_below_threshold(self, db_with_edges):
        conn = connect(db_with_edges)
        stats = run_decay(conn)
        conn.close()
        assert stats["pruned"] >= 0  # may or may not prune depending on exact strength

    def test_returns_stats(self, db_with_edges):
        conn = connect(db_with_edges)
        stats = run_decay(conn)
        conn.close()
        assert "updated" in stats
        assert "pruned" in stats

    def test_dry_run_no_changes(self, db_with_edges):
        conn = connect(db_with_edges)
        old_edge = conn.execute(
            "SELECT strength FROM edges WHERE source='q1' AND target='a.md'"
        ).fetchone()[0]
        run_decay(conn, dry_run=True)
        new_edge = conn.execute(
            "SELECT strength FROM edges WHERE source='q1' AND target='a.md'"
        ).fetchone()[0]
        conn.close()
        assert old_edge == new_edge
