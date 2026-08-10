"""Honest uncapped denominator for content-similarity link candidates.

WHY THIS EXISTS (D190-1 rec 1, auditor MEDIUM finding registered 2026-08-09)
-----------------------------------------------------------------------------
``find_content_similar_unlinked`` caps its emission at ``limit`` (default
50) logical pairs. The autolink wrapper script (``memfs-dream-autolink.sh``)
reported that capped emission count as ``link_candidates_total`` in its
METRICS line -- which reads as "the whole pool" when it is really "the top
50 of however many exist." A positive control on 2026-08-06
(``memfs link-suggest --limit 300`` returning 300) proved the true pool was
>=300, i.e. the previous denominator was overstating coverage by >=6x
(<=17% true coverage read as 100%).

The fix (2026-08-10): extract the tokenize/score/mirror-dedupe pass shared
by both functions into ``_content_similarity_pool``, and expose
``count_content_similarity_pool`` as a cheap (no per-pair already-linked
query) way to measure the TRUE pool size, uncapped. The wrapper script now
reports this alongside the capped emission count so "total" stops meaning
"applied."

No Neo4j: pure-function test against a fake graph, same pattern as
test_link_orphan_tiebreak.py / test_link_mirror_dedupe.py.
"""

from __future__ import annotations

from memfs.dream import count_content_similarity_pool, find_content_similar_unlinked


class FakeGraph:
    """Minimal stand-in: serves the node scan only."""

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


def test_pool_size_exceeds_a_cap_that_would_hide_it():
    """6 nodes -> 3 independent similar pairs. A limit=1 emission must not
    be mistaken for the true pool size of 3."""
    corpus = [
        _node("a1", ["aaaa", "bbbb", "cccc"]),
        _node("a2", ["aaaa", "bbbb", "dddd"]),
        _node("b1", ["eeee", "ffff", "gggg"]),
        _node("b2", ["eeee", "ffff", "hhhh"]),
        _node("c1", ["iiii", "jjjj", "kkkk"]),
        _node("c2", ["iiii", "jjjj", "llll"]),
    ]
    graph = FakeGraph(corpus)

    pool_size = count_content_similarity_pool(graph)
    assert pool_size == 3, f"expected all 3 independent pairs counted, got {pool_size}"

    capped = find_content_similar_unlinked(graph, limit=1)
    assert len(capped) == 1, "sanity: the cap actually caps emission"
    assert pool_size > len(capped), (
        "the whole point of the fix: the true pool must be visibly larger "
        "than a capped emission, not silently equal to it"
    )


def test_pool_size_matches_emission_when_uncapped():
    """With limit >= pool size, the capped emission count and the pool
    census must agree exactly -- they are two views of the same set."""
    corpus = [
        _node("a1", ["aaaa", "bbbb", "cccc"]),
        _node("a2", ["aaaa", "bbbb", "dddd"]),
        _node("b1", ["eeee", "ffff", "gggg"]),
        _node("b2", ["eeee", "ffff", "hhhh"]),
    ]
    graph = FakeGraph(corpus)

    pool_size = count_content_similarity_pool(graph)
    uncapped = find_content_similar_unlinked(graph, limit=1000)

    assert pool_size == len(uncapped) == 2


def test_empty_corpus_reports_zero_not_an_error():
    graph = FakeGraph([_node("solo", ["aaaa", "bbbb"])])
    assert count_content_similarity_pool(graph) == 0


def test_mirror_twins_collapse_in_the_pool_count_too():
    """The pool census must apply the same mirror-dedupe as emission --
    otherwise the two numbers could drift (rec 1's own failure mode, just
    inverted: an inflated denominator instead of a deflated one)."""
    corpus = [
        _node("mvm/knowledge/resources/x.md", ["aaaa", "bbbb", "cccc"]),
        _node("resources/x.md", ["aaaa", "bbbb", "cccc"]),  # mirror of x.md
        _node("resources/y.md", ["aaaa", "bbbb", "dddd"]),
    ]
    graph = FakeGraph(corpus)

    pool_size = count_content_similarity_pool(graph)
    uncapped = find_content_similar_unlinked(graph, limit=1000)

    assert pool_size == 1, (
        f"the two raw spellings of the same logical pair (x,y) must collapse "
        f"to ONE pool entry, got {pool_size}"
    )
    assert pool_size == len(uncapped), (
        "pool census and emission must count the same logical pairs even "
        "when mirror spellings are present"
    )
