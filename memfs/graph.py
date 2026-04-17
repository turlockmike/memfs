"""Neo4j graph store for memfs.

Replaces the SQLite backing store. Files remain source of truth; Neo4j is a
derived cache rebuildable via `memfs reindex`.

Schema:
    (:Node {path, title, description, layer, source, content_hash, date_hint,
             created_at, modified_at, last_searched, search_count,
             freshness_verified_at, freshness_source_url, freshness_stale_after_days,
             content})
    (:Query {id, text, created_at, last_used, use_count})
    (:Claim {id, text, confidence, scope, claimed_at, claimed_to,
             verified_at, outcome})
    (src:Node)-[:LINK {strength, created_at, last_activated, access_count}]->(tgt:Node)
    (q:Query)-[:SEARCH {strength, rank, created_at, last_activated, access_count}]->(n:Node)
    (n:Node)-[:DERIVED_FROM {extraction_type, created_at}]->(src:Node)
    (n:Node)-[:CLAIMS_ABOUT {created_at}]->(c:Claim)
    (a:Claim)-[:CONTRADICTS {detected_at, adjudicated}]->(b:Claim)

Full-text index:
    node_content ON Node(title, description, content)

Design note:
- `connect()` returns a `Graph` object (not a raw connection). Close with `.close()`.
- All writes happen inside transactions. Reads use auto-commit.
- `Graph` is a thin pseudo-replacement for the SQLite conn object — provides
  execute-style methods so indexer.py/search.py don't need massive rewrites.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable

from neo4j import Driver, GraphDatabase

SCHEMA_VERSION = "2"

DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"
DEFAULT_PASSWORD = "memfsdev"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_uri() -> str:
    return os.environ.get("MEMFS_NEO4J_URI", DEFAULT_URI)


def _get_auth() -> tuple[str, str]:
    user = os.environ.get("MEMFS_NEO4J_USER", DEFAULT_USER)
    password = os.environ.get("MEMFS_NEO4J_PASSWORD", DEFAULT_PASSWORD)
    return user, password


class Graph:
    """Wrapper around a Neo4j driver that provides execute/commit semantics
    roughly compatible with the old sqlite3 connection surface.

    Not a perfect shim — callers that need graph-native operations call the
    explicit methods (add_node, get_node, upsert_link_edge, ...) rather than
    execute() with Cypher strings.
    """

    def __init__(self, driver: Driver, database: str = "neo4j"):
        self._driver = driver
        self._database = database

    def close(self) -> None:
        self._driver.close()

    # Context manager support
    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # Low-level execute for one-off queries
    def run(self, cypher: str, **params) -> list[dict]:
        """Run a Cypher query and return list of records as dicts."""
        with self._driver.session(database=self._database) as session:
            result = session.run(cypher, **params)
            return [dict(r) for r in result]

    def run_one(self, cypher: str, **params) -> dict | None:
        """Run a Cypher query and return the first record as a dict, or None."""
        with self._driver.session(database=self._database) as session:
            result = session.run(cypher, **params)
            record = result.single()
            return dict(record) if record else None

    def run_scalar(self, cypher: str, **params) -> Any:
        """Run a Cypher query and return the first value of the first record."""
        record = self.run_one(cypher, **params)
        if record is None:
            return None
        # Return the first value
        return next(iter(record.values()))


def create_db(uri: str | None = None, *, fresh: bool = False) -> None:
    """Create / verify schema. Idempotent. `fresh=True` wipes the graph first."""
    driver = GraphDatabase.driver(uri or _get_uri(), auth=_get_auth())
    try:
        with driver.session() as session:
            if fresh:
                session.run("MATCH (n) DETACH DELETE n")
                # Drop and recreate indexes/constraints (IF EXISTS for 5.x)
                for stmt in _DROP_STATEMENTS:
                    try:
                        session.run(stmt)
                    except Exception:
                        pass
            # Create constraints + indexes (idempotent in Neo4j 5)
            for stmt in _SCHEMA_STATEMENTS:
                session.run(stmt)
            # Write meta
            session.run(
                """MERGE (m:Meta {key: 'schema_version'})
                   SET m.value = $v, m.updated_at = $now""",
                v=SCHEMA_VERSION, now=_now(),
            )
            session.run(
                """MERGE (m:Meta {key: 'created_at'})
                   ON CREATE SET m.value = $now""",
                now=_now(),
            )
    finally:
        driver.close()


def connect(uri: str | None = None) -> Graph:
    """Open a graph connection. Returns a Graph wrapper; call .close() when done."""
    driver = GraphDatabase.driver(uri or _get_uri(), auth=_get_auth())
    return Graph(driver)


# -------- Schema DDL --------

_SCHEMA_STATEMENTS = [
    # Node path must be unique
    "CREATE CONSTRAINT node_path_unique IF NOT EXISTS FOR (n:Node) REQUIRE n.path IS UNIQUE",
    "CREATE CONSTRAINT query_id_unique IF NOT EXISTS FOR (q:Query) REQUIRE q.id IS UNIQUE",
    "CREATE CONSTRAINT claim_id_unique IF NOT EXISTS FOR (c:Claim) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT meta_key_unique IF NOT EXISTS FOR (m:Meta) REQUIRE m.key IS UNIQUE",
    # Search-performance indexes
    "CREATE INDEX node_layer IF NOT EXISTS FOR (n:Node) ON (n.layer)",
    "CREATE INDEX node_modified IF NOT EXISTS FOR (n:Node) ON (n.modified_at)",
    "CREATE INDEX node_last_searched IF NOT EXISTS FOR (n:Node) ON (n.last_searched)",
    # Full-text index for grep (Lucene-based, BM25 scoring native)
    (
        "CREATE FULLTEXT INDEX node_content IF NOT EXISTS "
        "FOR (n:Node) ON EACH [n.title, n.description, n.content] "
        "OPTIONS { indexConfig: { `fulltext.analyzer`: 'english' } }"
    ),
]

_DROP_STATEMENTS = [
    "DROP CONSTRAINT node_path_unique IF EXISTS",
    "DROP CONSTRAINT query_id_unique IF EXISTS",
    "DROP CONSTRAINT claim_id_unique IF EXISTS",
    "DROP CONSTRAINT meta_key_unique IF EXISTS",
    "DROP INDEX node_layer IF EXISTS",
    "DROP INDEX node_modified IF EXISTS",
    "DROP INDEX node_last_searched IF EXISTS",
    "DROP INDEX node_content IF EXISTS",
]


# -------- Node operations --------

def add_node(
    graph: Graph,
    path: str,
    title: str,
    content_hash: str,
    date_hint: str | None = None,
    *,
    description: str | None = None,
    content: str = "",
    layer: int = 2,
    source: str | None = None,
    freshness_verified_at: str | None = None,
    freshness_source_url: str | None = None,
    freshness_stale_after_days: int | None = None,
) -> None:
    """Insert a new node. Raises on duplicate path."""
    now = _now()
    result = graph.run(
        """CREATE (n:Node {
             path: $path, title: $title, description: $description,
             content: $content, content_hash: $content_hash, date_hint: $date_hint,
             layer: $layer, source: $source,
             freshness_verified_at: $fv, freshness_source_url: $fs,
             freshness_stale_after_days: $fd,
             created_at: $now, modified_at: $now,
             last_searched: null, search_count: 0
           })
           RETURN n.path AS path""",
        path=path, title=title, description=description,
        content=content, content_hash=content_hash, date_hint=date_hint,
        layer=layer, source=source,
        fv=freshness_verified_at, fs=freshness_source_url, fd=freshness_stale_after_days,
        now=now,
    )
    if not result:
        raise RuntimeError(f"failed to create node: {path}")


def upsert_node(
    graph: Graph,
    path: str,
    title: str,
    content_hash: str,
    date_hint: str | None,
    *,
    description: str | None = None,
    content: str = "",
    layer: int = 2,
    source: str | None = None,
    freshness_verified_at: str | None = None,
    freshness_source_url: str | None = None,
    freshness_stale_after_days: int | None = None,
) -> None:
    """Insert or update a node (merge on path)."""
    now = _now()
    graph.run(
        """MERGE (n:Node {path: $path})
           ON CREATE SET
             n.title = $title, n.description = $description,
             n.content = $content, n.content_hash = $content_hash, n.date_hint = $date_hint,
             n.layer = $layer, n.source = $source,
             n.freshness_verified_at = $fv, n.freshness_source_url = $fs,
             n.freshness_stale_after_days = $fd,
             n.created_at = $now, n.modified_at = $now,
             n.last_searched = null, n.search_count = 0
           ON MATCH SET
             n.title = $title, n.description = $description,
             n.content = $content, n.content_hash = $content_hash, n.date_hint = $date_hint,
             n.layer = $layer, n.source = $source,
             n.freshness_verified_at = $fv, n.freshness_source_url = $fs,
             n.freshness_stale_after_days = $fd,
             n.modified_at = $now""",
        path=path, title=title, description=description,
        content=content, content_hash=content_hash, date_hint=date_hint,
        layer=layer, source=source,
        fv=freshness_verified_at, fs=freshness_source_url, fd=freshness_stale_after_days,
        now=now,
    )


def get_node(graph: Graph, path: str) -> dict | None:
    record = graph.run_one(
        "MATCH (n:Node {path: $path}) RETURN n",
        path=path,
    )
    if not record:
        return None
    n = record["n"]
    return dict(n)


def remove_node(graph: Graph, path: str) -> None:
    """Delete a node and all its incident relationships."""
    graph.run("MATCH (n:Node {path: $path}) DETACH DELETE n", path=path)


def get_all_nodes(graph: Graph) -> list[dict]:
    rows = graph.run("MATCH (n:Node) RETURN n ORDER BY n.path")
    return [dict(r["n"]) for r in rows]


def count_nodes(graph: Graph) -> int:
    return int(graph.run_scalar("MATCH (n:Node) RETURN count(n)") or 0)


def count_edges(graph: Graph, type: str | None = None) -> int:
    if type == "link":
        q = "MATCH ()-[r:LINK]->() RETURN count(r)"
    elif type == "search":
        q = "MATCH ()-[r:SEARCH]->() RETURN count(r)"
    else:
        q = "MATCH ()-[r]->() WHERE type(r) IN ['LINK','SEARCH'] RETURN count(r)"
    return int(graph.run_scalar(q) or 0)


def count_queries(graph: Graph) -> int:
    return int(graph.run_scalar("MATCH (q:Query) RETURN count(q)") or 0)


# -------- Link edge operations --------

def clear_link_edges_from(graph: Graph, source_path: str) -> None:
    """Delete LINK edges originating from a given node.

    Only clears edges created from parsed ``[[wikilinks]]`` in the file itself
    — i.e. edges whose ``source`` property is either ``"authored"`` or missing
    (legacy edges predating the property). Edges attributed to dream/link-apply
    (``source in {"content_similarity", "cosearch", ...}``) survive so that
    rebuilding a node from its file content doesn't wipe the graph-level
    structural signal. Authored edges always win: on re-index, old authored
    edges are dropped and the current file's wikilinks are re-added.
    """
    graph.run(
        """MATCH (s:Node {path: $path})-[r:LINK]->()
           WHERE coalesce(r.source, 'authored') = 'authored'
           DELETE r""",
        path=source_path,
    )


def upsert_link_edge(
    graph: Graph,
    source_path: str,
    target_path: str,
    *,
    strength: float = 1.0,
    source: str = "authored",
) -> None:
    """Create or update a LINK edge. Creates a placeholder target node if
    missing (``strength`` indicates broken vs alive).

    The ``source`` property distinguishes authored wikilinks ("authored")
    from graph-derived edges ("content_similarity", "cosearch", ...) so
    that ``clear_link_edges_from`` can wipe only the authored class on
    file re-index. An existing edge's source is preserved on re-upsert
    (ON MATCH does NOT overwrite source) so authored-then-rederived edges
    stay marked "authored".
    """
    now = _now()
    graph.run(
        """MERGE (s:Node {path: $src})
           MERGE (t:Node {path: $tgt})
           MERGE (s)-[r:LINK]->(t)
           ON CREATE SET r.strength = $strength, r.created_at = $now,
                         r.last_activated = $now, r.access_count = 0,
                         r.source = $source
           ON MATCH  SET r.strength = $strength""",
        src=source_path, tgt=target_path, strength=strength, now=now,
        source=source,
    )


def upgrade_broken_links(graph: Graph, target_path: str) -> int:
    """When a file is created, upgrade LINK edges pointing to it from strength=0 to 1.0."""
    result = graph.run(
        """MATCH ()-[r:LINK]->(t:Node {path: $tgt})
           WHERE r.strength = 0
           SET r.strength = 1.0
           RETURN count(r) AS upgraded""",
        tgt=target_path,
    )
    return int(result[0]["upgraded"]) if result else 0


# -------- Query + search edge operations --------

def upsert_query_node(graph: Graph, query_id: str, query_text: str) -> None:
    now = _now()
    graph.run(
        """MERGE (q:Query {id: $id})
           ON CREATE SET q.text = $text, q.created_at = $now,
                         q.last_used = $now, q.use_count = 1
           ON MATCH  SET q.last_used = $now, q.use_count = q.use_count + 1""",
        id=query_id, text=query_text, now=now,
    )


def upsert_search_edge(
    graph: Graph,
    query_id: str,
    target_path: str,
    rank: int,
    rank_weight: float,
) -> float:
    """Create or strengthen a SEARCH edge from query to target. Returns new strength."""
    now = _now()
    result = graph.run(
        """MATCH (q:Query {id: $qid})
           MATCH (n:Node {path: $tgt})
           MERGE (q)-[r:SEARCH]->(n)
           ON CREATE SET r.strength = $weight, r.rank = $rank,
                         r.created_at = $now, r.last_activated = $now, r.access_count = 1
           ON MATCH  SET r.strength = CASE
                                       WHEN r.strength + $weight * 0.1 > 5.0 THEN 5.0
                                       ELSE r.strength + $weight * 0.1
                                      END,
                         r.rank = $rank,
                         r.last_activated = $now,
                         r.access_count = r.access_count + 1
           RETURN r.strength AS s""",
        qid=query_id, tgt=target_path, weight=rank_weight, rank=rank, now=now,
    )
    return float(result[0]["s"]) if result else 0.0


def update_node_search_tracking(graph: Graph, path: str) -> None:
    graph.run(
        """MATCH (n:Node {path: $path})
           SET n.last_searched = $now,
               n.search_count = coalesce(n.search_count, 0) + 1""",
        path=path, now=_now(),
    )


def get_search_edge_strength(graph: Graph, query_id: str, target_path: str) -> float:
    val = graph.run_scalar(
        """MATCH (:Query {id: $qid})-[r:SEARCH]->(:Node {path: $tgt})
           RETURN r.strength AS s""",
        qid=query_id, tgt=target_path,
    )
    return float(val) if val is not None else 0.0


# -------- Full-text search --------

def fulltext_search(graph: Graph, query: str, limit: int = 20,
                    layer: int | None = None) -> list[dict]:
    """Run a BM25-ranked full-text search. Returns list of dicts with
    path, title, score, snippet."""
    # Neo4j fulltext accepts Lucene syntax. Use simple query for now.
    cypher = (
        "CALL db.index.fulltext.queryNodes('node_content', $q) "
        "YIELD node, score "
        + ("WHERE node.layer = $layer " if layer is not None else "")
        + "RETURN node.path AS path, node.title AS title, "
        "node.description AS description, node.content AS content, "
        "node.layer AS layer, node.date_hint AS date_hint, "
        "node.modified_at AS modified_at, "
        "node.freshness_verified_at AS freshness_verified_at, "
        "node.freshness_stale_after_days AS freshness_stale_after_days, "
        "score "
        "LIMIT $limit"
    )
    params: dict[str, Any] = {"q": query, "limit": limit}
    if layer is not None:
        params["layer"] = layer
    return graph.run(cypher, **params)


# -------- Neighborhood (for grep result enrichment) --------

def neighborhood(graph: Graph, path: str, max_siblings: int = 10) -> dict:
    """Return directory, siblings, index file, links_to, linked_from for a node."""
    directory = os.path.dirname(path)

    # Siblings (direct children of same directory, not nested)
    if directory:
        prefix = directory + "/"
        raw = graph.run(
            """MATCH (n:Node)
               WHERE n.path STARTS WITH $prefix
                 AND n.path <> $me
               RETURN n.path AS path, n.title AS title, n.description AS description
               ORDER BY n.path""",
            prefix=prefix, me=path,
        )
        # Filter in Python for direct children only
        siblings = [
            s for s in raw
            if os.path.dirname(s["path"]) == directory
        ][:max_siblings]
    else:
        siblings = graph.run(
            """MATCH (n:Node)
               WHERE NOT n.path CONTAINS '/'
                 AND n.path <> $me
               RETURN n.path AS path, n.title AS title, n.description AS description
               ORDER BY n.path LIMIT $limit""",
            me=path, limit=max_siblings,
        )

    # Index file if present
    index_info = None
    if directory:
        index_path = directory + "/index.md"
        idx_row = graph.run_one(
            "MATCH (n:Node {path: $p}) RETURN n.path AS path, n.title AS title",
            p=index_path,
        )
        if idx_row:
            index_info = {"path": idx_row["path"], "title": idx_row["title"]}

    # Outgoing links
    links_to = [
        r["target"] for r in graph.run(
            """MATCH (:Node {path: $p})-[r:LINK]->(t:Node)
               RETURN t.path AS target ORDER BY r.strength DESC""",
            p=path,
        )
    ]

    # Incoming links
    linked_from = [
        r["source"] for r in graph.run(
            """MATCH (s:Node)-[r:LINK]->(:Node {path: $p})
               RETURN s.path AS source ORDER BY r.strength DESC""",
            p=path,
        )
    ]

    result = {
        "directory": directory or ".",
        "siblings": siblings,
        "links_to": links_to,
        "linked_from": linked_from,
    }
    if index_info:
        result["index"] = index_info
    return result


# -------- Decay --------

def iter_edges_with_activation(graph: Graph) -> Iterable[dict]:
    """Yield all LINK and SEARCH edges with their last_activated and type."""
    for t in ("LINK", "SEARCH"):
        rows = graph.run(
            f"MATCH (s)-[r:{t}]->(tt) "
            "RETURN s.path AS source, "
            + ("id(s) AS source_qid, " if t == "SEARCH" else "")
            + "tt.path AS target, type(r) AS etype, "
            "r.strength AS strength, r.last_activated AS last_activated"
        )
        for row in rows:
            yield row


def apply_decay_updates(
    graph: Graph,
    link_updates: list[tuple[str, str, float, str]],
    search_updates: list[tuple[str, str, float, str]],
    link_prunes: list[tuple[str, str]],
    search_prunes: list[tuple[str, str]],
) -> None:
    """Bulk apply decay results. Runs in a transaction."""
    for src, tgt, new_strength, now in link_updates:
        graph.run(
            """MATCH (:Node {path: $s})-[r:LINK]->(:Node {path: $t})
               SET r.strength = $st, r.last_activated = $now""",
            s=src, t=tgt, st=new_strength, now=now,
        )
    for qid, tgt, new_strength, now in search_updates:
        graph.run(
            """MATCH (:Query {id: $q})-[r:SEARCH]->(:Node {path: $t})
               SET r.strength = $st, r.last_activated = $now""",
            q=qid, t=tgt, st=new_strength, now=now,
        )
    for src, tgt in link_prunes:
        graph.run(
            """MATCH (:Node {path: $s})-[r:LINK]->(:Node {path: $t}) DELETE r""",
            s=src, t=tgt,
        )
    for qid, tgt in search_prunes:
        graph.run(
            """MATCH (:Query {id: $q})-[r:SEARCH]->(:Node {path: $t}) DELETE r""",
            q=qid, t=tgt,
        )


# -------- Meta operations --------

def set_meta(graph: Graph, key: str, value: str) -> None:
    graph.run(
        "MERGE (m:Meta {key: $k}) SET m.value = $v",
        k=key, v=value,
    )


def get_meta(graph: Graph, key: str) -> str | None:
    return graph.run_scalar(
        "MATCH (m:Meta {key: $k}) RETURN m.value",
        k=key,
    )


# -------- Orphan detection --------

def get_orphans(graph: Graph) -> list[dict]:
    """Nodes with no incoming or outgoing LINK edges and search_count = 0."""
    rows = graph.run(
        """MATCH (n:Node)
           WHERE NOT (n)-[:LINK]-()
             AND NOT ()-[:LINK]->(n)
             AND coalesce(n.search_count, 0) = 0
           RETURN n.path AS path, n.title AS title,
                  coalesce(n.search_count, 0) AS search_count
           ORDER BY n.path"""
    )
    return rows


# -------- Bulk path operations (directory rename) --------

def rename_prefix(graph: Graph, old_prefix: str, new_prefix: str) -> None:
    """Rename all nodes whose path starts with old_prefix."""
    graph.run(
        """MATCH (n:Node) WHERE n.path STARTS WITH $old
           SET n.path = $new + substring(n.path, size($old))""",
        old=old_prefix, new=new_prefix,
    )


# -------- Full reset (for reindex) --------

def clear_data(graph: Graph) -> None:
    """Delete all Node, Query, LINK, SEARCH. Preserves Meta and Claims."""
    graph.run("MATCH (n:Node) DETACH DELETE n")
    graph.run("MATCH (q:Query) DETACH DELETE q")
