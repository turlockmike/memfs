"""Tests for the dream-briefing candidate generator.

Builds a synthetic graph that contains one of each candidate type, then
verifies run_briefing detects all six.
"""

import os
from datetime import datetime, timezone, timedelta

import pytest

from memfs import graph as graph_mod
from memfs.dream import run_briefing
from memfs.indexer import index_file


class Args:
    def __init__(self, orphan_days=0, bloat_lines=5, bloat_bytes=100,
                 dead_weight_days=60, dead_weight_min_layer=3):
        self.orphan_days = orphan_days
        self.bloat_lines = bloat_lines
        self.bloat_bytes = bloat_bytes
        self.dead_weight_days = dead_weight_days
        self.dead_weight_min_layer = dead_weight_min_layer


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _set_node_modified(graph, path: str, days_ago: int) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    graph.run(
        "MATCH (n:Node {path: $p}) SET n.modified_at = $ts, n.created_at = $ts",
        p=path, ts=ts,
    )


@pytest.fixture
def dream_corpus(tmp_path, graph):
    """Build a synthetic corpus with one of each candidate type."""
    mh = tmp_path / "mem"
    mh.mkdir()
    mem_home = str(mh)

    # 1. Orphan: a node with no LINK edges and no searches, aged 60d
    _write(f"{mem_home}/alone.md", "# Alone\n\nNo inbound, no outbound.\n")
    index_file(graph, mem_home, "alone.md")
    _set_node_modified(graph, "alone.md", 60)

    # 2. Near-duplicate pair
    _write(f"{mem_home}/dup_a.md",
           "# Compression Hypothesis notes\n\n"
           "Agents survive by compression. Lossy compression is learnable.\n"
           "Memory is compression memory compression agent viability viability.\n")
    _write(f"{mem_home}/dup_b.md",
           "# Compression Hypothesis notes v2\n\n"
           "Agents survive by compression. Lossy compression is learnable.\n"
           "Memory is compression memory compression agent viability viability.\n")
    index_file(graph, mem_home, "dup_a.md")
    index_file(graph, mem_home, "dup_b.md")

    # 3. Bloated file: lots of lines
    big_lines = "\n".join([f"line {i}" for i in range(50)])
    _write(f"{mem_home}/big.md", f"# Big file\n\n{big_lines}\n")
    index_file(graph, mem_home, "big.md")

    # 4. Directory with many files, no index.md
    for i in range(12):
        _write(f"{mem_home}/cluster/{i}.md", f"# file {i}\n\ncontent\n")
        index_file(graph, mem_home, f"cluster/{i}.md")

    # 5. Co-searched-but-unlinked pair
    _write(f"{mem_home}/topic_x.md", "# Topic X\n\nFoo bar baz quux.\n")
    _write(f"{mem_home}/topic_y.md", "# Topic Y\n\nFoo bar baz quux.\n")
    index_file(graph, mem_home, "topic_x.md")
    index_file(graph, mem_home, "topic_y.md")
    # Manually create 4 Query nodes each with top-3 SEARCH edges pointing to both
    now = datetime.now(timezone.utc).isoformat()
    for q in range(4):
        graph.run(
            """MERGE (q:Query {id: $qid})
               ON CREATE SET q.text = $text, q.created_at = $now,
                             q.last_used = $now, q.use_count = 1""",
            qid=f"qid_{q}", text=f"query {q}", now=now,
        )
        for rank, path in enumerate(["topic_x.md", "topic_y.md"], start=1):
            graph.run(
                """MATCH (q:Query {id: $qid}), (n:Node {path: $p})
                   MERGE (q)-[r:SEARCH]->(n)
                   ON CREATE SET r.strength = 1.0, r.rank = $rank,
                                 r.created_at = $now, r.last_activated = $now,
                                 r.access_count = 1""",
                qid=f"qid_{q}", p=path, rank=rank, now=now,
            )

    # 6. Stale freshness — write a .md with verified_at 100d ago, stale after 7d
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    _write(
        f"{mem_home}/stale_fact.md",
        f"---\nlayer: 3\nsource: https://example.com/old\n"
        f"freshness_verified_at: {stale_ts}\n"
        f"freshness_source_url: https://example.com/old\n"
        f"freshness_stale_after_days: 7\n---\n\n"
        "# Old fact\n\nThis fact was verified long ago.\n",
    )
    index_file(graph, mem_home, "stale_fact.md")

    return mem_home


def test_run_briefing_finds_all_candidate_types(dream_corpus, graph):
    candidates = run_briefing(graph, mem_home=dream_corpus, args=Args())
    types = {c["candidate_type"] for c in candidates}
    assert "orphan" in types, candidates
    assert "merge" in types, candidates
    assert "split" in types, candidates
    assert "index" in types, candidates
    assert "link" in types, candidates
    assert "stale" in types, candidates


