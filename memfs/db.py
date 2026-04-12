"""SQLite database operations for memfs."""

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS nodes (
    path          TEXT PRIMARY KEY,
    title         TEXT,
    description   TEXT,
    created_at    TEXT NOT NULL,
    modified_at   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    embedded_at   TEXT,
    last_searched TEXT,
    search_count  INTEGER DEFAULT 0,
    date_hint     TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    source         TEXT NOT NULL,
    target         TEXT NOT NULL,
    type           TEXT NOT NULL CHECK(type IN ('link', 'search')),
    strength       REAL NOT NULL DEFAULT 1.0,
    last_activated TEXT,
    access_count   INTEGER DEFAULT 0,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (source, target, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_strength ON edges(strength);

CREATE TABLE IF NOT EXISTS queries (
    id          TEXT PRIMARY KEY,
    query_text  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used   TEXT NOT NULL,
    use_count   INTEGER DEFAULT 1
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    path, title, content,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
    path       TEXT PRIMARY KEY,
    vector     BLOB NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def create_db(db_path: str) -> None:
    """Create the database with full schema."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    # Insert or update schema version
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('created_at', ?)",
        (_now(),),
    )
    conn.commit()
    conn.close()


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with WAL mode and row factory."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def add_node(
    conn: sqlite3.Connection,
    path: str,
    title: str,
    content_hash: str,
    date_hint: str | None,
) -> None:
    """Insert a new node. Raises IntegrityError on duplicate."""
    now = _now()
    conn.execute(
        """INSERT INTO nodes (path, title, created_at, modified_at, content_hash, date_hint)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (path, title, now, now, content_hash, date_hint),
    )
    conn.commit()


def get_node(conn: sqlite3.Connection, path: str) -> dict | None:
    """Get a node by path. Returns dict or None."""
    row = conn.execute("SELECT * FROM nodes WHERE path = ?", (path,)).fetchone()
    if row is None:
        return None
    return dict(row)


def remove_node(conn: sqlite3.Connection, path: str) -> None:
    """Remove a node and its edges and FTS entry."""
    conn.execute("DELETE FROM edges WHERE source = ? OR target = ?", (path, path))
    conn.execute("DELETE FROM fts WHERE path = ?", (path,))
    conn.execute("DELETE FROM embeddings WHERE path = ?", (path,))
    conn.execute("DELETE FROM nodes WHERE path = ?", (path,))
    conn.commit()


def get_all_nodes(conn: sqlite3.Connection) -> list[dict]:
    """Get all nodes as a list of dicts."""
    rows = conn.execute("SELECT * FROM nodes ORDER BY path").fetchall()
    return [dict(r) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
