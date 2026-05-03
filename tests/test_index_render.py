"""Tests for index_render — drift detection between graph, index.md, and filesystem.

Critical regression test: the n=1 watch
`memfs-detector-blind-to-fs-drift` was filed because
`memfs check-indexes` returned 0 drift while index.md referenced 53
phantom files (in graph but missing from disk). The detector compared
graph-vs-index, never index-vs-filesystem. These tests pin filesystem
ground-truth into the contract.
"""

import os
import pytest

from memfs.index_render import (
    check_drift_for_dir,
    render_index_for_dir,
)
from memfs.indexer import index_file


class TestPhantomFiles:
    """Filesystem is ground truth. Graph staleness must not silence drift."""

    def test_phantom_file_in_graph_and_index_but_not_on_disk(self, graph, tmp_path):
        """Reproduces n=1 watch: graph holds stale entry, index references it,
        file doesn't exist on disk. Detector MUST flag."""
        # Real file gets indexed; graph and disk agree.
        real = tmp_path / "real.md"
        real.write_text("# Real\nA real file.")
        index_file(graph, str(tmp_path), "real.md")

        # Now simulate the bug: a phantom file gets into the graph
        # (e.g. session wrote then was rolled back / file deleted out of band)
        # while index.md still references it.
        phantom = tmp_path / "phantom.md"
        phantom.write_text("# Phantom\nWill vanish.")
        index_file(graph, str(tmp_path), "phantom.md")
        phantom.unlink()  # file gone, but graph still has it

        # Render an index from the (now stale) graph — this is what auto-render does.
        index_text = render_index_for_dir(graph, str(tmp_path), "")
        (tmp_path / "index.md").write_text(index_text + "\n")

        findings = check_drift_for_dir(graph, str(tmp_path), "")

        # The phantom MUST be flagged. Pre-fix: silently returned [].
        assert any("phantom.md" in f for f in findings), (
            f"Phantom not flagged. Findings: {findings}"
        )

    def test_phantom_file_in_index_only_no_graph(self, graph, tmp_path):
        """Index references a file that's neither on disk nor in graph.
        Detector MUST flag (covers handcrafted indexes with stale entries)."""
        # Write only an index.md with a phantom reference — graph empty.
        (tmp_path / "index.md").write_text(
            "# Index\n\n## Files\n\n| File | Description |\n|---|---|\n"
            "| `phantom.md` | does not exist |\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")

        # Pre-fix early-exit (no graph content) silently returned [].
        assert any("phantom.md" in f for f in findings), (
            f"Index-only phantom not flagged. Findings: {findings}"
        )

    def test_real_file_no_drift(self, graph, tmp_path):
        """Sanity check: when graph + index + disk all agree, no drift."""
        real = tmp_path / "real.md"
        real.write_text("# Real")
        index_file(graph, str(tmp_path), "real.md")
        (tmp_path / "index.md").write_text(
            render_index_for_dir(graph, str(tmp_path), "") + "\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        assert findings == [], f"Unexpected drift: {findings}"


class TestPhantomSubdirs:
    """Subdir references must be checked for filesystem existence."""

    def test_phantom_subdir_reference(self, graph, tmp_path):
        """Index references `subdir/` but no such directory exists.
        Detector MUST flag."""
        # Need a real indexed file so the dir has graph content (otherwise
        # the early-exit branch — once fixed — will be the path under test
        # for the index-only case above).
        real = tmp_path / "real.md"
        real.write_text("# Real")
        index_file(graph, str(tmp_path), "real.md")

        (tmp_path / "index.md").write_text(
            "# Index\n\n## Files\n\n| File | Description |\n|---|---|\n"
            "| `real.md` | real |\n\n"
            "## Subdirectories\n\n| Dir | Index |\n|---|---|\n"
            "| `phantom-subdir/` | auto |\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        assert any("phantom-subdir" in f for f in findings), (
            f"Phantom subdir not flagged. Findings: {findings}"
        )

    def test_real_subdir_no_drift(self, graph, tmp_path):
        """Real subdir reference doesn't false-positive."""
        sub = tmp_path / "real-subdir"
        sub.mkdir()
        (sub / "child.md").write_text("# Child")
        index_file(graph, str(tmp_path), "real-subdir/child.md")

        # Render and write index; should pick up real-subdir/ correctly.
        (tmp_path / "index.md").write_text(
            render_index_for_dir(graph, str(tmp_path), "") + "\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        assert findings == [], f"Unexpected drift: {findings}"


class TestExternalReferencesNotFlagged:
    """Handcrafted indexes legitimately mention external commands and paths.
    These must NOT be flagged as missing files."""

    def test_slash_commands_skipped(self, graph, tmp_path):
        """`/telegram`, `/gmail` etc. are slash-prefixed external refs."""
        real = tmp_path / "real.md"
        real.write_text("# Real")
        index_file(graph, str(tmp_path), "real.md")

        (tmp_path / "index.md").write_text(
            "<!-- handcrafted -->\n# Skills\n\n"
            "| Skill | Replacement |\n|---|---|\n"
            "| `/telegram` | `send-telegram.sh \"msg\"` |\n"
            "| `/gmail` | `gog` |\n"
            "| `real.md` | the real file |\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        # /telegram and /gmail must not be flagged as missing
        assert not any("/telegram" in f for f in findings), findings
        assert not any("/gmail" in f for f in findings), findings
        assert not any("send-telegram.sh" in f for f in findings), findings

    def test_bare_command_names_skipped(self, graph, tmp_path):
        """`gog`, `kalshi`, `mon` — bare CLI names without `.md` or `/` suffix."""
        real = tmp_path / "real.md"
        real.write_text("# Real")
        index_file(graph, str(tmp_path), "real.md")

        (tmp_path / "index.md").write_text(
            "<!-- handcrafted -->\n# Capabilities\n\n"
            "| Request | Command |\n|---|---|\n"
            "| Send Telegram | `tg` |\n"
            "| Finances | `mon` |\n"
            "| `real.md` | the real file |\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        assert not any(name in str(findings) for name in ("`tg`", "`mon`")), findings

    def test_home_relative_paths_skipped(self, graph, tmp_path):
        """`~/mail/outbox/` is an external path, not a local subdir claim."""
        real = tmp_path / "real.md"
        real.write_text("# Real")
        index_file(graph, str(tmp_path), "real.md")

        (tmp_path / "index.md").write_text(
            "<!-- handcrafted -->\n# Index\n\n"
            "Drop files in `~/mail/outbox/`.\n\n"
            "| File | Description |\n|---|---|\n"
            "| `real.md` | the real file |\n"
        )

        findings = check_drift_for_dir(graph, str(tmp_path), "")
        assert not any("~/mail/outbox" in f for f in findings), findings
