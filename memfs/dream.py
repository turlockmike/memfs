"""Dream briefing — produce a list of consolidation candidates.

The LLM-driven "dream" pass is left to the agent (Karpathy via /memfs-dream).
This module answers the smaller question: *what should the agent consider?*

Candidate types (one NDJSON line each):

    {"candidate_type": "orphan", "nodes": [p], "reason": ..., "priority": f}
    {"candidate_type": "merge",  "nodes": [a, b], "reason": ..., ...}
    {"candidate_type": "split",  "nodes": [p], "reason": ..., ...}
    {"candidate_type": "link",   "nodes": [a, b], "reason": ..., ...}
    {"candidate_type": "stale",  "nodes": [p], "reason": ..., ...}
    {"candidate_type": "index",  "nodes": [dir_path], "reason": ..., ...}
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Iterable

from memfs import graph as graph_mod
from memfs.search import _freshness_status


# ----- helpers -----

def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _file_size_and_lines(mem_home: str, rel_path: str) -> tuple[int, int] | None:
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.isfile(abs_path):
        return None
    try:
        size = os.path.getsize(abs_path)
        with open(abs_path, "rb") as f:
            lines = sum(1 for _ in f)
        return size, lines
    except OSError:
        return None


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _normalized_tokens(text: str) -> set[str]:
    """Very cheap tokenizer for near-duplicate heuristic."""
    import re
    tokens = re.findall(r"\w{4,}", text.lower())
    return set(tokens[:500])  # cap for speed


# ----- individual candidate finders -----

def find_orphans(graph, orphan_days: int = 30) -> list[dict]:
    """Nodes with no LINK edges in/out AND search_count == 0 AND older than N days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=orphan_days)).isoformat()
    rows = graph.run(
        """MATCH (n:Node)
           WHERE NOT (n)-[:LINK]-()
             AND NOT ()-[:LINK]->(n)
             AND coalesce(n.search_count, 0) = 0
             AND coalesce(n.modified_at, '') < $cutoff
           RETURN n.path AS path, n.title AS title,
                  n.modified_at AS modified_at
           ORDER BY n.path""",
        cutoff=cutoff,
    )
    out = []
    for r in rows:
        out.append({
            "candidate_type": "orphan",
            "nodes": [r["path"]],
            "reason": f"no LINK edges, never searched, modified < {orphan_days}d ago",
            "priority": 0.3,
            "title": r.get("title"),
            "modified_at": r.get("modified_at"),
        })
    return out


def find_near_duplicates(graph, limit: int = 10) -> list[dict]:
    """Cheap heuristic: compare every pair of nodes whose title-token overlap
    passes a Jaccard threshold OR whose description is identical. Caps at
    `limit` candidates to avoid quadratic blow-up on huge graphs.

    Exclusions:
    - Session transcripts (paths starting with ``sessions/``) are time-series
      records, not semantic notes. They frequently share template-heavy
      prefixes (wake prompts, Stop-hook error templates) that drive title
      + content jaccard to 1.0 despite having different session IDs, dates,
      and actual session content. Merging them would destroy the forward
      ingest flow. Added 2026-04-17 03:00 CDT after dream briefing flagged
      10+ false-positive merges from Apr 11–16 stop-hook sessions.
    """
    nodes = graph.run(
        "MATCH (n:Node) "
        "RETURN n.path AS path, n.title AS title, n.description AS description, "
        "       n.content AS content, n.layer AS layer "
        "ORDER BY n.path"
    )
    if len(nodes) > 2000:
        nodes = nodes[:2000]  # safety cap

    # Filter out session transcripts — they're time-series, not semantic
    # duplicates. See docstring.
    nodes = [n for n in nodes if not (n.get("path") or "").startswith("sessions/")]

    # Pre-tokenize titles (cheap) and content heads
    title_tokens = {n["path"]: _normalized_tokens((n.get("title") or "") + " " + (n.get("description") or "")) for n in nodes}
    content_preview = {n["path"]: (n.get("content") or "")[:600] for n in nodes}

    seen = set()
    out: list[dict] = []
    # Group candidate pairs by shared title token → drastically reduces O(n^2)
    token_to_paths: dict[str, list[str]] = defaultdict(list)
    for path, toks in title_tokens.items():
        for t in toks:
            token_to_paths[t].append(path)

    pair_scores: dict[tuple[str, str], float] = {}
    for tok, paths in token_to_paths.items():
        if len(paths) < 2 or len(paths) > 50:
            continue  # too-common tokens are noise
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                a, b = sorted([paths[i], paths[j]])
                if (a, b) in pair_scores:
                    continue
                j_title = _jaccard(title_tokens[a], title_tokens[b])
                if j_title < 0.5:
                    continue
                # Content length similarity check
                la = len(content_preview.get(a, ""))
                lb = len(content_preview.get(b, ""))
                if max(la, lb) == 0:
                    continue
                len_sim = min(la, lb) / max(la, lb)
                score = 0.6 * j_title + 0.4 * len_sim
                if score >= 0.55:
                    pair_scores[(a, b)] = score

    for (a, b), score in sorted(pair_scores.items(), key=lambda x: -x[1])[:limit]:
        seen.add((a, b))
        out.append({
            "candidate_type": "merge",
            "nodes": [a, b],
            "reason": f"title/content overlap jaccard={score:.2f}",
            "priority": round(min(0.9, score), 2),
        })
    return out


