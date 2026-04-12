"""Search — FTS5 grep with query node tracking and search edge creation."""

import hashlib
import re
import string
from datetime import datetime, timezone


def normalize_query(query_text: str) -> str:
    """Normalize query: lowercase, strip punctuation, sort tokens, SHA-256 hash."""
    text = query_text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = sorted(text.split())
    normalized = " ".join(tokens)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _escape_fts5(query: str) -> str:
    """Escape a query string for FTS5 MATCH syntax.

    Wraps each token in double quotes to treat them as literals,
    preventing FTS5 syntax errors from special characters like ? * + - etc.
    """
    tokens = query.split()
    if not tokens:
        return '""'
    escaped = " ".join(f'"{t}"' for t in tokens)
    return escaped


def grep(conn, query: str, limit: int = 20) -> list[dict]:
    """Search via FTS5, create search edges for top-3, return ranked results."""
    now = _now()

    # Escape query for FTS5
    fts_query = _escape_fts5(query)

    # FTS5 search with BM25 ranking
    # Column weights: path=1.0, title=5.0, content=1.0
    rows = conn.execute(
        """SELECT path, title, rank, snippet(fts, 2, '...', '...', '', 30) as snippet
           FROM fts
           WHERE fts MATCH ?
           ORDER BY bm25(fts, 1.0, 5.0, 1.0)
           LIMIT ?""",
        (fts_query, limit),
    ).fetchall()

    if not rows:
        return []

    results = []
    for i, row in enumerate(rows):
        results.append({
            "path": row[0],
            "title": row[1],
            "rank": i + 1,
            "score": -row[2],  # bm25 returns negative scores; negate for positive
            "snippet": row[3] or "",
        })

    # Create/update query node
    query_id = normalize_query(query)
    existing = conn.execute(
        "SELECT use_count FROM queries WHERE id = ?", (query_id,)
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE queries SET last_used = ?, use_count = use_count + 1 WHERE id = ?",
            (now, query_id),
        )
    else:
        conn.execute(
            "INSERT INTO queries (id, query_text, created_at, last_used, use_count) VALUES (?, ?, ?, ?, 1)",
            (query_id, query, now, now),
        )

    # Create search edges for top 3 results (rank-weighted)
    rank_weights = [1.0, 0.66, 0.33]
    for i, result in enumerate(results[:3]):
        weight = rank_weights[i]
        target = result["path"]

        # Upsert search edge
        existing_edge = conn.execute(
            "SELECT strength, access_count FROM edges WHERE source = ? AND target = ? AND type = 'search'",
            (query_id, target),
        ).fetchone()

        if existing_edge:
            new_strength = min(5.0, existing_edge[0] + weight * 0.1)
            conn.execute(
                """UPDATE edges SET strength = ?, last_activated = ?, access_count = access_count + 1
                   WHERE source = ? AND target = ? AND type = 'search'""",
                (new_strength, now, query_id, target),
            )
        else:
            conn.execute(
                """INSERT INTO edges (source, target, type, strength, last_activated, access_count, created_at)
                   VALUES (?, ?, 'search', ?, ?, 1, ?)""",
                (query_id, target, weight, now, now),
            )

        # Update node search tracking
        conn.execute(
            "UPDATE nodes SET last_searched = ?, search_count = search_count + 1 WHERE path = ?",
            (now, target),
        )

    # Add edge_strength to results
    for result in results:
        edge = conn.execute(
            "SELECT strength FROM edges WHERE source = ? AND target = ? AND type = 'search'",
            (query_id, result["path"]),
        ).fetchone()
        result["edge_strength"] = edge[0] if edge else 0.0

    conn.commit()
    return results


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
