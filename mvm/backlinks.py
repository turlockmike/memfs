"""mvm backlinks <path> — list files that link TO <path>.

Used by /mvm-ingest's cascade step: when canonical X is updated, find files
that reference X (their tests may now be invalidated by X's change).

Output: one path per line.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List files that link TO the given path.")
    parser.add_argument("path", help="Relative path (under knowledge/) of the target doc.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)

    graph_db = args.state / "graph.db"
    if not graph_db.exists():
        print(f"ERROR: graph.db not found at {graph_db}. Run `mvm index` first.", file=sys.stderr)
        return 2

    # Open read-only with URI so reads don't contend with writers
    uri = f"file:{graph_db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.OperationalError as e:
        print(f"ERROR: cannot open graph.db: {e}", file=sys.stderr)
        return 4
    try:
        cur = conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? ORDER BY src", (args.path,)
        )
        for (src,) in cur.fetchall():
            print(src)
    finally:
        conn.close()
    return 0
