"""
mvm-search: tri-mode retrieval over the indexed KB.

Three signals, combinable:
  - text:       SQLite FTS5 (BM25-ranked)            [v0; vector replaces in v0.1]
  - graph:      adjacency from markdown links + frontmatter refs
  - hierarchy:  path-distance from a subtree root or seed doc

Usage:
  mvm-search "<query>"                                  # text-only ranking
  mvm-search "<query>" --in poe2/0.5                    # restrict to subtree
  mvm-search "<query>" --near moonlaif.md               # graph proximity
  mvm-search "<query>" --kind canonical                 # frontmatter filter
  mvm-search "<query>" --in poe2/0.5 --near moonlaif.md --top-k 10
  mvm-search "<query>" --json                           # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
from collections import deque
from pathlib import Path

_EMBED_MODEL = None


def _embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        from fastembed import TextEmbedding
        _EMBED_MODEL = TextEmbedding()
    return _EMBED_MODEL


def _embed_query(text: str) -> bytes:
    emb = next(iter(_embed_model().embed([text])))
    return struct.pack(f"{len(emb)}f", *emb)

DEFAULT_ROOT = Path(os.environ.get("MVM_KNOWLEDGE", str(Path.home() / "mvm" / "knowledge")))
DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))

W_TEXT = float(os.environ.get("MVM_W_TEXT", "0.6"))
W_GRAPH = float(os.environ.get("MVM_W_GRAPH", "0.25"))
W_HIER = float(os.environ.get("MVM_W_HIER", "0.15"))


def hierarchy_distance(path_a: str, path_b: str) -> int:
    """Number of directory hops from path_a to path_b (lower = closer)."""
    pa = Path(path_a).parts
    pb = Path(path_b).parts
    common = 0
    for x, y in zip(pa, pb):
        if x == y:
            common += 1
        else:
            break
    return (len(pa) - common) + (len(pb) - common)


def graph_distances(graph_db: Path, seed: str, max_depth: int = 4) -> dict[str, int]:
    """BFS from seed in the bidirectional link graph. Returns {path: distance}."""
    if not graph_db.exists():
        return {}
    conn = sqlite3.connect(graph_db)
    cur = conn.cursor()
    distances = {seed: 0}
    queue = deque([(seed, 0)])
    while queue:
        node, d = queue.popleft()
        if d >= max_depth:
            continue
        cur.execute("SELECT dst FROM edges WHERE src = ?", (node,))
        for (dst,) in cur.fetchall():
            if dst not in distances:
                distances[dst] = d + 1
                queue.append((dst, d + 1))
        cur.execute("SELECT src FROM edges WHERE dst = ?", (node,))
        for (src,) in cur.fetchall():
            if src not in distances:
                distances[src] = d + 1
                queue.append((src, d + 1))
    conn.close()
    return distances


def vec_search(index_db: Path, query: str, kind: str | None, in_prefix: str | None,
               limit: int = 50) -> list[tuple[str, float]]:
    """Vector cosine search via sqlite-vec. Returns [(path, normalized_score)] desc."""
    if not index_db.exists():
        return []
    conn = sqlite3.connect(index_db)
    conn.enable_load_extension(True)
    try:
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        conn.close()
        return []

    try:
        q_blob = _embed_query(query)
    except Exception:
        conn.close()
        return []

    cur = conn.cursor()
    # vec0 returns rows ordered by distance (ascending) when MATCH'd.
    sql = """
        SELECT v.path, v.distance
        FROM files_vec v
        JOIN files f ON f.path = v.path
        WHERE v.embedding MATCH ? AND k = ?
    """
    params: list = [q_blob, limit]
    if kind:
        sql += " AND f.kind = ?"
        params.append(kind)
    if in_prefix:
        sql += " AND f.path LIKE ?"
        params.append(f"{in_prefix.rstrip('/')}%")

    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    if not rows:
        return []
    # vec0 distance is L2 on normalized vectors; convert to similarity ~ 1 - d/2
    sims = [(p, max(0.0, 1.0 - d / 2.0)) for p, d in rows]
    max_s = max(s for _, s in sims) or 1.0
    return [(p, s / max_s) for p, s in sims]


def fts_search(index_db: Path, query: str, kind: str | None, in_prefix: str | None,
               limit: int = 50) -> list[tuple[str, float]]:
    """FTS5 BM25 fallback. Returns [(path, normalized_score)] descending."""
    if not index_db.exists():
        return []
    conn = sqlite3.connect(index_db)
    cur = conn.cursor()
    fts_query = " ".join(query.split())
    # Strip apostrophes / quotes which break FTS5 syntax
    fts_query = fts_query.replace("'", "").replace('"', "")
    sql = (
        "SELECT files_fts.path, bm25(files_fts) AS score "
        "FROM files_fts JOIN files ON files.path = files_fts.path "
        "WHERE files_fts MATCH ?"
    )
    params: list = [fts_query]
    if kind:
        sql += " AND files.kind = ?"
        params.append(kind)
    if in_prefix:
        sql += " AND files.path LIKE ?"
        params.append(f"{in_prefix.rstrip('/')}%")
    sql += " ORDER BY score LIMIT ?"
    params.append(limit)
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        conn.close()
        if "no such" in str(e).lower() or "syntax" in str(e).lower():
            return []
        raise
    conn.close()
    if not rows:
        return []
    raw = [(p, -s) for p, s in rows]
    max_s = max(s for _, s in raw) or 1.0
    return [(p, s / max_s) for p, s in raw]


def fetch_metadata(index_db: Path, paths: list[str]) -> dict[str, dict]:
    if not paths or not index_db.exists():
        return {}
    conn = sqlite3.connect(index_db)
    cur = conn.cursor()
    placeholders = ",".join("?" * len(paths))
    cur.execute(
        f"SELECT path, kind, source, ingested_at, n_tests FROM files WHERE path IN ({placeholders})",
        paths,
    )
    out = {}
    for path, kind, source, ingested_at, n_tests in cur.fetchall():
        out[path] = {
            "kind": kind, "source": source,
            "ingested_at": ingested_at, "n_tests": n_tests,
        }
    conn.close()
    return out


def main(argv = None) -> int:
    parser = argparse.ArgumentParser(description="Tri-mode retrieval (text + graph + hierarchy).")
    parser.add_argument("query", help="Search query.")
    parser.add_argument("--in", dest="in_prefix", help="Restrict to subtree (path prefix).")
    parser.add_argument("--near", help="Graph proximity seed (rel-path or wikilink).")
    parser.add_argument("--kind", help="Filter by frontmatter kind (e.g. canonical, log).")
    parser.add_argument("--top-k", type=int, default=5, help="Top-K results (default: 5).")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE,
                        help=f"State dir (default: {DEFAULT_STATE}).")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"Knowledge root (default: {DEFAULT_ROOT}).")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    index_db = args.state / "index.db"
    graph_db = args.state / "graph.db"

    # Vector search first (semantic); FTS fallback if vector returns nothing
    text_results = vec_search(index_db, args.query, args.kind, args.in_prefix, limit=50)
    if not text_results:
        text_results = fts_search(index_db, args.query, args.kind, args.in_prefix, limit=50)
    if not text_results:
        if args.json:
            print(json.dumps({"query": args.query, "results": []}))
        else:
            print("(no results)")
        return 0

    graph_dist: dict[str, int] = {}
    if args.near:
        graph_dist = graph_distances(graph_db, args.near)

    seed_for_hier = args.near or (args.in_prefix or "")
    scored: list[tuple[str, float, dict]] = []
    for path, text_score in text_results:
        gd = graph_dist.get(path)
        graph_score = 1.0 / (gd + 1) if gd is not None else 0.0
        if seed_for_hier:
            hd = hierarchy_distance(seed_for_hier, path)
            hier_score = 1.0 / (hd + 1)
        else:
            hier_score = 0.0
        total = (W_TEXT * text_score + W_GRAPH * graph_score + W_HIER * hier_score)
        scored.append((path, total, {
            "text": round(text_score, 4),
            "graph": round(graph_score, 4),
            "hier": round(hier_score, 4),
            "graph_dist": gd,
        }))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = scored[: args.top_k]

    meta = fetch_metadata(index_db, [p for p, _, _ in top])

    if args.json:
        print(json.dumps({
            "query": args.query,
            "filters": {"in": args.in_prefix, "near": args.near, "kind": args.kind},
            "weights": {"text": W_TEXT, "graph": W_GRAPH, "hier": W_HIER},
            "results": [
                {"path": p, "score": round(s, 4), "components": c, "metadata": meta.get(p, {})}
                for p, s, c in top
            ],
        }, indent=2))
    else:
        print(f"query: {args.query}")
        if args.in_prefix or args.near or args.kind:
            filters = []
            if args.in_prefix: filters.append(f"in={args.in_prefix}")
            if args.near: filters.append(f"near={args.near}")
            if args.kind: filters.append(f"kind={args.kind}")
            print(f"filters: {' '.join(filters)}")
        print()
        for path, score, comp in top:
            m = meta.get(path, {})
            kind_str = f" [{m['kind']}]" if m.get("kind") else ""
            tests_str = f" tests={m['n_tests']}" if m.get("n_tests") else ""
            print(f"  {score:.3f}  {path}{kind_str}{tests_str}")
            print(f"          text={comp['text']:.3f} graph={comp['graph']:.3f} hier={comp['hier']:.3f}")
    return 0