def test_candidates_are_ndjson_serializable(dream_corpus, graph):
    import json
    candidates = run_briefing(graph, mem_home=dream_corpus, args=Args())
    for c in candidates:
        s = json.dumps(c)  # must not raise
        assert "candidate_type" in c
        assert "nodes" in c and isinstance(c["nodes"], list)
        assert "reason" in c
        assert "priority" in c
        assert 0.0 <= c["priority"] <= 1.0


def test_candidates_sorted_by_priority(dream_corpus, graph):
    candidates = run_briefing(graph, mem_home=dream_corpus, args=Args())
    priorities = [c["priority"] for c in candidates]
    assert priorities == sorted(priorities, reverse=True)


def test_dead_weight_candidate_included_in_briefing(tmp_path, graph):
    """Roadmap priority #1 test #3: the dead_weight candidate type must
    appear in dream-briefing output for layer-3+ nodes with no retrieval
    in the cold_days window.

    Setup: one layer-3 node that's indexed but never retrieved, one
    layer-1 node (below min_layer) that's also never retrieved. Only
    the layer-3 one should produce a dead_weight candidate.
    """
    mh = tmp_path / "mem"
    mh.mkdir()
    mem_home = str(mh)

    _write(
        f"{mem_home}/unused_high_layer.md",
        "---\nlayer: 3\nsource: synthetic-test-fixture\n---\n\n"
        "# Never retrieved, layer 3\n\nBody.\n",
    )
    index_file(graph, mem_home, "unused_high_layer.md")

    _write(f"{mem_home}/unused_low_layer.md",
           "---\nlayer: 1\n---\n\n# Never retrieved, layer 1\n\nBody.\n")
    index_file(graph, mem_home, "unused_low_layer.md")

    candidates = run_briefing(
        graph, mem_home=mem_home,
        args=Args(dead_weight_days=0, dead_weight_min_layer=3),
    )

    dead = [c for c in candidates if c["candidate_type"] == "dead_weight"]
    dead_paths = {p for c in dead for p in c["nodes"]}

    assert "unused_high_layer.md" in dead_paths, (
        f"layer-3 never-retrieved node should be dead_weight; got {dead}"
    )
    assert "unused_low_layer.md" not in dead_paths, (
        f"layer-1 must be filtered by min_layer; got {dead}"
    )
    # dead_weight candidates must satisfy the same invariant as others
    for c in dead:
        assert "reason" in c and "priority" in c and "nodes" in c
        assert 0.0 <= c["priority"] <= 1.0
        assert c.get("never_retrieved") is True


def test_sessions_excluded_from_merge_candidates(tmp_path, graph):
    """Session transcripts under sessions/ must never appear as merge
    candidates even when their titles/content prefixes are byte-identical.

    Regression: 2026-04-17 dream briefing flagged 10+ sessions/2026-04-*/*.md
    as jaccard=1.00 merge candidates because the Stop-hook error template
    (which starts every such session) dominates both title and content[:600]
    jaccard scoring. These are time-series records, not semantic duplicates.
    """
    mh = tmp_path / "mem"
    mh.mkdir()
    mem_home = str(mh)

    # Three near-identical "session" files under sessions/
    common_prefix = (
        "Stop hook feedback: [bash hook.sh]: Max sleep is 30 minutes. " * 20
    )
    for i, date in enumerate(["2026-04-11", "2026-04-12", "2026-04-13"]):
        session_dir = mh / "sessions" / date
        session_dir.mkdir(parents=True)
        path = session_dir / f"aaaa{i}.md"
        _write(
            str(path),
            f"---\ntitle: \"Stop hook feedback\"\nlayer: 2\n---\n\n"
            f"# Session aaaa{i} — {date}\n\n{common_prefix}",
        )
        index_file(graph, mem_home, f"sessions/{date}/aaaa{i}.md")

    # And a regular pair of near-duplicates OUTSIDE sessions/ to confirm
    # the filter doesn't disable merge detection wholesale
    for name in ("dup_alpha.md", "dup_beta.md"):
        path = mh / name
        _write(
            str(path),
            f"---\ntitle: \"Duplicate note\"\nlayer: 2\n---\n\n"
            f"This content is nearly identical across both files. {common_prefix}",
        )
        index_file(graph, mem_home, name)

    candidates = run_briefing(graph, mem_home=mem_home, args=Args())
    merge_candidates = [c for c in candidates if c["candidate_type"] == "merge"]

    # No sessions/ pair may appear in a merge candidate
    for c in merge_candidates:
        for node in c["nodes"]:
            assert not node.startswith("sessions/"), (
                f"sessions/ path {node!r} leaked into merge candidate: {c}"
            )

    # But non-session near-duplicates MUST still be detected — otherwise
    # we've accidentally disabled the whole feature
    non_session_pairs = {
        tuple(sorted(c["nodes"])) for c in merge_candidates
    }
    assert ("dup_alpha.md", "dup_beta.md") in non_session_pairs, (
        f"non-session duplicate pair should still be flagged; got: {non_session_pairs}"
    )
