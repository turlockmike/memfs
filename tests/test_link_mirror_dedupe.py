"""Mirror-twin collapse in the content-similarity link finder.

THE BUG (measured 2026-08-08, auditor #188 REC 1)
--------------------------------------------------
``mvm-mirror`` (cron */5) keeps ``~/resources`` byte-identical with
``~/mvm/knowledge/resources``, so every logical note is indexed under TWO
Node paths. One logical pair (X,Y) therefore produced up to FOUR link
candidates -- (X,Y), (mX,Y), (X,mY), (mX,mY) -- carrying an identical
jaccard score. The 2026-08-08T00:30 autolink run applied **49 raw edges
covering 14 distinct logical pairs = 3.50x inflation**, and 43 of the 49
scored exactly 0.545: one score smeared across mirror spellings. The
consolidation arm grades itself on ``link_edges``, so it read 3.5x more
connectivity than it had while 11,010 orphans went untouched.

WHY THESE TESTS ARE SHAPED THIS WAY
-----------------------------------
Two assertions, and the second is the anti-hack:

(a) ratio -- raw candidates / distinct logical pairs == 1.00.
(b) coverage -- the run still spends its whole ``limit`` on REAL pairs.

Dedupe placed AFTER the cap satisfies (a) while HALVING (b): 50 raw edges
collapse to ~14 real ones and the metric looks repaired while coverage
drops. That is gaming the number, not fixing it. Hence the cap-pressure
test below runs with limit < available logical pairs and demands the cap
be filled with distinct pairs.

No Neo4j: this is a pure-function property of the pair-collapse logic, so
it runs against a fake graph. That keeps it fast, deterministic, and
incapable of wiping a production corpus (see conftest's two guards -- the
karpathy graph was nuked twice by dev pytest runs in April 2026).
"""

from __future__ import annotations

import pytest

from memfs.dream import _canonical_path, find_content_similar_unlinked

MIRROR = "mvm/knowledge/"


class FakeGraph:
    """Minimal stand-in: serves the node scan, reports no existing LINKs."""

    def __init__(self, nodes: list[dict], linked: set[tuple[str, str]] | None = None):
        self._nodes = nodes
        self._linked = linked or set()
        self.existence_queries = 0

    def run(self, cypher: str, **params):
        assert "MATCH (n:Node)" in cypher
        return list(self._nodes)

    def run_scalar(self, cypher: str, **params):
        self.existence_queries += 1
        pa = params.get("pa") or []
        pb = params.get("pb") or []
        for a in pa:
            for b in pb:
                if (a, b) in self._linked or (b, a) in self._linked:
                    return True
        return False


def _corpus(n_groups: int = 20, *, mirrored: bool = True) -> list[dict]:
    """n_groups pairs of related notes, each optionally mirrored.

    Within a group the two notes share 10 group-specific tokens and carry
    10 unique ones each => jaccard 10/30 = 0.333, inside the finder's
    [min_score, max_score) window. Across groups the overlap is zero, so
    the only real link candidates are the n_groups intra-group pairs.
    """
    nodes: list[dict] = []
    for g in range(n_groups):
        shared = " ".join(f"grouptoken{g}x{k}" for k in range(10))
        for side in ("a", "b"):
            uniq = " ".join(f"uniqtoken{g}{side}y{k}" for k in range(10))
            path = f"resources/topic{g}/{side}.md"
            body = {"path": path, "title": "", "description": "",
                    "content": f"{shared} {uniq}", "layer": 3}
            nodes.append(body)
            if mirrored:
                nodes.append({**body, "path": MIRROR + path})
    return nodes


def _logical_key(cand: dict) -> tuple[str, str]:
    a, b = (_canonical_path(p) for p in cand["nodes"])
    return (a, b) if a < b else (b, a)


def test_canonical_path_strips_mirror_prefix_only():
    assert _canonical_path(MIRROR + "resources/x.md") == "resources/x.md"
    assert _canonical_path("resources/x.md") == "resources/x.md"
    # Not a prefix match -- must be left alone.
    assert _canonical_path("other/mvm/knowledge/x.md") == "other/mvm/knowledge/x.md"


def test_mirror_twins_collapse_to_one_candidate_per_logical_pair():
    """(a) ratio: raw candidates / distinct logical pairs == 1.00x."""
    g = FakeGraph(_corpus(20))
    out = find_content_similar_unlinked(g, limit=50)

    logical = {_logical_key(c) for c in out}
    assert out, "finder produced no candidates -- corpus fixture is broken"
    ratio = len(out) / len(logical)
    assert ratio == 1.0, (
        f"{len(out)} raw candidates over {len(logical)} logical pairs "
        f"= {ratio:.2f}x inflation (baseline bug was 3.50x)"
    )
    assert len(logical) == 20


def test_dedupe_happens_before_the_cap_not_after():
    """(b) anti-hack: a limit-bound run spends every slot on a REAL pair.

    Dedupe-after-cap would return ~limit/4 logical pairs here. Only
    dedupe-before-cap fills the cap with distinct pairs.
    """
    limit = 8
    g = FakeGraph(_corpus(20))
    out = find_content_similar_unlinked(g, limit=limit)

    logical = {_logical_key(c) for c in out}
    assert len(out) == limit, f"cap under-spent: {len(out)} < {limit}"
    assert len(logical) == limit, (
        f"cap spent on only {len(logical)} real pairs -- dedupe is running "
        "after the cap, which fakes the ratio by halving coverage"
    )


def test_mirrored_corpus_covers_same_pairs_as_unmirrored():
    """Mirroring must not change WHICH logical pairs get proposed."""
    plain = find_content_similar_unlinked(FakeGraph(_corpus(20, mirrored=False)),
                                          limit=50)
    mirrored = find_content_similar_unlinked(FakeGraph(_corpus(20)), limit=50)
    assert {_logical_key(c) for c in plain} == {_logical_key(c) for c in mirrored}


def test_node_never_linked_to_its_own_mirror():
    g = FakeGraph(_corpus(20))
    for cand in find_content_similar_unlinked(g, limit=50):
        a, b = cand["nodes"]
        assert _canonical_path(a) != _canonical_path(b), (
            f"self-link across mirror spellings: {a} <-> {b}"
        )


def test_existing_link_recognised_under_any_mirror_spelling():
    """Convergence: an edge stored under a mirror spelling must suppress the
    candidate. Per-spelling existence checks are what let one pair be
    re-applied on every run."""
    corpus = _corpus(20)
    # Edge recorded under the FULLY-mirrored spelling of group 0's pair.
    linked = {(MIRROR + "resources/topic0/a.md", MIRROR + "resources/topic0/b.md")}
    out = find_content_similar_unlinked(FakeGraph(corpus, linked), limit=50)
    keys = {_logical_key(c) for c in out}
    assert ("resources/topic0/a.md", "resources/topic0/b.md") not in keys


def test_representative_spelling_is_stable_and_prefers_primary_tree():
    """Same input => same emitted spelling, and it is the non-mirror one.

    An unstable representative never converges: run N applies spelling A,
    run N+1 emits spelling B, the already-linked check misses, forever.
    """
    first = find_content_similar_unlinked(FakeGraph(_corpus(20)), limit=50)
    second = find_content_similar_unlinked(FakeGraph(_corpus(20)), limit=50)
    assert [c["nodes"] for c in first] == [c["nodes"] for c in second]
    for cand in first:
        for p in cand["nodes"]:
            assert not p.startswith(MIRROR), (
                f"emitted mirror spelling {p} while a primary-tree path exists"
            )
