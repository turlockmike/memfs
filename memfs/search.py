"""Search — Neo4j full-text grep with query node tracking and search edge creation.

Preserves v1 behavior: query normalization, date extraction, temporal boost,
rank-weighted search edge creation, node search tracking, neighborhood enrichment.

Vector/RRF fusion is dropped for M1 (may be reintroduced later).
"""

import hashlib
import os
import re
import string
from datetime import datetime, timezone, date as date_type, timedelta

from memfs import graph as graph_mod


def normalize_query(query_text: str) -> str:
    """Normalize query: lowercase, strip punctuation, sort tokens, SHA-256 hash."""
    text = query_text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    tokens = sorted(text.split())
    normalized = " ".join(tokens)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _escape_lucene(query: str) -> str:
    """Lightly sanitize a query for Neo4j fulltext (Lucene).

    Lucene special characters: + - && || ! ( ) { } [ ] ^ " ~ * ? : \\ /
    Strategy: strip punctuation, then use tokens as an OR query to mimic
    the FTS5 "any token" semantics.
    """
    # Replace Lucene specials with space; keep letters/digits/_
    sanitized = re.sub(r"[+\-&|!(){}\[\]^\"~*?:\\/]", " ", query)
    tokens = [t for t in sanitized.split() if t]
    if not tokens:
        return ""
    # Each term escaped by wrapping; Lucene defaults to OR when not quoted
    return " ".join(tokens)


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


def _freshness_status(node: dict) -> str:
    """Return 'fresh' | 'stale' | 'never_verified' for a node."""
    verified = node.get("freshness_verified_at")
    if not verified:
        return "never_verified"
    stale_days = node.get("freshness_stale_after_days")
    if not stale_days:
        return "fresh"  # verified but no stale window specified
    try:
        v_dt = datetime.fromisoformat(str(verified).replace("Z", "+00:00"))
        if v_dt.tzinfo is None:
            v_dt = v_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "never_verified"
    age = datetime.now(timezone.utc) - v_dt
    if age > timedelta(days=int(stale_days)):
        return "stale"
    return "fresh"


def grep(graph, query: str, limit: int = 20, layer: int | None = None,
         fresh_only: bool = False) -> list[dict]:
    """Neo4j fulltext search, creates search edges for top-3, returns ranked results.

    Parameters
    ----------
    graph : memfs.graph.Graph
    query : search text
    limit : max results
    layer : if set, restrict to nodes with this layer
    fresh_only : if True, drop results whose freshness == 'stale'
    """
    # Extract date for temporal boosting (before sanitization strips digits)
    query_date = _extract_date(query)

    # Strip date patterns from query before search
    clean_query = query
    for pattern in DATE_PATTERNS:
        clean_query = pattern.sub("", clean_query)
    clean_query = clean_query.strip()
    if not clean_query:
        clean_query = query

    lucene_query = _escape_lucene(clean_query)
    if not lucene_query:
        return []

    # Over-fetch so we can re-rank after temporal boost
    raw = graph_mod.fulltext_search(
        graph, lucene_query, limit=limit * 3, layer=layer,
    )
    if not raw:
        return []

    results = []
    for i, row in enumerate(raw):
        score = float(row.get("score") or 0.0)

        # Temporal boost
        if query_date:
            doc_date = (
                _parse_date(row.get("date_hint"))
                or _parse_date(row.get("modified_at"))
            )
            if doc_date:
                days_diff = abs((query_date - doc_date).days)
                temporal_boost = 1.0 / (1.0 + days_diff)
                score = score * (1 + temporal_boost)

        node_like = {
            "freshness_verified_at": row.get("freshness_verified_at"),
            "freshness_stale_after_days": row.get("freshness_stale_after_days"),
        }
        freshness = _freshness_status(node_like)
        if fresh_only and freshness == "stale":
            continue

        results.append({
            "path": row["path"],
            "title": row.get("title"),
            "rank": i + 1,  # will be rewritten after re-rank
            "score": score,
            "snippet": (row.get("description") or "")[:200],
            "layer": row.get("layer"),
            "freshness": freshness,
        })

    # Re-rank after temporal boost
    results.sort(key=lambda r: r["score"], reverse=True)
    results = results[:limit]
    for i, r in enumerate(results):
        r["rank"] = i + 1

    # Enrich with neighborhood
    for r in results:
        nbh = graph_mod.neighborhood(graph, r["path"])
        r.update(nbh)

    # Create / strengthen query node + search edges for top 3
    query_id = normalize_query(query)
    graph_mod.upsert_query_node(graph, query_id, query)

    rank_weights = [1.0, 0.66, 0.33]
    for i, result in enumerate(results[:3]):
        graph_mod.upsert_search_edge(
            graph, query_id, result["path"], rank=i + 1,
            rank_weight=rank_weights[i],
        )
        graph_mod.update_node_search_tracking(graph, result["path"])

    # Attach edge_strength
    for r in results:
        r["edge_strength"] = graph_mod.get_search_edge_strength(
            graph, query_id, r["path"],
        )

    return results
