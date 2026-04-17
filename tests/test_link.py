"""Tests for the content-similarity link finder (dream.find_content_similar_unlinked)
and the ``memfs link-suggest`` / ``memfs link-apply`` CLI pair.

The background: on 2026-04-17 the karpathy corpus had 197 indexed nodes and
exactly 0 real LINK edges — the juxtaposition surface was empty because nobody
had authored ``[[wikilinks]]`` and there wasn't enough SEARCH traffic for
co-search candidates to bootstrap. These tests lock in the new content-
similarity path used to populate that surface.
"""

from __future__ import annotations

import io
import json
import os
import sys

import pytest

from memfs import graph as graph_mod
from memfs.dream import find_content_similar_unlinked, run_briefing
from memfs.indexer import index_file


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class _Args:
    def __init__(self, orphan_days=0, bloat_lines=500, bloat_bytes=10240):
        self.orphan_days = orphan_days
        self.bloat_lines = bloat_lines
        self.bloat_bytes = bloat_bytes


def _make_related_pair(mem_home: str, graph, a_name: str, b_name: str,
                       topic: str) -> None:
    """Two nodes that share a distinctive topic vocabulary. Both index into
    graph. Uses rare-ish topic words so the DF filter keeps them.
    """
    _write(f"{mem_home}/{a_name}",
           f"# {topic} primer\n\n"
           f"Notes on {topic}: kalshi volatility edge drawdown "
           f"trading gate filter regime.\n"
           f"The {topic} regime cares about kalshi bridge rules and "
           f"trading volatility bounds.\n")
    _write(f"{mem_home}/{b_name}",
           f"# {topic} addendum\n\n"
           f"Extended {topic} playbook — kalshi volatility gate drawdown "
           f"regime and trading filter.\n"
           f"Discussion of {topic} bridge and trading edge thresholds.\n")
    index_file(graph, mem_home, a_name)
    index_file(graph, mem_home, b_name)


def _make_unrelated(mem_home: str, graph, name: str) -> None:
    _write(f"{mem_home}/{name}",
           "# Pool temperature log\n\nSpa set 102, pool 78, chlorinator green.\n")
    index_file(graph, mem_home, name)


# ---------------- find_content_similar_unlinked ----------------

def test_finds_overlapping_pair_but_not_unrelated(tmp_path, graph):
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)

    _make_related_pair(mem_home, graph, "kalshi_a.md", "kalshi_b.md", "kalshi")
    _make_unrelated(mem_home, graph, "pool.md")

    cands = find_content_similar_unlinked(graph, min_score=0.08, limit=10)
    link_pairs = {tuple(sorted(c["nodes"])) for c in cands}

    assert ("kalshi_a.md", "kalshi_b.md") in link_pairs
    # The pool note shares essentially no vocabulary with the kalshi notes
    assert not any("pool.md" in nodes for nodes in link_pairs), cands


def test_skips_already_linked_pairs(tmp_path, graph):
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    _make_related_pair(mem_home, graph, "a.md", "b.md", "kalshi")

    # Pre-create a LINK edge so the candidate must be filtered
    from memfs.graph import upsert_link_edge
    upsert_link_edge(graph, "a.md", "b.md", strength=1.0)

    cands = find_content_similar_unlinked(graph, min_score=0.08, limit=10)
    for c in cands:
        assert tuple(sorted(c["nodes"])) != ("a.md", "b.md"), c


def test_skips_near_duplicates(tmp_path, graph):
    """Pairs above max_score belong to merge, not link. The two finders
    must produce disjoint output on overlapping candidate regions.
    """
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)

    # Byte-identical bodies → jaccard > 0.9 → merge territory
    body = ("kalshi volatility regime drawdown bridge trading edge filter "
            "gate rules variability bound threshold playbook expectation.\n") * 5
    for name in ("dup_a.md", "dup_b.md"):
        _write(f"{mem_home}/{name}", f"# duplicate note\n\n{body}")
        index_file(graph, mem_home, name)

    cands = find_content_similar_unlinked(graph, min_score=0.08,
                                          max_score=0.55, limit=10)
    for c in cands:
        assert tuple(sorted(c["nodes"])) != ("dup_a.md", "dup_b.md"), c


def test_excludes_sessions_paths(tmp_path, graph):
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)

    _make_related_pair(mem_home, graph,
                       "sessions/2026-04-16/aaa.md",
                       "sessions/2026-04-16/bbb.md",
                       "kalshi")

    cands = find_content_similar_unlinked(graph, min_score=0.05, limit=10)
    for c in cands:
        for n in c["nodes"]:
            assert not n.startswith("sessions/"), c


def test_candidate_shape_is_ndjson_safe(tmp_path, graph):
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    _make_related_pair(mem_home, graph, "a.md", "b.md", "kalshi")

    cands = find_content_similar_unlinked(graph, min_score=0.05, limit=10)
    assert cands, "expected at least one candidate"
    for c in cands:
        s = json.dumps(c)  # must not raise
        assert c["candidate_type"] == "link"
        assert isinstance(c["nodes"], list) and len(c["nodes"]) == 2
        assert 0.0 <= c["priority"] <= 1.0
        assert c["source"] == "content_similarity"
        assert "score" in c
        _ = s


