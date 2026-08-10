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


# Trees that are byte-identical mirrors of a primary tree. `mvm-mirror` (cron
# */5) keeps ~/resources <-> ~/mvm/knowledge/resources in sync, so the SAME
# logical note is indexed under two Node paths. Left unhandled, one logical
# pair (X,Y) emits up to FOUR link candidates -- (X,Y), (mX,Y), (X,mY),
# (mX,mY) -- all with an identical jaccard score.
#
# Measured 2026-08-08 (auditor #188 REC 1): the 2026-08-08T00:30 autolink run
# applied 49 raw edges covering only 14 distinct logical pairs -- a 3.50x
# inflation of the connectivity number the consolidation arm grades itself on,
# while 11,010 orphans went untouched. 43 of the 49 scored exactly 0.545,
# the signature of one score smeared across mirror spellings.
_MIRROR_PREFIXES = ("mvm/knowledge/",)


def _canonical_path(path: str) -> str:
    """Collapse a mirror spelling to its primary-tree spelling.

    Pure string operation on the indexed relative path -- no filesystem
    access -- so it is safe to call inside the pair loop.
    """
    for pfx in _MIRROR_PREFIXES:
        if path.startswith(pfx):
            return path[len(pfx):]
    return path


def _mirror_rank(pair: tuple[str, str]) -> tuple[int, str, str]:
    """Deterministic preference key among spellings of one logical pair.

    Prefers the spelling with the FEWEST mirror-prefixed paths (i.e. the
    primary tree), tie-broken lexicographically. Determinism matters across
    runs: if run N applies the edge under spelling A and run N+1 emits
    spelling B, the already-linked check misses and the pair is re-applied
    forever. Same key every run => converges after one application.
    """
    mirrored = sum(1 for p in pair if p != _canonical_path(p))
    return (mirrored, pair[0], pair[1])


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


_TIME_SERIES_PATH_PREFIXES = (
    # All of these are template-driven, dated, append-only series. Their
    # title+content tokens overlap by construction (shared frontmatter,
    # shared section headers like "## Session N (sid)"), driving jaccard
    # to 1.00 on pairs whose actual content is *different events*. Merging
    # them destroys forward history.
    "sessions/",                          # Added 2026-04-17 (stop-hook sessions)
    "journals/",                          # Added 2026-05-06 (date-stamped journal entries)
    "areas/orient-predictions/",          # Added 2026-05-06 (per-session predictions)
    "areas/consolidate-log/",             # Added 2026-05-06 (daily consolidate logs)
    "areas/proactive-findings/",          # Added 2026-05-06 (date-stamped finding reports)
    "evals/",                             # Added 2026-05-06 (eval cycle templates: cycle-N-clone-answer, cycle-N-organize-probe, etc.)
    "projects/evals/",                    # Added 2026-05-06 (cycle-NN-prediction templates)
    "job-runs/",                          # Added 2026-05-06 (per-day cron logs)
    "resources/poe2/canonical/db/",       # Added 2026-05-06 (scraped item DB — tiered variants like biting-frost-i/ii share scaffolding but represent distinct items)
)


def _same_inode(a_abs: str, b_abs: str) -> bool:
    """True if a and b are the same physical file (symlink or hardlink).
    Defensive: returns False on any stat failure."""
    try:
        sa = os.stat(a_abs)
        sb = os.stat(b_abs)
    except OSError:
        return False
    return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino


def _all_known_roots(primary: str) -> list[str]:
    """Return the list of mem_home paths to check inode equality against.
    Includes the primary root and any roots configured in roots.json — needed
    because a candidate pair may span roots (e.g. an ``alfred-state`` symlink
    that targets a file in the ``alfred-home`` root).

    Reads the roots.json config DIRECTLY rather than via load_roots() — when
    MEM_HOME is set in the env (as the memfs shim does for dream-briefing),
    load_roots() returns only the pinned root, but the briefing's graph
    spans every configured root. Falls back gracefully on any failure.
    """
    paths = [primary]
    try:
        import json
        from memfs.roots import CONFIG_PATH
        if CONFIG_PATH.is_file():
            data = json.loads(CONFIG_PATH.read_text())
            for entry in data.get("roots") or []:
                p = entry.get("path")
                if not p:
                    continue
                p_abs = os.path.abspath(os.path.expanduser(p))
                if p_abs not in paths:
                    paths.append(p_abs)
    except Exception:
        pass
    return paths


def _resolve_in_any_root(rel_path: str, roots: list[str]) -> str | None:
    """Try each root in turn until a real file is found at root/rel_path.
    Returns the absolute path of the first match, or None."""
    for root in roots:
        candidate = os.path.join(root, rel_path)
        if os.path.exists(candidate):
            return candidate
    return None


