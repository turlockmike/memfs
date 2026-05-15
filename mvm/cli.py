"""mvm — single-CLI dispatcher with subcommands."""
from __future__ import annotations

import argparse
import sys


SUBCOMMANDS = {
    "verify": "mvm.verify",
    "index": "mvm.index",
    "search": "mvm.search",
    "stats": "mvm.stats",
    "watch": "mvm.watch",
    "backlinks": "mvm.backlinks",
    "log": "mvm.log",
}


HELP = """\
mvm — minimum viable memory

Usage:
  mvm <subcommand> [args...]

Subcommands:
  verify  Cold-clone verify a markdown doc against its locked tests
  index   Walk the knowledge tree, build FTS + vector + graph indexes
  search  Tri-mode retrieval (vector + graph + hierarchy + kind filter)
  stats   Decoherence dashboard from recall-log.jsonl
  watch     Daemon: passively embed new/changed docs after a debounce window
  backlinks List files that link TO a given path (used by /mvm-ingest cascade)
  log       Show recall-log or dream-log entries

Run `mvm <subcommand> --help` for subcommand usage.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(HELP)
        return 0
    sub = argv[0]
    if sub not in SUBCOMMANDS:
        print(f"mvm: unknown subcommand {sub!r}\n", file=sys.stderr)
        print(HELP, file=sys.stderr)
        return 2
    import importlib
    mod = importlib.import_module(SUBCOMMANDS[sub])
    return int(mod.main(argv[1:]) or 0)


if __name__ == "__main__":
    sys.exit(main())
