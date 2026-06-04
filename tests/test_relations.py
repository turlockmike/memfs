"""Typed semantic relation edges — LOCKED TEST (S721, 2026-06-04).

Immutable after authoring (ARCHITECTURE invariant #3). Covers the three gaps
that the S720 grounding identified in ~/resources/infrastructure/mvm-typed-edges-design.md:

  Gap 0 — frontmatter relation targets written as `~/resources/...` (the mirror
          path with a literal ~/ prefix) must NORMALIZE to knowledge-root-relative
          `resources/...` so the edge resolves to a real graph node. The 6 live
          `frontmatter_superseded_by` edges were all dangling because of this.
  Gap 1 — the relation-key allowlist must include the forward/peer relations
          `supersedes`, `in_tension_with`, `derived_from` (previously dropped).
  Gap 2 — a mechanical CONSUMER (`mvm relations <doc>`) must return a doc's typed
          relation neighborhood from graph.db, in BOTH directions, so mvm-dream can
          query typed edges instead of re-reading prose. (no-sinks: producer Gaps
          0+1 ship together with this consumer.)
"""
import sqlite3
from pathlib import Path

from mvm.index import extract_edges
from mvm.relations import query_relations


# ---------------------------------------------------------------- Gap 0 + Gap 1
def _edges_for(body: str, fm: dict, root: Path):
    """Run extract_edges for a doc at <root>/sub/doc-a.md."""
    src = root / "resources" / "poe2" / "doc-a.md"
    return extract_edges(src, body, fm, root)


def test_gap0_tilde_resources_target_normalizes_to_root_relative():
    """`~/resources/.../B.md` must store as root-relative `resources/.../B.md`."""
    root = Path("/home/mike/mvm/knowledge")
    edges = _edges_for("# A\n", {"superseded_by": "~/resources/poe2/B.md"}, root)
    supers = [e for e in edges if e[2] == "frontmatter_superseded_by"]
    assert supers, "superseded_by edge must be emitted"
    assert supers[0][1] == "resources/poe2/B.md", (
        f"dst must normalize to root-relative, got {supers[0][1]!r}"
    )


def test_gap0_mirror_knowledge_path_normalizes():
    """`~/mvm/knowledge/resources/B.md` also normalizes to `resources/B.md`."""
    root = Path("/home/mike/mvm/knowledge")
    edges = _edges_for("# A\n", {"superseded_by": "~/mvm/knowledge/resources/B.md"}, root)
    supers = [e for e in edges if e[2] == "frontmatter_superseded_by"]
    assert supers and supers[0][1] == "resources/B.md", supers


def test_gap0_already_relative_target_unchanged():
    """An already root-relative target is preserved verbatim (no double-mangling)."""
    root = Path("/home/mike/mvm/knowledge")
    edges = _edges_for("# A\n", {"superseded_by": "resources/poe2/B.md"}, root)
    supers = [e for e in edges if e[2] == "frontmatter_superseded_by"]
    assert supers and supers[0][1] == "resources/poe2/B.md", supers


def test_gap1_new_relation_keys_emit_typed_edges():
    """supersedes / in_tension_with / derived_from must now emit typed edges."""
    root = Path("/home/mike/mvm/knowledge")
    fm = {
        "supersedes": "resources/old.md",
        "in_tension_with": ["resources/x.md", "resources/y.md"],
        "derived_from": "resources/src.md",
    }
    edges = _edges_for("# A\n", fm, root)
    types = {e[2] for e in edges}
    assert "frontmatter_supersedes" in types
    assert "frontmatter_in_tension_with" in types
    assert "frontmatter_derived_from" in types
    # list value yields one edge per item
    tensions = [e[1] for e in edges if e[2] == "frontmatter_in_tension_with"]
    assert set(tensions) == {"resources/x.md", "resources/y.md"}


# ----------------------------------------------------------------------- Gap 2
def _mk_graph(tmp_path) -> Path:
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE edges (src TEXT, dst TEXT, edge_type TEXT,
                            PRIMARY KEY (src, dst, edge_type));
    """)
    conn.executemany(
        "INSERT INTO edges (src, dst, edge_type) VALUES (?, ?, ?)",
        [
            ("resources/a.md", "resources/b.md", "frontmatter_superseded_by"),
            ("resources/a.md", "resources/c.md", "frontmatter_in_tension_with"),
            ("resources/a.md", "https://example.com", "external_url"),  # non-semantic
            ("resources/z.md", "resources/a.md", "frontmatter_supersedes"),  # inbound
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_gap2_outbound_and_inbound_semantic_relations(tmp_path):
    db = _mk_graph(tmp_path)
    rows = query_relations(db, "resources/a.md")
    # outbound semantic
    assert ("frontmatter_superseded_by", "out", "resources/b.md") in rows
    assert ("frontmatter_in_tension_with", "out", "resources/c.md") in rows
    # inbound semantic
    assert ("frontmatter_supersedes", "in", "resources/z.md") in rows
    # non-semantic edge_type (external_url) must be EXCLUDED
    assert all(r[0] != "external_url" for r in rows)


def test_gap2_rel_filter(tmp_path):
    db = _mk_graph(tmp_path)
    rows = query_relations(db, "resources/a.md", rel="superseded_by")
    assert rows == [("frontmatter_superseded_by", "out", "resources/b.md")]


def test_gap2_missing_db_returns_empty(tmp_path):
    assert query_relations(tmp_path / "nope.db", "resources/a.md") == []
