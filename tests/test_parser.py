"""Tests for file parsing — frontmatter, links, content hash."""

from memfs.parser import parse_file, extract_links, compute_hash


class TestParseFrontmatter:
    def test_extracts_title(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: My Note\ndate: 2026-04-12\n---\n# Content\nHello world")
        result = parse_file(str(f))
        assert result["title"] == "My Note"

    def test_extracts_date_hint(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\ndate: 2026-04-12\n---\nContent")
        result = parse_file(str(f))
        assert result["date_hint"] == "2026-04-12"

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("# Just a heading\nSome content")
        result = parse_file(str(f))
        assert result["title"] == "Just a heading"
        assert result["date_hint"] is None

    def test_title_from_heading_when_no_frontmatter_title(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ndate: 2026-01-01\n---\n# Heading Title\nContent")
        result = parse_file(str(f))
        assert result["title"] == "Heading Title"

    def test_title_from_filename_when_no_heading(self, tmp_path):
        f = tmp_path / "my-note.md"
        f.write_text("Just plain text, no heading")
        result = parse_file(str(f))
        assert result["title"] == "my-note"

    def test_content_without_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\n---\n# Heading\nBody text")
        result = parse_file(str(f))
        assert "Body text" in result["content"]
        assert "title: Test" not in result["content"]

    def test_content_hash(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Hello world")
        result = parse_file(str(f))
        assert result["content_hash"] == compute_hash("Hello world")


class TestExtractLinks:
    def test_single_link(self):
        links = extract_links("See [[projects/satori]]")
        assert links == ["projects/satori"]

    def test_multiple_links(self):
        links = extract_links("See [[a]] and [[b]]")
        assert set(links) == {"a", "b"}

    def test_aliased_link(self):
        links = extract_links("See [[target|display name]]")
        assert links == ["target"]

    def test_no_links(self):
        links = extract_links("No links here")
        assert links == []

    def test_link_with_md_extension(self):
        links = extract_links("See [[notes/foo.md]]")
        assert links == ["notes/foo.md"]

    def test_deduplicates(self):
        links = extract_links("[[a]] and [[a]] again")
        assert links == ["a"]


class TestComputeHash:
    def test_deterministic(self):
        assert compute_hash("hello") == compute_hash("hello")

    def test_different_content_different_hash(self):
        assert compute_hash("hello") != compute_hash("world")