def test_integrates_into_run_briefing(tmp_path, graph):
    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)

    _make_related_pair(mem_home, graph, "a.md", "b.md", "kalshi")

    cands = run_briefing(graph, mem_home=mem_home, args=_Args())
    link_cands = [c for c in cands if c["candidate_type"] == "link"]
    # Either cosearch or content_similarity flavor is fine; here only CS
    # should fire (no SEARCH edges wired).
    sources = {c.get("source") for c in link_cands}
    assert "content_similarity" in sources, link_cands


# ---------------- link-apply (stdin mode) ----------------

def test_link_apply_stdin_materializes_edges(tmp_path, graph, monkeypatch, capsys):
    from memfs.cli import cmd_link_apply
    from memfs.graph import count_edges

    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    # Pre-create two real nodes so the LINK targets aren't placeholders
    for name in ("a.md", "b.md", "c.md"):
        _write(f"{mem_home}/{name}", f"# {name}\n\nbody\n")
        index_file(graph, mem_home, name)

    assert count_edges(graph, "link") == 0

    ndjson = (
        json.dumps({"candidate_type": "link", "nodes": ["a.md", "b.md"],
                    "priority": 0.5, "source": "content_similarity"}) + "\n" +
        # Non-link candidate should be silently skipped
        json.dumps({"candidate_type": "merge", "nodes": ["x.md", "y.md"],
                    "priority": 0.8}) + "\n" +
        json.dumps({"candidate_type": "link", "nodes": ["b.md", "c.md"],
                    "priority": 0.4, "source": "cosearch"}) + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(ndjson))

    class Args:
        from_stdin = True
        strength = 1.0
        source = None
        target = None

    cmd_link_apply(Args())

    assert count_edges(graph, "link") == 2


def test_link_apply_stdin_handles_bad_json(tmp_path, graph, monkeypatch):
    from memfs.cli import cmd_link_apply
    from memfs.graph import count_edges

    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    for name in ("a.md", "b.md"):
        _write(f"{mem_home}/{name}", f"# {name}\n\nbody\n")
        index_file(graph, mem_home, name)

    ndjson = (
        "not even JSON\n" +
        json.dumps({"candidate_type": "link", "nodes": ["a.md", "b.md"],
                    "priority": 0.5}) + "\n" +
        json.dumps({"candidate_type": "link", "nodes": ["a.md"],  # bad
                    "priority": 0.5}) + "\n"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(ndjson))

    class Args:
        from_stdin = True
        strength = 1.0
        source = None
        target = None

    cmd_link_apply(Args())
    # One good candidate applied, two bad ones rejected cleanly
    assert count_edges(graph, "link") == 1


def test_dream_link_edges_survive_reindex(tmp_path, graph):
    """Regression guard: edges created via link-apply must NOT be wiped when
    the source file is re-indexed. Only authored (content [[wikilink]]) edges
    are swept by clear_link_edges_from. This is what makes dream-derived
    structural signal durable.
    """
    from memfs.cli import cmd_link_apply
    from memfs.indexer import index_file
    from memfs.graph import count_edges

    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    for name in ("a.md", "b.md"):
        _write(f"{mem_home}/{name}", f"# {name}\n\nbody\n")
        index_file(graph, mem_home, name)

    class Args:
        from_stdin = False
        strength = 1.0
        source = "a.md"
        target = "b.md"
        link_source = "content_similarity"

    cmd_link_apply(Args())
    assert count_edges(graph, "link") == 1

    # Now re-index a.md. Its file content has no [[wikilinks]], so the old
    # authored-clear path would wipe the CS-derived edge we just created.
    # The source-aware clear keeps it.
    index_file(graph, mem_home, "a.md")
    assert count_edges(graph, "link") == 1, (
        "content_similarity edge was wiped by re-index — clear_link_edges_from "
        "must spare non-authored edges"
    )


def test_authored_edges_still_cleared_on_reindex(tmp_path, graph):
    """The flip side: authored edges from old content MUST be cleared when
    the file no longer references them. Otherwise stale wikilinks persist
    forever.
    """
    from memfs.indexer import index_file
    from memfs.graph import count_edges

    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)

    _write(f"{mem_home}/target.md", "# Target\n\nhello\n")
    index_file(graph, mem_home, "target.md")

    # v1: links to target
    _write(f"{mem_home}/src.md", "# Source\n\nsee [[target.md]]\n")
    index_file(graph, mem_home, "src.md")
    assert count_edges(graph, "link") == 1

    # v2: removes the link. The authored edge must be cleared.
    _write(f"{mem_home}/src.md", "# Source\n\nunrelated content\n")
    index_file(graph, mem_home, "src.md")
    assert count_edges(graph, "link") == 0


def test_link_apply_single_pair(tmp_path, graph):
    from memfs.cli import cmd_link_apply
    from memfs.graph import count_edges

    mh = tmp_path / "mem"; mh.mkdir()
    mem_home = str(mh)
    for name in ("a.md", "b.md"):
        _write(f"{mem_home}/{name}", f"# {name}\n\nbody\n")
        index_file(graph, mem_home, name)

    class Args:
        from_stdin = False
        strength = 0.8
        source = "a.md"
        target = "b.md"
        link_source = "manual"

    cmd_link_apply(Args())
    assert count_edges(graph, "link") == 1
    # And it's idempotent — same call again doesn't create a duplicate edge
    cmd_link_apply(Args())
    assert count_edges(graph, "link") == 1