def find_bloated_files(mem_home: str, bloat_lines: int, bloat_bytes: int) -> list[dict]:
    """Files exceeding line-count or byte-size thresholds."""
    out: list[dict] = []
    # Walk filesystem directly (memfs content field may be trimmed)
    for dirpath, dirnames, filenames in os.walk(mem_home):
        # Skip .mem / .git etc
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules",)]
        for fn in filenames:
            if not (fn.endswith(".md") or fn.endswith(".jsonl")):
                continue
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, mem_home)
            info = _file_size_and_lines(mem_home, rel)
            if info is None:
                continue
            size, lines = info
            if lines >= bloat_lines or size >= bloat_bytes:
                reasons = []
                if lines >= bloat_lines:
                    reasons.append(f"{lines} lines ≥ {bloat_lines}")
                if size >= bloat_bytes:
                    reasons.append(f"{size} bytes ≥ {bloat_bytes}")
                out.append({
                    "candidate_type": "split",
                    "nodes": [rel],
                    "reason": "; ".join(reasons),
                    "priority": round(min(0.9, 0.3 + lines / (bloat_lines * 4)), 2),
                    "bytes": size,
                    "lines": lines,
                })
    # Most-bloated first
    out.sort(key=lambda c: -c.get("lines", 0))
    return out


def find_dirs_missing_index(mem_home: str, min_files: int = 10) -> list[dict]:
    out: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(mem_home):
        # skip hidden
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("node_modules",)]
        rel_dir = os.path.relpath(dirpath, mem_home)
        if rel_dir.startswith("."):
            continue
        md_files = [f for f in filenames if f.endswith(".md")]
        if len(md_files) < min_files:
            continue
        if "index.md" in md_files:
            continue
        out.append({
            "candidate_type": "index",
            "nodes": [rel_dir if rel_dir != "." else ""],
            "reason": f"{len(md_files)} .md files, no index.md",
            "priority": round(min(0.8, 0.3 + len(md_files) / 40), 2),
            "file_count": len(md_files),
        })
    out.sort(key=lambda c: -c.get("file_count", 0))
    return out


def find_cosearched_unlinked(graph, min_cooccur: int = 3) -> list[dict]:
    """Pairs of nodes that appear in the same top-3 SEARCH result set for
    N+ distinct queries but have no LINK edge between them."""
    # For each query, get all top-3 search edges; count pair co-occurrences.
    rows = graph.run(
        """MATCH (q:Query)-[r:SEARCH]->(n:Node)
           WHERE r.rank <= 3
           RETURN q.id AS qid, n.path AS path"""
    )
    by_q: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        by_q[r["qid"]].append(r["path"])

    pair_count: dict[tuple[str, str], int] = defaultdict(int)
    for paths in by_q.values():
        unique = sorted(set(paths))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                a, b = unique[i], unique[j]
                pair_count[(a, b)] += 1

    candidates: list[dict] = []
    for (a, b), n in pair_count.items():
        if n < min_cooccur:
            continue
        # Skip if a LINK edge already exists (either direction)
        existing = graph.run_scalar(
            """MATCH (x:Node {path: $a}),(y:Node {path: $b})
               RETURN EXISTS( (x)-[:LINK]-(y) ) AS has""",
            a=a, b=b,
        )
        if existing:
            continue
        candidates.append({
            "candidate_type": "link",
            "nodes": [a, b],
            "reason": f"co-searched in top-3 across {n} queries, no LINK edge",
            "priority": round(min(0.9, 0.4 + n / 10.0), 2),
            "cooccur_count": n,
        })
    candidates.sort(key=lambda c: -c.get("cooccur_count", 0))
    return candidates


def find_stale_facts(graph) -> list[dict]:
    rows = graph.run(
        "MATCH (n:Node) "
        "WHERE n.freshness_verified_at IS NOT NULL "
        "  AND n.freshness_stale_after_days IS NOT NULL "
        "RETURN n.path AS path, n.title AS title, "
        "n.freshness_verified_at AS verified_at, "
        "n.freshness_stale_after_days AS stale_after, "
        "n.freshness_source_url AS source_url"
    )
    out: list[dict] = []
    for row in rows:
        status = _freshness_status({
            "freshness_verified_at": row.get("verified_at"),
            "freshness_stale_after_days": row.get("stale_after"),
        })
        if status != "stale":
            continue
        out.append({
            "candidate_type": "stale",
            "nodes": [row["path"]],
            "reason": f"verified_at={row['verified_at']}, stale_after={row['stale_after']}d",
            "priority": 0.5,
            "source_url": row.get("source_url"),
        })
    return out


# ----- top-level entry point -----

def run_briefing(graph, *, mem_home: str, args) -> list[dict]:
    """Return all candidate types as a flat list of NDJSON-serializable dicts."""
    orphan_days = getattr(args, "orphan_days", 30)
    bloat_lines = getattr(args, "bloat_lines", 500)
    bloat_bytes = getattr(args, "bloat_bytes", 10240)

    candidates: list[dict] = []
    candidates.extend(find_orphans(graph, orphan_days=orphan_days))
    candidates.extend(find_near_duplicates(graph))
    candidates.extend(find_bloated_files(mem_home, bloat_lines=bloat_lines, bloat_bytes=bloat_bytes))
    candidates.extend(find_dirs_missing_index(mem_home))
    candidates.extend(find_cosearched_unlinked(graph))
    candidates.extend(find_stale_facts(graph))

    # Highest priority first
    candidates.sort(key=lambda c: -c.get("priority", 0.0))
    return candidates
