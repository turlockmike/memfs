"""Lock down the isolation invariants of mvm.verify.claude_subprocess.

These tests do NOT actually spawn `claude`; they patch subprocess.run and
inspect the call args. The point is to fail loudly if someone removes the
HOME redirect, the clean-cwd shape, or the defensive flags — silently
re-introducing contamination would break the entire MVM closed-loop guarantee.

Run with: cd ~/mvm && python3 -m pytest test/test_verify_isolation.py -v
(or: python3 -m unittest test/test_verify_isolation.py)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Ensure we import the in-tree mvm package, not anything site-installed.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mvm import verify  # noqa: E402


def _fake_run_ok(stdout="ok"):
    """Return a fake subprocess.run that always succeeds with given stdout."""
    def _run(*args, **kwargs):
        return mock.Mock(returncode=0, stdout=stdout, stderr="")
    return _run


class DefaultIsolationPathTests(unittest.TestCase):
    """Default path = OAuth-compatible: HOME redirect + clean cwd + defensive flags."""

    def setUp(self):
        # Strip env vars that could flip into the bare path.
        self._saved = {}
        for k in ("MVM_FORCE_BARE", "MVM_BARE", "ANTHROPIC_API_KEY"):
            if k in os.environ:
                self._saved[k] = os.environ.pop(k)

    def tearDown(self):
        for k, v in self._saved.items():
            os.environ[k] = v

    def test_cwd_is_a_tmpdir(self):
        captured = {}
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        cwd = captured["kwargs"].get("cwd")
        self.assertIsNotNone(cwd, "cwd must be set on default-path spawn")
        # tempfile.TemporaryDirectory uses the system tmp prefix; on Linux
        # that's typically /tmp. Just check the path exists in a tmp tree.
        self.assertTrue(
            "/tmp/" in cwd or cwd.startswith("/var/folders") or "mvm-verify-" in cwd,
            f"cwd should be a tempdir, got: {cwd}",
        )
        self.assertIn("mvm-verify-", cwd)

    def test_home_is_redirected_with_credentials_symlinked(self):
        captured = {}
        def _run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["cwd"] = kwargs.get("cwd")
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        env = captured["env"]
        self.assertIsNotNone(env)
        # HOME redirect: should equal the cwd (both = the per-call tempdir).
        self.assertEqual(env.get("HOME"), captured["cwd"])
        # If real ~/.claude/.credentials.json exists, symlink should be in
        # the redirected HOME. We cannot inspect the symlink after the
        # contextmanager exits (TemporaryDirectory is auto-cleaned on exit),
        # but we can prove the directory at least existed during the call:
        # subprocess.run was called inside the contextmanager scope.
        # Also check that contamination-bearing vars are stripped.
        self.assertNotIn("CLAUDE_PROJECT_DIR", env)
        self.assertNotIn("CLAUDE_AGENT", env)
        self.assertNotIn("CLAUDE_CODE_AGENT", env)

    def test_argv_contains_defensive_flags(self):
        captured = {}
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        cmd = captured["cmd"]
        # Defensive isolation flags.
        self.assertIn("--setting-sources", cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        # Existing isolation contracts.
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--system-prompt", cmd)
        # MUST NOT have --bare on the default path.
        self.assertNotIn("--bare", cmd)


class BareFastPathTests(unittest.TestCase):
    """Opt-in --bare path. Engages only with MVM_FORCE_BARE=1 + ANTHROPIC_API_KEY."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in
                       ("MVM_FORCE_BARE", "MVM_BARE", "ANTHROPIC_API_KEY")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_force_bare_without_api_key_falls_back_to_default(self):
        os.environ["MVM_FORCE_BARE"] = "1"
        # No ANTHROPIC_API_KEY → cannot engage --bare.
        captured = {}
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        self.assertNotIn("--bare", captured["cmd"])
        # Default path: cwd must be a tmpdir.
        self.assertIsNotNone(captured["kwargs"].get("cwd"))

    def test_force_bare_with_api_key_engages_bare(self):
        os.environ["MVM_FORCE_BARE"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-fake-key"
        captured = {}
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        self.assertIn("--bare", captured["cmd"])
        # Bare path doesn't use cwd/env redirect — it trusts --bare.
        self.assertIsNone(captured["kwargs"].get("cwd"))

    def test_legacy_mvm_bare_alias_still_works(self):
        os.environ["MVM_BARE"] = "1"
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-fake-key"
        captured = {}
        def _run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock.Mock(returncode=0, stdout="x", stderr="")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            verify.claude_subprocess("sys", "user")
        self.assertIn("--bare", captured["cmd"])


class FailureSurfacingTests(unittest.TestCase):
    """Failed subprocess must raise — never return silent garbage."""

    def setUp(self):
        for k in ("MVM_FORCE_BARE", "MVM_BARE", "ANTHROPIC_API_KEY"):
            os.environ.pop(k, None)

    def test_nonzero_exit_raises(self):
        def _run(*args, **kwargs):
            return mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch.object(verify.subprocess, "run", side_effect=_run):
            with self.assertRaises(RuntimeError) as cm:
                verify.claude_subprocess("sys", "user")
            self.assertIn("boom", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
