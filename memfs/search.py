"""Search — FTS5 grep with query node tracking and search edge creation."""

import hashlib
import os
import re
import string
from datetime import datetime, timezone, date as date_type


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


def grep(conn, query: str, limit: int = 20, use_vectors: bool = False) -> list[dict]:
    """Search via FTS5, create search edges for top-3, return ranked results."""
    now = _now()

    # Extract date from query for temporal boosting (before FTS5 escaping)
    query_date = _extract_date(query)

    # Strip date patterns from query before FTS5 search
    clean_query = query
    for pattern in DATE_PATTERNS:
        clean_query = pattern.sub("", clean_query)
    clean_query = clean_query.strip()
    if not clean_query:
        clean_query = query  # Fallback if query was entirely a date

    # Escape query for FTS5
    fts_query = _escape_fts5(clean_query)

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

    if not rows and not use_vectors:
        return []

    results = []
    for i, row in enumerate(rows):
        score = -row[2]  # bm25 returns negative scores; negate for positive

        # Apply temporal proximity boost if query contains a date
        if query_date:
            doc_date_str = conn.execute(
                "SELECT date_hint, modified_at FROM nodes WHERE path = ?", (row[0],)
            ).fetchone()
            if doc_date_str:
                doc_date = _parse_date(doc_date_str[0]) or _parse_date(doc_date_str[1])
                if doc_date:
                    days_diff = abs((query_date - doc_date).days)
                    temporal_boost = 1.0 / (1.0 + days_diff)
                    score = score * (1 + temporal_boost)

        results.append({
            "path": row[0],
            "title": row[1],
            "rank": i + 1,
            "score": score,
            "snippet": row[3] or "",
        })

    # RRF fusion with vector search if enabled
    if use_vectors:
        try:
            from memfs.embeddings import cosine_search
            vec_results = cosine_search(conn, query, top_k=limit)
            if vec_results:
                # Build RRF scores: 1/(k+rank) for each result set
                K = 60  # Standard RRF constant
                rrf_scores = {}

                # FTS5 results
                for i, r in enumerate(results):
                    rrf_scores[r["path"]] = {"fts_rank": i + 1, "vec_rank": None,
                                              "title": r["title"], "snippet": r["snippet"]}
                    rrf_scores[r["path"]]["score"] = 1.0 / (K + i + 1)

                # Vector results
                for i, (path, sim) in enumerate(vec_results):
                    if path in rrf_scores:
                        rrf_scores[path]["score"] += 1.0 / (K + i + 1)
                        rrf_scores[path]["vec_rank"] = i + 1
                    else:
                        title_row = conn.execute(
                            "SELECT title FROM nodes WHERE path = ?", (path,)
                        ).fetchone()
                        rrf_scores[path] = {
                            "fts_rank": None, "vec_rank": i + 1,
                            "title": title_row[0] if title_row else path,
                            "snippet": "",
                            "score": 1.0 / (K + i + 1),
                        }

                # Rebuild results from RRF scores
                results = []
                for path, info in rrf_scores.items():
                    results.append({
                        "path": path,
                        "title": info["title"],
                        "rank": 0,  # Will be set below
                        "score": info["score"],
                        "snippet": info["snippet"],
                    })
        except ImportError:
            pass  # sentence-transformers not installed — FTS5 only

    # Re-rank by score after temporal boost / RRF fusion
    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:limit]
    for i, r in enumerate(results):
        r["rank"] = i + 1

    # Enrich each result with neighborhood context
    for r in results:
        r.update(_get_neighborhood(conn, r["path"]))

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


def _get_neighborhood(conn, path: str) -> dict:
    """Get the neighborhood context for a file: directory, siblings, index, links."""
    directory = os.path.dirname(path)
    if not directory:
        directory = ""

    # Siblings (other files in the same directory) with title + description
    if directory:
        siblings_rows = conn.execute(
            "SELECT path, title, description FROM nodes WHERE path LIKE ? AND path != ? ORDER BY path",
            (directory + "/%", path),
        ).fetchall()
        # Only direct children, not nested
        siblings = [{"path": r[0], "title": r[1], "description": r[2]}
                     for r in siblings_rows if os.path.dirname(r[0]) == directory]
    else:
        # Root level files
        siblings_rows = conn.execute(
            "SELECT path, title, description FROM nodes WHERE path NOT LIKE '%/%' AND path != ? ORDER BY path",
            (path,),
        ).fetchall()
        siblings = [{"path": r[0], "title": r[1], "description": r[2]} for r in siblings_rows]

    # Directory index.md (if exists)
    index_info = None
    if directory:
        index_path = directory + "/index.md"
        index_row = conn.execute(
            "SELECT path, title FROM nodes WHERE path = ?", (index_path,)
        ).fetchone()
        if index_row:
            index_info = {"path": index_row[0], "title": index_row[1]}

    # Outgoing links (what this file links to)
    links_to = [r[0] for r in conn.execute(
        "SELECT target FROM edges WHERE source = ? AND type = 'link' ORDER BY strength DESC",
        (path,),
    ).fetchall()]

    # Incoming links (what files reference this one)
    linked_from = [r[0] for r in conn.execute(
        "SELECT source FROM edges WHERE target = ? AND type = 'link' ORDER BY strength DESC",
        (path,),
    ).fetchall()]

    result = {
        "directory": directory or ".",
        "siblings": siblings[:10],  # Cap to keep output manageable
        "links_to": links_to,
        "linked_from": linked_from,
    }
    if index_info:
        result["index"] = index_info

    return result


DATE_PATTERNS = [
    re.compile(r"(\d{4}[-/]\d{2}[-/]\d{2})"),          # 2023-04-10 or 2023/04/10
    re.compile(r"(\d{4}/\d{2}/\d{2}\s*\([A-Za-z]+\))"), # 2023/04/10 (Mon)
]


def _extract_date(query: str) -> date_type | None:
    """Extract a date reference from a query string."""
    for pattern in DATE_PATTERNS:
        match = pattern.search(query)
        if match:
            date_str = match.group(1).split("(")[0].strip()
            return _parse_date(date_str)
    return None


def _parse_date(date_str: str | None) -> date_type | None:
    """Parse a date string into a date object."""
    if not date_str:
        return None
    date_str = str(date_str).strip().strip('"').split("(")[0].strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
