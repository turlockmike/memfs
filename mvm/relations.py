"""mvm relations <path> — a doc's TYPED semantic relation neighborhood.

The mechanical CONSUMER of frontmatter_* relation edges (S721). Lets mvm-dream's
contradiction/staleness audit QUERY typed edges (superseded_by / supersedes /
in_tension_with / derived_from / see_also / prereq / episodic_source) instead of
re-reading prose. Returns BOTH directions:

  out  src=<path>  ->  the docs <path> declares a relation TO
  in   dst=<path>  <-  the docs that declare a relation to <path>

Non-semantic edges (md_link / external_url / wikilink / source_ref ...) are
excluded — this is the typed-relation view only.

Output (default): one `edge_type<TAB>direction<TAB>other` line per relation.
With --json: a list of {edge_type, direction, other} objects.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))

SEMANTIC_PREFIX = "frontmatter_"


def query_relations(graph_db: Path, path: str, rel: str | None = None) -> list[tuple[str, str, str]]:
    """Return [(edge_type, direction, other)] for <path>'s semantic relations.

    direction is "out" (path is src) or "in" (path is dst). If rel is given
    (e.g. "superseded_by"), restrict to edge_type == f"frontmatter_{rel}".
    Returns [] if the db is missing. Read-only.
    """
    graph_db = Path(graph_db)
    if not graph_db.exists():
        return []
    if rel:
        type_clause = "edge_type = ?"
        type_arg = (f"{SEMANTIC_PREFIX}{rel}",)
    else:
        type_clause = "edge_type LIKE ? ESCAPE '\\'"
        type_arg = (f"{SEMANTIC_PREFIX.replace('_', chr(92) + '_')}%",)

    uri = f"file:{graph_db}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.OperationalError:
        return []
    rows: list[tuple[str, str, str]] = []
    try:
        for et, dst in conn.execute(
            f"SELECT edge_type, dst FROM edges WHERE src = ? AND {type_clause} ORDER BY edge_type, dst",
            (path, *type_arg),
        ):
            rows.append((et, "out", dst))
        for et, src in conn.execute(
            f"SELECT edge_type, src FROM edges WHERE dst = ? AND {type_clause} ORDER BY edge_type, src",
            (path, *type_arg),
        ):
            rows.append((et, "in", src))
    finally:
        conn.close()
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show a doc's typed semantic relation neighborhood.")
    parser.add_argument("path", help="Relative path (under knowledge/) of the doc.")
    parser.add_argument("--rel", help="Restrict to one relation (e.g. superseded_by, in_tension_with).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of TSV lines.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)

    graph_db = args.state / "graph.db"
    if not graph_db.exists():
        print(f"ERROR: graph.db not found at {graph_db}. Run `mvm index` first.", file=sys.stderr)
        return 2

    rows = query_relations(graph_db, args.path, rel=args.rel)
    if args.json:
        print(json.dumps([{"edge_type": et, "direction": d, "other": o} for et, d, o in rows]))
    else:
        for et, d, o in rows:
            print(f"{et}\t{d}\t{o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
