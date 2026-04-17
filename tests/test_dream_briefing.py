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
    def __init__(self, orphan_days=0, bloat_lines=5, bloat_bytes=100):
        self.orphan_days = orphan_days
        self.bloat_lines = bloat_lines
        self.bloat_bytes = bloat_bytes


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