def find_near_duplicates(graph, limit: int = 10, mem_home: str | None = None) -> list[dict]:
    """Cheap heuristic: compare every pair of nodes whose title-token overlap
    passes a Jaccard threshold OR whose description is identical. Caps at
    `limit` candidates to avoid quadratic blow-up on huge graphs.

    Exclusions:
    - Time-series template-driven directories (see ``_TIME_SERIES_PATH_PREFIXES``)
      are excluded because their content shares fixed scaffolding (frontmatter,
      session-id headers, prediction templates) that drives jaccard to 1.00
      despite the actual events being different. Originally added for
      ``sessions/`` on 2026-04-17 after stop-hook templates triggered 10+
      false-positive merges; extended on 2026-05-06 (this dream cycle) after
      ``areas/orient-predictions/``, ``areas/consolidate-log/``,
      ``evals/active-inference-predictions/``, ``projects/evals/``,
      ``journals/``, and ``job-runs/`` all surfaced as Jaccard=1.00 false
      positives in a single dream-briefing run.
    """
    nodes = graph.run(
        "MATCH (n:Node) "
        "RETURN n.path AS path, n.title AS title, n.description AS description, "
        "       n.content AS content, n.layer AS layer "
        "ORDER BY n.path"
    )
    if len(nodes) > 2000:
        nodes = nodes[:2000]  # safety cap

    # Filter out time-series template-driven directories — they're not
    # semantic duplicates even when jaccard==1.00. See docstring.
    nodes = [
        n for n in nodes
        if not (n.get("path") or "").startswith(_TIME_SERIES_PATH_PREFIXES)
    ]
    # Filter out auto-rendered index.md files. memfs/index hooks regenerate
    # these from directory contents on every reindex; they share boilerplate
    # ("# <Dir Name>", auto-rendered footer) which drives jaccard high
    # whenever two unrelated directories happen to have similar names. Cross-
    # directory index.md merges destroy the very routing structure they
    # exist to provide. Added 2026-05-06.
    nodes = [
        n for n in nodes
        if not (n.get("path") or "").endswith("/index.md")
        and (n.get("path") or "") != "index.md"
    ]

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

    roots = _all_known_roots(mem_home) if mem_home else []
    for (a, b), score in sorted(pair_scores.items(), key=lambda x: -x[1]):
        if len(out) >= limit:
            break
        # Skip pairs that are the same physical file (symlink/hardlink). The
        # canonical case: areas/USER.md is a symlink to areas/mike-state.md
        # (alfred-state ↔ alfred-home dual-rooted memfs), so they index as two
        # nodes pointing at the same inode. Merging would `rm` the file both
        # paths share. Resolves each rel_path against ALL configured roots
        # because the pair may span scopes. Added 2026-05-06.
        if roots:
            a_abs = _resolve_in_any_root(a, roots)
            b_abs = _resolve_in_any_root(b, roots)
            if a_abs and b_abs and _same_inode(a_abs, b_abs):
                continue
        seen.add((a, b))
        out.append({
            "candidate_type": "merge",
            "nodes": [a, b],
            "reason": f"title/content overlap jaccard={score:.2f}",
            "priority": round(min(0.9, score), 2),
        })
    return out


def find_bloated_files(mem_home: str, bloat_lines: int, bloat_bytes: int) -> list[dict]:
    """Files exceeding line-count or byte-size thresholds.

    Exclusions:
    - ``job-runs/**/*.jsonl`` — per-day cron run logs. Append-only; "splitting
      by thematic section" is meaningless for append-only event streams. They
      will always grow past the byte threshold within a day. Filtered here
      so they don't dominate the dream briefing every night. Added 2026-05-06.
    """
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
            # Skip per-day cron event logs — append-only by design.
            if rel.startswith("job-runs/") and rel.endswith(".jsonl"):
                continue
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


