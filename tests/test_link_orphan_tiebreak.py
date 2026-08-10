"""Orphan-preferring tie-break in the content-similarity link finder.

WHY THIS EXISTS (D190-1 rec 3, auditor MEDIUM finding registered 2026-08-09)
-----------------------------------------------------------------------------
``find_content_similar_unlinked`` caps its output at ``limit`` (default 50)
logical pairs, ordered by score descending. Until this fix, ties broke on
path string alone -- pure alphabetical luck, blind to which pairs would
actually shrink ``orphans_remaining`` (nodes with no LINK edges and never
searched) if the candidate were applied. Measured 2026-08-09:
``orphans_remaining`` kept RISING (11,011 -> 11,033) across three days of
autolink runs that DID apply ~50 edges/day, because nothing steered the
capped selection toward orphan-touching pairs.

The fix: fetch the live orphan set once (``graph_mod.get_orphans``) and
prefer, among equal-score pairs, the one that touches an orphan node.

No Neo4j: this is a pure-function property of the selection/sort logic, so
it runs against a fake graph + a monkeypatched orphan set. Same rationale as
test_link_mirror_dedupe.py -- fast, deterministic, cannot touch production.

⚠ NOTE: a live A/B run against the production graph (2026-08-10) showed no
observable delta in the top-50 selection on THAT snapshot, because ~75% of
the whole corpus (10,935 / 14,596 nodes) already qualifies as an orphan --
most ties already touch one regardless of this tie-break. These tests prove
the MECHANISM fires correctly on a constructed tie; they do not claim the
live orphans_remaining trend has reversed (that needs a fresh measurement
after real runs, per the standing verified-vs-estimated discipline).
"""

from __future__ import annotations

from memfs.dream import find_content_similar_unlinked


class FakeGraph:
    """Minimal stand-in: serves the node scan, reports no existing LINKs."""

    def __init__(self, nodes: list[dict]):
        self._nodes = nodes

    def run(self, cypher: str, **params):
        assert "MATCH (n:Node)" in cypher
        return list(self._nodes)

    def run_scalar(self, cypher: str, **params):
        return False  # no pre-existing LINK edges in these fixtures


def _node(path: str, tokens: list[str]) -> dict:
    return {
        "path": path,
        "title": "",
        "description": "",
        "content": " ".join(tokens),
        "layer": 3,
    }


def _tied_corpus() -> list[dict]:
    """Two pairs with IDENTICAL jaccard scores (0.333 = 2/6), so a cap of 1
    forces the tie-break to decide between them.

    (n1, n2) share {a, b}; (n3, n4) share {g, h}. Each pair has exactly the
    same overlap/union shape, so raw score alone cannot separate them --
    only the tie-break (or, pre-fix, alphabetical luck) can.
    """
    return [
        _node("n1", ["aaaa", "bbbb", "cccc", "dddd"]),
        _node("n2", ["aaaa", "bbbb", "eeee", "ffff"]),
        _node("n3", ["gggg", "hhhh", "iiii", "jjjj"]),
        _node("n4", ["gggg", "hhhh", "kkkk", "llll"]),
    ]


def test_orphan_touching_pair_wins_a_score_tie(monkeypatch):
    """With limit=1 and (n1,n2) alphabetically ahead of (n3,n4) at an equal
    score, marking n4 as the only orphan must flip the winner to (n3,n4)."""
    import memfs.dream as dream_mod

    monkeypatch.setattr(
        dream_mod.graph_mod, "get_orphans",
        lambda graph: [{"path": "n4", "title": "n4", "search_count": 0}],
    )
    out = find_content_similar_unlinked(FakeGraph(_tied_corpus()), limit=1)

    assert len(out) == 1
    assert set(out[0]["nodes"]) == {"n3", "n4"}, (
        f"expected the orphan-touching pair (n3,n4) to win the tie over the "
        f"alphabetically-earlier (n1,n2), got {out[0]['nodes']}"
    )


def test_alphabetical_fallback_when_neither_side_is_an_orphan(monkeypatch):
    """No orphans at all -> falls back to the pre-fix alphabetical order,
    proving the tie-break only changes behavior when it has a real signal."""
    import memfs.dream as dream_mod

    monkeypatch.setattr(dream_mod.graph_mod, "get_orphans", lambda graph: [])
    out = find_content_similar_unlinked(FakeGraph(_tied_corpus()), limit=1)

    assert len(out) == 1
    assert set(out[0]["nodes"]) == {"n1", "n2"}, (
        "with no orphans in play the original alphabetical tie order must "
        f"hold (n1,n2 first), got {out[0]['nodes']}"
    )


def test_higher_score_still_wins_regardless_of_orphan_status():
    """The tie-break is a TIE-break only -- it must never override score."""
    corpus = [
        _node("hi_a", ["aaaa", "bbbb", "cccc"]),
        # jaccard(hi_a, hi_b) = 2/4 = 0.50 (higher, and inside the window)
        _node("hi_b", ["aaaa", "bbbb", "dddd"]),
        _node("lo_orphan_a", ["gggg", "hhhh", "iiii", "jjjj", "kkkk"]),
        # jaccard(lo_orphan_a, lo_orphan_b) = 2/8 = 0.25 (lower)
        _node("lo_orphan_b", ["gggg", "hhhh", "llll", "mmmm", "nnnn"]),
    ]
    out = find_content_similar_unlinked(FakeGraph(corpus), limit=1)

    assert len(out) == 1
    assert set(out[0]["nodes"]) == {"hi_a", "hi_b"}, (
        "higher-scoring non-orphan pair must beat a lower-scoring orphan "
        f"pair, got {out[0]['nodes']}"
    )
