#!/usr/bin/env python3
"""memfs — Unix-native memory filesystem for LLM agents.

Agent prompt (3 sentences):
  Your memory lives in $MEM_HOME. Read and write files normally with any tool.
  Use `memfs grep <query>` to search — connections strengthen when you search
  and weaken over time.
"""

import argparse
import json
import os
import sys

from memfs.db import create_db, connect
from memfs.indexer import index_directory, reindex as do_reindex
from memfs.search import grep as do_grep


def out(obj):
    """Print NDJSON line to stdout."""
    print(json.dumps(obj))


def err(obj):
    """Print NDJSON error to stderr."""
    print(json.dumps(obj), file=sys.stderr)


def get_mem_home(args=None):
    """Resolve MEM_HOME from args or environment."""
    if args and hasattr(args, "dir") and args.dir:
        return os.path.abspath(args.dir)
    return os.environ.get("MEM_HOME", os.getcwd())


def get_db_path(mem_home):
    return os.path.join(mem_home, ".mem", "memory.db")


# --- Commands ---

def cmd_init(args):
    mem_home = os.path.abspath(args.dir) if args.dir else os.getcwd()
    db_path = get_db_path(mem_home)

    create_db(db_path)
    conn = connect(db_path)
    count = index_directory(conn, mem_home)
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()

    out({"action": "init", "mem_home": mem_home, "nodes": count, "edges": edges})


def cmd_grep(args):
    mem_home = get_mem_home(args)
    db_path = get_db_path(mem_home)

    if not os.path.exists(db_path):
        err({"error": "not_initialized", "hint": f"run memfs init {mem_home}"})
        sys.exit(1)

    conn = connect(db_path)
    results = do_grep(conn, args.query, limit=args.limit)
    conn.close()

    for r in results:
        out(r)


def cmd_ls(args):
    mem_home = get_mem_home(args)
    db_path = get_db_path(mem_home)

    if not os.path.exists(db_path):
        err({"error": "not_initialized", "hint": f"run memfs init {mem_home}"})
        sys.exit(1)

    conn = connect(db_path)

    # Filter by subdirectory if provided
    subdir = args.subdir
    if subdir:
        subdir = subdir.rstrip("/")
        rows = conn.execute(
            "SELECT path, title FROM nodes WHERE path LIKE ? ORDER BY path",
            (subdir + "/%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT path, title FROM nodes ORDER BY path").fetchall()

    if args.verbose:
        for row in rows:
            path = row[0]
            links_out = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE source = ? AND type = 'link'", (path,)
            ).fetchone()[0]
            links_in = conn.execute(
                "SELECT COUNT(*) FROM edges WHERE target = ? AND type = 'link'", (path,)
            ).fetchone()[0]
            search_hits = conn.execute(
                "SELECT search_count FROM nodes WHERE path = ?", (path,)
            ).fetchone()[0]
            out({"path": path, "title": row[1], "links_out": links_out,
                 "links_in": links_in, "search_hits": search_hits or 0})
    else:
        for row in rows:
            out({"path": row[0]})

    conn.close()


def cmd_status(args):
    mem_home = get_mem_home(args)
    db_path = get_db_path(mem_home)

    if not os.path.exists(db_path):
        err({"error": "not_initialized", "hint": f"run memfs init {mem_home}"})
        sys.exit(1)

    conn = connect(db_path)
    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    link_edges = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type='link'"
    ).fetchone()[0]
    search_edges = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type='search'"
    ).fetchone()[0]
    queries = conn.execute("SELECT COUNT(*) FROM queries").fetchone()[0]

    last_index = conn.execute(
        "SELECT value FROM meta WHERE key='last_index'"
    ).fetchone()
    last_decay = conn.execute(
        "SELECT value FROM meta WHERE key='last_decay'"
    ).fetchone()

    conn.close()

    out({
        "nodes": nodes,
        "edges": {"link": link_edges, "search": search_edges},
        "queries": queries,
        "last_index": last_index[0] if last_index else None,
        "last_decay": last_decay[0] if last_decay else None,
    })


def cmd_reindex(args):
    mem_home = get_mem_home(args)
    db_path = get_db_path(mem_home)

    if not os.path.exists(db_path):
        err({"error": "not_initialized", "hint": f"run memfs init {mem_home}"})
        sys.exit(1)

    conn = connect(db_path)
    count = do_reindex(conn, mem_home)
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()

    out({"action": "reindex", "nodes": count, "edges": edges})


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        prog="memfs",
        description="Unix-native memory filesystem for LLM agents.",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize a memory root")
    p_init.add_argument("dir", nargs="?", default=None, help="Directory to initialize")

    # grep
    p_grep = sub.add_parser("grep", help="Search memory (agent's primary command)")
    p_grep.add_argument("query", help="Search query")
    p_grep.add_argument("--limit", type=int, default=20, help="Max results")

    # ls
    p_ls = sub.add_parser("ls", help="List indexed files")
    p_ls.add_argument("subdir", nargs="?", default=None, help="Subdirectory to list")
    p_ls.add_argument("--verbose", "-v", action="store_true", help="Show edge counts")

    # status
    sub.add_parser("status", help="Show index statistics")

    # reindex
    sub.add_parser("reindex", help="Rebuild index from files")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "grep": cmd_grep,
        "ls": cmd_ls,
        "status": cmd_status,
        "reindex": cmd_reindex,
    }

    try:
        commands[args.command](args)
    except Exception as e:
        err({"error": str(e), "type": type(e).__name__})
        sys.exit(2)


if __name__ == "__main__":
    main()
