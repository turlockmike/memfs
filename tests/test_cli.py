"""Top-level CLI dispatcher."""
from unittest import mock

from mvm.cli import main


def test_help_shown_on_no_args(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage:" in out
    for sub in ("verify", "index", "search", "stats"):
        assert sub in out


def test_help_flag(capsys):
    rc = main(["--help"])
    assert rc == 0
    assert "Usage:" in capsys.readouterr().out


def test_unknown_subcommand(capsys):
    rc = main(["frobnicate"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "unknown subcommand" in err.lower()


def test_dispatches_to_subcommand():
    """Verify dispatcher routes argv[1:] to the right module."""
    with mock.patch("mvm.search.main", return_value=42) as m:
        rc = main(["search", "some query", "--top-k", "3"])
    assert rc == 42
    m.assert_called_once_with(["some query", "--top-k", "3"])
