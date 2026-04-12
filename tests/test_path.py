"""Tests for path resolution and normalization."""

from memfs.paths import normalize_path, resolve_link


class TestNormalizePath:
    def test_relative_stays_relative(self):
        assert normalize_path("projects/satori.md", "/home/mem") == "projects/satori.md"

    def test_absolute_becomes_relative(self):
        result = normalize_path("/home/mem/projects/satori.md", "/home/mem")
        assert result == "projects/satori.md"

    def test_rejects_dotdot(self):
        """Paths with .. are not allowed in stored paths."""
        import pytest
        with pytest.raises(ValueError, match="not allowed"):
            normalize_path("../outside/file.md", "/home/mem")

    def test_strips_leading_slash(self):
        result = normalize_path("/home/mem/file.md", "/home/mem")
        assert not result.startswith("/")

    def test_strips_trailing_slash(self):
        result = normalize_path("projects/", "/home/mem")
        assert not result.endswith("/")


class TestResolveLink:
    def test_simple_link(self):
        result = resolve_link("target.md", "source.md", "/home/mem")
        assert result == "target.md"

    def test_link_from_subdir(self):
        result = resolve_link("sibling.md", "projects/source.md", "/home/mem")
        assert result == "projects/sibling.md"

    def test_link_with_path(self):
        result = resolve_link("people/ken.md", "projects/source.md", "/home/mem")
        assert result == "people/ken.md"

    def test_adds_md_extension(self):
        """Links without .md extension should resolve with it."""
        result = resolve_link("target", "source.md", "/home/mem")
        assert result == "target.md"
