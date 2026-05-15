"""INDEX.md auto-regeneration from children's frontmatter summaries."""
from pathlib import Path

from mvm.index import regenerate_folder_indexes


def _write_doc(path: Path, summary: str, kind: str = "canonical") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nkind: {kind}\nsummary: \"{summary}\"\n---\n\n# {path.stem}\n\nbody\n")


def test_regenerate_writes_index(tmp_path):
    _write_doc(tmp_path / "topic-a.md", "Summary of A.")
    _write_doc(tmp_path / "topic-b.md", "Summary of B.")
    n = regenerate_folder_indexes(tmp_path)
    assert n == 1
    idx = (tmp_path / "INDEX.md").read_text()
    assert "topic-a" in idx
    assert "topic-b" in idx
    assert "Summary of A." in idx
    assert "Summary of B." in idx
    assert "Auto-generated" in idx


def test_regenerate_lists_subfolders(tmp_path):
    _write_doc(tmp_path / "areas" / "poe2.md", "PoE2 stuff.")
    _write_doc(tmp_path / "resources" / "ref.md", "Reference.")
    regenerate_folder_indexes(tmp_path)
    # Root INDEX.md should not exist if no .md at root, OR list subdirs if it does
    # Both subfolders should have their own INDEX.md
    assert (tmp_path / "areas" / "INDEX.md").exists()
    assert (tmp_path / "resources" / "INDEX.md").exists()
    areas_idx = (tmp_path / "areas" / "INDEX.md").read_text()
    assert "poe2" in areas_idx
    assert "PoE2 stuff" in areas_idx


def test_regenerate_skips_index_md(tmp_path):
    """An old INDEX.md with stale content should not appear as a child of itself."""
    _write_doc(tmp_path / "topic-a.md", "Summary of A.")
    (tmp_path / "INDEX.md").write_text("stale content")
    regenerate_folder_indexes(tmp_path)
    idx = (tmp_path / "INDEX.md").read_text()
    # INDEX.md should NOT list itself as a child
    assert "INDEX" not in idx.split("\n", 1)[0]  # not in heading line referring to itself
    # Should contain topic-a
    assert "topic-a" in idx


def test_regenerate_skips_tests_yaml(tmp_path):
    _write_doc(tmp_path / "topic-a.md", "Summary of A.")
    (tmp_path / "topic-a.tests.yaml").write_text("- id: 1\n  q: x\n  a: y\n")
    regenerate_folder_indexes(tmp_path)
    idx = (tmp_path / "INDEX.md").read_text()
    # Should list topic-a but not the tests file (yaml not md anyway)
    assert "topic-a" in idx
    assert "tests.yaml" not in idx


def test_regenerate_handles_missing_summary(tmp_path):
    """Doc without summary frontmatter still gets an entry, no summary tail."""
    (tmp_path / "topic-a.md").write_text("---\nkind: canonical\n---\n\n# A\n")
    regenerate_folder_indexes(tmp_path)
    idx = (tmp_path / "INDEX.md").read_text()
    assert "topic-a" in idx
    # Line for topic-a should not have a stray " — " with no description
    line = next(ln for ln in idx.split("\n") if "topic-a" in ln)
    assert not line.endswith(" — ")
