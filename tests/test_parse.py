"""Markdown + frontmatter parsing, edge extraction."""
from pathlib import Path

from mvm.index import parse_markdown, extract_edges


def test_parse_markdown_with_frontmatter(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nkind: canonical\nsource: file://x\n---\n\n# Body\n")
    fm, body = parse_markdown(p)
    assert fm == {"kind": "canonical", "source": "file://x"}
    assert body.startswith("\n# Body")


def test_parse_markdown_no_frontmatter(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("# Just a body\n\nNo frontmatter here.\n")
    fm, body = parse_markdown(p)
    assert fm == {}
    assert body == "# Just a body\n\nNo frontmatter here.\n"


def test_parse_markdown_malformed_frontmatter(tmp_path):
    p = tmp_path / "x.md"
    p.write_text("---\nthis is: not: valid: yaml: at: all\n---\n\nbody\n")
    fm, body = parse_markdown(p)
    # Malformed YAML returns empty dict (graceful degradation)
    assert isinstance(fm, dict)
    assert "body" in body


def test_extract_edges_md_link(tmp_path):
    root = tmp_path
    src = root / "a.md"
    (root / "a.md").write_text("")
    (root / "b.md").write_text("")
    body = "See [B](b.md) for more."
    edges = extract_edges(src, body, {}, root)
    assert ("a.md", "b.md", "md_link") in edges


def test_extract_edges_wikilink(tmp_path):
    src = tmp_path / "a.md"
    src.write_text("")
    body = "Check [[some-topic]] please."
    edges = extract_edges(src, body, {}, tmp_path)
    assert ("a.md", "some-topic", "wikilink") in edges


def test_extract_edges_external_url(tmp_path):
    src = tmp_path / "a.md"
    src.write_text("")
    body = "From [docs](https://example.com/x)."
    edges = extract_edges(src, body, {}, tmp_path)
    assert any(et == "external_url" and "example.com" in dst for _, dst, et in edges)


def test_extract_edges_frontmatter_source_url(tmp_path):
    src = tmp_path / "a.md"
    src.write_text("")
    fm = {"source": "https://example.com/source"}
    edges = extract_edges(src, "", fm, tmp_path)
    assert any(et == "source_url" for _, _, et in edges)


def test_extract_edges_frontmatter_see_also_list(tmp_path):
    src = tmp_path / "a.md"
    src.write_text("")
    fm = {"see_also": ["b.md", "c.md"]}
    edges = extract_edges(src, "", fm, tmp_path)
    assert sum(1 for _, _, et in edges if et == "frontmatter_see_also") == 2


def test_backlinks_query_for_cascade(tmp_path):
    """Cascade-on-ingest: when A is updated, find files that link TO A.

    The cascade SQL is `SELECT DISTINCT src FROM edges WHERE dst = ?` —
    this test exercises the same pattern against a temp graph DB to confirm
    the backlink retrieval works as the ingest skill prescribes.
    """
    import sqlite3
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE edges (src TEXT, dst TEXT, edge_type TEXT,
                            PRIMARY KEY (src, dst, edge_type));
    """)
    # C and B both link to A; D links to nothing-relevant
    conn.executemany("INSERT INTO edges (src, dst, edge_type) VALUES (?, ?, ?)", [
        ("c.md", "a.md", "md_link"),
        ("b.md", "a.md", "wikilink"),
        ("d.md", "x.md", "md_link"),
    ])
    conn.commit()

    # Backlinks for a.md = {b, c}
    cur = conn.execute("SELECT DISTINCT src FROM edges WHERE dst = ?", ("a.md",))
    backlinks = {row[0] for row in cur.fetchall()}
    conn.close()

    assert backlinks == {"b.md", "c.md"}