def find_content_similar_unlinked(
    graph,
    *,
    limit: int = 50,
    min_score: float = 0.12,
    max_score: float = 0.55,
) -> list[dict]:
    """Pairs of nodes with overlapping content tokens but no LINK edge.

    Complements ``find_cosearched_unlinked`` for corpora that don't yet have
    enough query traffic to surface SEARCH-based link candidates. Uses the
    same token-overlap machinery as ``find_near_duplicates`` but with a lower
    threshold (``min_score``); pairs at or above ``max_score`` are rejected
    because those are handled as ``merge`` candidates elsewhere, not links.

    Bootstraps the juxtaposition surface when authored ``[[wikilinks]]`` are
    sparse — which is the empirical starting condition (observed 2026-04-17:
    197 indexed nodes, 0 real LINK edges).

    Exclusions:
    - ``sessions/`` paths (time-series transcripts, not semantic notes).
    - Near-duplicates at or above ``max_score`` (those go to merge).
    - Pairs that already have a LINK edge in either direction.
    """
    rows = graph.run(
        "MATCH (n:Node) "
        "RETURN n.path AS path, n.title AS title, n.description AS description, "
        "       n.content AS content, n.layer AS layer"
    )
    nodes = [r for r in rows if not (r.get("path") or "").startswith("sessions/")]
    if len(nodes) < 2:
        return []

    # Tokenize combined title + description + content head
    tokens_by_path: dict[str, set[str]] = {}
    for n in nodes:
        combined = " ".join([
            n.get("title") or "",
            n.get("description") or "",
            (n.get("content") or "")[:1500],
        ])
        tokens_by_path[n["path"]] = _normalized_tokens(combined)

    # Document-frequency filter: keep rare-ish tokens (discriminative).
    # Tokens appearing in everything (e.g. "mike", "karpathy") are noise;
    # tokens appearing in only one node can't produce pairs.
    token_df: dict[str, int] = defaultdict(int)
    for toks in tokens_by_path.values():
        for t in toks:
            token_df[t] += 1
    max_df = max(3, len(nodes) // 3)  # appears in at most 1/3 of corpus
    rare_tokens = {t for t, df in token_df.items() if 2 <= df <= max_df}

    # Inverted index on rare tokens only
    token_to_paths: dict[str, list[str]] = defaultdict(list)
    for path, toks in tokens_by_path.items():
        for t in toks:
            if t in rare_tokens:
                token_to_paths[t].append(path)

    pair_scores: dict[tuple[str, str], float] = {}
    for tok, paths in token_to_paths.items():
        if len(paths) < 2 or len(paths) > 50:
            continue  # too-common or degenerate token buckets
        for i in range(len(paths)):
            for j in range(i + 1, len(paths)):
                a, b = sorted([paths[i], paths[j]])
                if (a, b) in pair_scores:
                    continue
                score = _jaccard(tokens_by_path[a], tokens_by_path[b])
                if score < min_score or score >= max_score:
                    continue
                pair_scores[(a, b)] = score

    # --- collapse mirror twins BEFORE the limit is spent -------------------
    # Order matters and is the whole point: deduping AFTER the cap would make
    # the ratio honest by *shrinking* coverage (50 raw -> ~14 real). Deduping
    # BEFORE the cap spends all `limit` slots on distinct logical pairs.
    spellings_by_canon: dict[str, list[str]] = defaultdict(list)
    for p in tokens_by_path:
        spellings_by_canon[_canonical_path(p)].append(p)

    logical: dict[tuple[str, str], tuple[tuple[str, str], float]] = {}
    for (a, b), score in pair_scores.items():
        ca, cb = _canonical_path(a), _canonical_path(b)
        if ca == cb:
            continue  # a node paired with its own mirror is not a link
        key = (ca, cb) if ca < cb else (cb, ca)
        prev = logical.get(key)
        # Keep the highest score; on ties keep the preferred (primary-tree)
        # spelling so the choice is stable across runs.
        if prev is None or score > prev[1] or (
            score == prev[1] and _mirror_rank((a, b)) < _mirror_rank(prev[0])
        ):
            logical[key] = ((a, b), score)

    # --- orphan-preferring tie-break (D190-1 rec 3, 2026-08-10) ------------
    # `limit` caps how many of the (possibly hundreds of) equal/near-equal-
    # score logical pairs actually get emitted, and until now ties broke on
    # path string alone -- pure alphabetical luck, blind to which pairs would
    # actually shrink `orphans_remaining` if applied. Motivated by auditor
    # D190-1 (2026-08-09): orphans_remaining kept RISING (11,011 -> 11,033)
    # across three days of autolink runs that DID apply ~50 edges/day.
    # Fetching the live orphan set once and preferring pairs that touch one
    # is free (one extra query) and, when a real tie exists, steers the
    # capped selection toward the pairs that actually shrink the count.
    # ⚠ measured 2026-08-10: on the CURRENT corpus (~75% of all 14,596 nodes
    # are orphans by this definition) a live A/B on production data showed
    # NO delta in the top-50 selection either way (48/50 touched an orphan
    # before AND after) -- the pool is so orphan-saturated that ties rarely
    # decide anything yet. Unit-level test proves the mechanism fires
    # correctly on a constructed tie (see tests/); whether it moves the
    # real orphans_remaining trend needs a fresh read after this ships and
    # the corpus's orphan ratio has room to move -- do not claim it "worked"
    # off this one wake's snapshot.
    orphan_paths = {r["path"] for r in graph_mod.get_orphans(graph)}

    def _touches_orphan(canon_pair: tuple[str, str]) -> bool:
        ca, cb = canon_pair
        return any(
            p in orphan_paths
            for p in spellings_by_canon[ca] + spellings_by_canon[cb]
        )

    # Highest score first; on ties, orphan-touching pairs win; then path for
    # determinism. Oversample then filter already-linked pairs.
    out: list[dict] = []
    for (ca, cb), ((a, b), score) in sorted(
        logical.items(),
        key=lambda x: (-x[1][1], 0 if _touches_orphan(x[0]) else 1, x[0]),
    ):
        if len(out) >= limit:
            break
        # Already-linked test is asked in LOGICAL space: an edge recorded
        # under any mirror spelling means this pair is connected. Asking it
        # per-spelling is what let the same pair be re-applied 4x.
        existing = graph.run_scalar(
            """MATCH (x:Node)-[:LINK]-(y:Node)
               WHERE x.path IN $pa AND y.path IN $pb
               RETURN count(*) > 0 AS has""",
            pa=spellings_by_canon[ca], pb=spellings_by_canon[cb],
        )
        if existing:
            continue
        out.append({
            "candidate_type": "link",
            "nodes": [a, b],
            "reason": f"content token jaccard={score:.2f}, no LINK edge",
            "priority": round(min(0.75, 0.3 + score), 2),
            "score": round(score, 3),
            "source": "content_similarity",
        })
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
            "source": "cosearch",
        })
    candidates.sort(key=lambda c: -c.get("cooccur_count", 0))
    return candidates


