"""Indexer — scan directories, index files, maintain edges."""

import os
from datetime import datetime, timezone
from fnmatch import fnmatch

from memfs.parser import parse_file, compute_hash
from memfs.paths import resolve_link, normalize_path

# Always ignored
HARDCODED_IGNORES = [".mem", ".mem/*", ".git", ".git/*", "node_modules",
                     "node_modules/*", "*.log", "*.tmp"]


def load_memignore(mem_home: str) -> list[str]:
    """Load .memignore patterns from MEM_HOME root."""
    patterns = list(HARDCODED_IGNORES)
    ignore_path = os.path.join(mem_home, ".memignore")
    if os.path.exists(ignore_path):
        with open(ignore_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line.rstrip("/"))
                    patterns.append(line.rstrip("/") + "/*")
    return patterns


def is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any ignore pattern."""
    for pat in patterns:
        if fnmatch(rel_path, pat):
            return True
        # Check each path component
        parts = rel_path.split(os.sep)
        for part in parts:
            if fnmatch(part, pat):
                return True
    return False


def index_file(conn, mem_home: str, rel_path: str) -> None:
    """Index a single file into the database."""
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.exists(abs_path):
        return

    parsed = parse_file(abs_path)
    now = _now()

    # Upsert node
    conn.execute(
        """INSERT INTO nodes (path, title, created_at, modified_at, content_hash, date_hint)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(path) DO UPDATE SET
               title=excluded.title,
               modified_at=excluded.modified_at,
               content_hash=excluded.content_hash,
               date_hint=excluded.date_hint""",
        (rel_path, parsed["title"], now, now, parsed["content_hash"], parsed["date_hint"]),
    )

    # Update FTS — delete old, insert new
    conn.execute("DELETE FROM fts WHERE path = ?", (rel_path,))
    conn.execute(
        "INSERT INTO fts (path, title, content) VALUES (?, ?, ?)",
        (rel_path, parsed["title"], parsed["content"]),
    )

    # Process links — remove old link edges from this source, add new ones
    conn.execute("DELETE FROM edges WHERE source = ? AND type = 'link'", (rel_path,))
    for link_target in parsed["links"]:
        resolved = resolve_link(link_target, rel_path, mem_home)
        # Check if target exists
        target_exists = os.path.exists(os.path.join(mem_home, resolved))
        strength = 1.0 if target_exists else 0.0
        conn.execute(
            """INSERT OR REPLACE INTO edges (source, target, type, strength, created_at)
               VALUES (?, ?, 'link', ?, ?)""",
            (rel_path, resolved, strength, now),
        )

    conn.commit()


def update_node(conn, mem_home: str, rel_path: str) -> bool:
    """Update a node if its content has changed. Returns True if updated."""
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.exists(abs_path):
        return False

    with open(abs_path, encoding="utf-8") as f:
        current_hash = compute_hash(f.read())

    row = conn.execute(
        "SELECT content_hash FROM nodes WHERE path = ?", (rel_path,)
    ).fetchone()

    if row and row[0] == current_hash:
        return False  # No change

    # Re-index the file
    index_file(conn, mem_home, rel_path)
    return True


def index_directory(conn, mem_home: str) -> int:
    """Index all .md files in a directory tree. Returns count of files indexed."""
    patterns = load_memignore(mem_home)
    count = 0

    for dirpath, dirnames, filenames in os.walk(mem_home):
        # Filter out ignored directories
        rel_dir = os.path.relpath(dirpath, mem_home)
        if rel_dir != "." and is_ignored(rel_dir, patterns):
            dirnames.clear()
            continue

        # Prune ignored subdirs from walk
        dirnames[:] = [
            d for d in dirnames
            if not is_ignored(
                os.path.relpath(os.path.join(dirpath, d), mem_home), patterns
            )
        ]

        for filename in filenames:
            if not filename.endswith(".md"):
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, filename), mem_home)
            if is_ignored(rel_path, patterns):
                continue
            index_file(conn, mem_home, rel_path)
            count += 1

    return count


def reindex(conn, mem_home: str) -> int:
    """Drop all data and reindex from scratch."""
    conn.execute("DELETE FROM nodes")
    conn.execute("DELETE FROM edges WHERE type = 'link'")
    conn.execute("DELETE FROM fts")
    conn.execute("DELETE FROM embeddings")
    conn.commit()
    return index_directory(conn, mem_home)


def remove_file(conn, rel_path: str) -> None:
    """Remove a file from the index."""
    conn.execute("DELETE FROM edges WHERE source = ? OR target = ?", (rel_path, rel_path))
    conn.execute("DELETE FROM fts WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM embeddings WHERE path = ?", (rel_path,))
    conn.execute("DELETE FROM nodes WHERE path = ?", (rel_path,))
    conn.commit()


def rename_path(conn, old_prefix: str, new_prefix: str) -> None:
    """Update all paths matching old_prefix to new_prefix (for directory renames)."""
    conn.execute(
        "UPDATE nodes SET path = replace(path, ?, ?) WHERE path LIKE ?",
        (old_prefix, new_prefix, old_prefix + "%"),
    )
    conn.execute(
        "UPDATE edges SET source = replace(source, ?, ?) WHERE source LIKE ?",
        (old_prefix, new_prefix, old_prefix + "%"),
    )
    conn.execute(
        "UPDATE edges SET target = replace(target, ?, ?) WHERE target LIKE ?",
        (old_prefix, new_prefix, old_prefix + "%"),
    )
    conn.execute(
        "UPDATE fts SET path = replace(path, ?, ?) WHERE path LIKE ?",
        (old_prefix, new_prefix, old_prefix + "%"),
    )
    conn.commit()


def upgrade_broken_links(conn, rel_path: str) -> int:
    """When a file is created, upgrade any broken link edges pointing to it."""
    cursor = conn.execute(
        "UPDATE edges SET strength = 1.0 WHERE target = ? AND strength = 0",
        (rel_path,),
    )
    conn.commit()
    return cursor.rowcount


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
