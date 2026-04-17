"""Tests for temporal proximity scoring in search."""

import pytest

from memfs.indexer import index_file
from memfs.search import grep


@pytest.fixture
def temporal_graph(graph, tmp_path):
    files = {
        "jan.md": "---\ntitle: January Meeting\ndate: 2023-01-15\n---\n# January Meeting\nDiscussed project kickoff and timeline.",
        "apr.md": "---\ntitle: April Meeting\ndate: 2023-04-10\n---\n# April Meeting\nDiscussed project progress and milestones.",
        "dec.md": "---\ntitle: December Meeting\ndate: 2023-12-20\n---\n# December Meeting\nDiscussed project wrap-up and retrospective.",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
        index_file(graph, str(tmp_path), name)
    return graph


class TestTemporalBoost:
    def test_date_in_query_boosts_nearby_results(self, temporal_graph):
        results = grep(temporal_graph, "meeting project 2023-04-10")
        paths = [r["path"] for r in results]
        assert "apr.md" in paths
        if "dec.md" in paths:
            assert paths.index("apr.md") < paths.index("dec.md")

    def test_no_date_no_temporal_boost(self, temporal_graph):
        results = grep(temporal_graph, "meeting project")
        paths = [r["path"] for r in results]
        assert len(paths) >= 2