def find_dead_weight(
    graph,
    *,
    cold_days: int = 60,
    min_layer: int = 3,
    limit: int = 50,
) -> list[dict]:
    """Indexed nodes at layer >= min_layer that have not been retrieved by
    any (:Access) in cold_days. Emits one candidate per node of type
    'dead_weight'.

    Rationale: memory accrues by adding. Without an opposing signal, it
    bloats. Access-log-driven dead-weight is the S3 counter-pressure — we
    ask "is this indexed thing still paying its keep?" and surface the
    ones that haven't been retrieved in a long window. The agent decides
    whether to archive, split, re-link, or (often) simply tighten the
    title so search finds it.

    This is the dream-briefing counterpart to the
    ``memfs access-report --kind gap-signals`` view. Both read the same
    (:Access) data; this emits candidate_type='dead_weight' dicts so the
    nightly briefing can stream it alongside orphan/merge/split/link.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=int(cold_days))).isoformat()
    rows = graph.run(
        "MATCH (n:Node) "
        "WHERE coalesce(n.layer, 0) >= $min_layer "
        "OPTIONAL MATCH (n)<-[:RETRIEVED]-(a:Access) "
        "WITH n, max(a.ts) AS last_hit "
        "WHERE last_hit IS NULL OR last_hit < $cutoff "
        "RETURN n.path AS path, n.title AS title, n.layer AS layer, "
        "       n.modified_at AS modified_at, last_hit "
        "ORDER BY coalesce(last_hit, '') ASC, n.path ASC "
        "LIMIT $limit",
        min_layer=int(min_layer),
        cutoff=cutoff,
        limit=int(limit),
    )

    out: list[dict] = []
    for row in rows:
        last_hit = row.get("last_hit")
        never_retrieved = last_hit is None
        reason = (
            f"layer {row.get('layer')}, never retrieved by any access "
            f"(dead_weight>={cold_days}d)"
            if never_retrieved else
            f"layer {row.get('layer')}, last retrieved {last_hit} "
            f"(dead_weight>={cold_days}d)"
        )
        out.append({
            "candidate_type": "dead_weight",
            "nodes": [row["path"]],
            "reason": reason,
            "priority": 0.35 if never_retrieved else 0.25,
            "title": row.get("title"),
            "layer": row.get("layer"),
            "modified_at": row.get("modified_at"),
            "last_hit": last_hit,
            "never_retrieved": never_retrieved,
        })
    return out


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
    dead_weight_days = getattr(args, "dead_weight_days", 60)
    dead_weight_min_layer = getattr(args, "dead_weight_min_layer", 3)

    candidates: list[dict] = []
    candidates.extend(find_orphans(graph, orphan_days=orphan_days))
    candidates.extend(find_near_duplicates(graph, mem_home=mem_home))
    candidates.extend(find_bloated_files(mem_home, bloat_lines=bloat_lines, bloat_bytes=bloat_bytes))
    candidates.extend(find_dirs_missing_index(mem_home))
    candidates.extend(find_cosearched_unlinked(graph))
    candidates.extend(find_content_similar_unlinked(graph))
    candidates.extend(find_stale_facts(graph))
    candidates.extend(find_dead_weight(
        graph,
        cold_days=dead_weight_days,
        min_layer=dead_weight_min_layer,
    ))

    # Highest priority first
    candidates.sort(key=lambda c: -c.get("priority", 0.0))
    return candidates
