"""Access-frequency logging for memfs retrieval.

Every grep call writes a single (:Access) node recording the query that ran,
the timestamp, and the set of results returned. An edge
(:Access)-[:RETRIEVED {rank}]->(:Node) points to each returned path so the
query <-> retrieved-nodes relationship is queryable over time.

Why a separate node type instead of just counters on the Node?

  * `search_count` / `last_searched` on :Node are aggregate running totals.
    They can tell you "ever hit" vs "never hit" but not "hit 10 times in the
    last week" vs "hit once a year ago."
  * Empty-hit queries are invisible to Node counters (no node to attribute
    to). Missing retrievals ARE the most interesting signal — they surface
    gaps in memory. Access nodes capture them explicitly.
  * Time-windowed access-frequency reports (hot-7d, cold-30d) require
    timestamps per access event, not a running counter.

This module is the S3 (memory quality control) instrument from
`viable-memory-architecture.md`:

    "per-file access-frequency tracking, bloat detection (size,
    duplication, unreachable content), retrieval failure logging"

Status kinds:
    - "hit"       : grep returned >=1 result
    - "empty_hit" : grep returned 0 results (gap signal)
    - "error"     : grep raised (defensive; currently unused)
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from memfs.graph import Graph


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def access_logging_enabled() -> bool:
    """Access logging is on by default. Tests and hot loops may disable via
    MEMFS_DISABLE_ACCESS_LOG=1."""
    return os.environ.get("MEMFS_DISABLE_ACCESS_LOG", "0") != "1"


def log_access(
    graph: Graph,
    query_text: str,
    query_id: str,
    results: Iterable[dict],
    status: str = "hit",
) -> str:
    """Record a single grep invocation as an (:Access) node with
    -[:RETRIEVED {rank}]-> edges to each returned path.

    Returns the access_id. Swallowing its own write errors — this is an
    instrument, not a source of truth, and a failed access log must not
    break the user-visible grep.
    """
    if not access_logging_enabled():
        return ""

    access_id = uuid.uuid4().hex
    results_list = list(results)
    paths = [r.get("path") for r in results_list if r.get("path")]
    ranks = [r.get("rank", i + 1) for i, r in enumerate(results_list)]

    actual_status = status
    if actual_status == "hit" and len(paths) == 0:
        actual_status = "empty_hit"

    try:
        graph.run(
            """CREATE (a:Access {
                 id: $id, ts: $ts, query_text: $qtext, query_id: $qid,
                 result_count: $rc, status: $status
               })""",
            id=access_id, ts=_now(), qtext=query_text, qid=query_id,
            rc=len(paths), status=actual_status,
        )
        if paths:
            graph.run(
                """UNWIND $pairs AS pair
                   MATCH (a:Access {id: $aid})
                   MATCH (n:Node {path: pair.path})
                   MERGE (a)-[r:RETRIEVED]->(n)
                   ON CREATE SET r.rank = pair.rank""",
                aid=access_id,
                pairs=[{"path": p, "rank": int(r)}
                       for p, r in zip(paths, ranks)],
            )
    except Exception:
        # Instrument failure — never propagate.
        return ""

    return access_id


# -------- reporting --------

def _window_cutoff(window_days: int | None) -> str | None:
    if window_days is None or window_days <= 0:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(window_days))
    return cutoff.isoformat()


def hot_nodes(
    graph: Graph,
    window_days: int | None = 7,
    limit: int = 20,
) -> list[dict]:
    """Nodes retrieved most often in the window. Counts RETRIEVED edges from
    Access nodes whose ts is within the window. Top-N by hit-count."""
    cutoff = _window_cutoff(window_days)
    where = "WHERE a.ts >= $cutoff" if cutoff else ""
    cypher = f"""
        MATCH (a:Access)-[:RETRIEVED]->(n:Node)
        {where}
        WITH n, count(*) AS hits, max(a.ts) AS last_hit
        RETURN n.path AS path, n.title AS title, n.layer AS layer,
               hits, last_hit
        ORDER BY hits DESC, last_hit DESC
        LIMIT $limit
    """
    params = {"limit": int(limit)}
    if cutoff:
        params["cutoff"] = cutoff
    return graph.run(cypher, **params)


def cold_nodes(
    graph: Graph,
    window_days: int | None = 30,
    limit: int = 20,
) -> list[dict]:
    """Nodes with NO access in the window. Cold means either never-retrieved
    or not-retrieved-recently. Sorted oldest-first (most stale on top)."""
    cutoff = _window_cutoff(window_days)

    if cutoff is None:
        # Treat as "never retrieved ever"
        cypher = """
            MATCH (n:Node)
            WHERE NOT (n)<-[:RETRIEVED]-(:Access)
            RETURN n.path AS path, n.title AS title, n.layer AS layer,
                   n.modified_at AS modified_at
            ORDER BY coalesce(n.modified_at, '') ASC
            LIMIT $limit
        """
        return graph.run(cypher, limit=int(limit))

    # Nodes whose last Access.ts is older than cutoff (or never accessed).
    cypher = """
        MATCH (n:Node)
        OPTIONAL MATCH (n)<-[:RETRIEVED]-(a:Access)
        WITH n, max(a.ts) AS last_hit
        WHERE last_hit IS NULL OR last_hit < $cutoff
        RETURN n.path AS path, n.title AS title, n.layer AS layer,
               n.modified_at AS modified_at, last_hit
        ORDER BY coalesce(last_hit, '') ASC, n.path ASC
        LIMIT $limit
    """
    return graph.run(cypher, cutoff=cutoff, limit=int(limit))


def empty_hit_queries(
    graph: Graph,
    window_days: int | None = 7,
    limit: int = 50,
) -> list[dict]:
    """Queries that returned zero results (status='empty_hit') in the window.
    These are the retrieval failures — memory didn't have what was asked for.
    Most recent first."""
    cutoff = _window_cutoff(window_days)
    where_clauses = ["a.status = 'empty_hit'"]
    if cutoff:
        where_clauses.append("a.ts >= $cutoff")
    where_sql = "WHERE " + " AND ".join(where_clauses)
    cypher = f"""
        MATCH (a:Access)
        {where_sql}
        RETURN a.id AS id, a.ts AS ts, a.query_text AS query_text,
               a.query_id AS query_id
        ORDER BY a.ts DESC
        LIMIT $limit
    """
    params = {"limit": int(limit)}
    if cutoff:
        params["cutoff"] = cutoff
    return graph.run(cypher, **params)


def access_summary(
    graph: Graph,
    window_days: int | None = 7,
) -> dict:
    """One-shot status: total accesses, hit vs empty, distinct queries,
    distinct nodes touched. For `memfs access-report` default output."""
    cutoff = _window_cutoff(window_days)
    where = "WHERE a.ts >= $cutoff" if cutoff else ""
    cypher = f"""
        MATCH (a:Access)
        {where}
        WITH count(a) AS total,
             sum(CASE WHEN a.status = 'hit' THEN 1 ELSE 0 END) AS hits,
             sum(CASE WHEN a.status = 'empty_hit' THEN 1 ELSE 0 END) AS empties,
             count(DISTINCT a.query_id) AS distinct_queries
        RETURN total, hits, empties, distinct_queries
    """
    params = {"cutoff": cutoff} if cutoff else {}
    row = graph.run_one(cypher, **params) or {}
    # distinct nodes touched
    nodes_touched_cypher = f"""
        MATCH (a:Access)-[:RETRIEVED]->(n:Node)
        {where}
        RETURN count(DISTINCT n) AS nodes_touched
    """
    touched = graph.run_scalar(nodes_touched_cypher, **params) or 0

    return {
        "window_days": window_days,
        "total_accesses": int(row.get("total") or 0),
        "hits": int(row.get("hits") or 0),
        "empty_hits": int(row.get("empties") or 0),
        "distinct_queries": int(row.get("distinct_queries") or 0),
        "distinct_nodes_retrieved": int(touched),
    }
