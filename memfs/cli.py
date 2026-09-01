#!/usr/bin/env python3
"""memfs — Unix-native memory filesystem for LLM agents (Neo4j backend).

Agent prompt (3 sentences):
  Your memory lives in $MEM_HOME. Read and write files normally with any tool.
  Use `memfs grep <query>` to search — connections strengthen when you search
  and weaken over time.
"""

import argparse
import json
import os
import sys

from memfs.graph import create_db, connect, count_nodes, count_edges, count_queries, get_meta
from memfs.indexer import index_directory, reindex as do_reindex
from memfs.search import grep as do_grep
from memfs.decay import run_decay
from memfs.watcher import start_watcher, stop_watcher, watcher_status
from memfs.access import (
    access_summary, hot_nodes, cold_nodes, empty_hit_queries, gap_signals,
)


def out(obj):
    """Print NDJSON line to stdout."""
    print(json.dumps(obj))


def err(obj):
    """Print NDJSON error to stderr."""
    print(json.dumps(obj), file=sys.stderr)


def get_mem_home(args=None):
    """Resolve MEM_HOME from args or environment."""
    if args and hasattr(args, "dir") and args.dir:
        return os.path.abspath(args.dir)
    return os.environ.get("MEM_HOME", os.getcwd())


def _connect_or_die():
    """Connect to Neo4j with a helpful error if unreachable."""
    try:
        graph = connect()
        # Force a round-trip so we fail fast if server is down
        graph.run_scalar("RETURN 1")
        return graph
    except Exception as e:
        err({
            "error": "neo4j_unreachable",
            "detail": str(e),
            "hint": "Start Neo4j: cd ~/apps/memfs && docker compose up -d neo4j",
        })
        sys.exit(3)


# --- Commands ---

def cmd_init(args):
    mem_home = os.path.abspath(args.dir) if args.dir else os.getcwd()
    os.makedirs(os.path.join(mem_home, ".mem"), exist_ok=True)

    try:
        create_db()
    except Exception as e:
        err({
            "error": "neo4j_unreachable",
            "detail": str(e),
            "hint": "Start Neo4j: cd ~/apps/memfs && docker compose up -d neo4j",
        })
        sys.exit(3)

    graph = _connect_or_die()
    try:
        count = index_directory(graph, mem_home)
        edges = count_edges(graph)
    finally:
        graph.close()

    out({"action": "init", "mem_home": mem_home, "nodes": count, "edges": edges})


def cmd_grep(args):
    graph = _connect_or_die()
    try:
        results = do_grep(
            graph, args.query, limit=args.limit,
            layer=args.layer, fresh_only=args.fresh_only,
            no_siblings=getattr(args, "no_siblings", False),
            siblings_top_n=getattr(args, "siblings_top_n", 3),
        )
    finally:
        graph.close()

    for r in results:
        out(r)


def cmd_ls(args):
    mem_home = get_mem_home(args)
    graph = _connect_or_die()
    try:
        if args.orphans:
            from memfs.graph import get_orphans
            for row in get_orphans(graph):
                out({"path": row["path"], "title": row["title"],
                     "search_count": row["search_count"], "orphan": True})
            return

        subdir = args.subdir
        # Filter out :Node stubs (broken wikilink placeholders) — content_hash
        # is null on stubs, set on every real upsert_node. Without this filter
        # `memfs ls` lists bogus entries with title=null/layer=null that
        # don't correspond to any file.
        if subdir:
            subdir = subdir.rstrip("/")
            rows = graph.run(
                "MATCH (n:Node) WHERE n.path STARTS WITH $prefix "
                "AND n.content_hash IS NOT NULL "
                "RETURN n.path AS path, n.title AS title, n.layer AS layer "
                "ORDER BY n.path",
                prefix=subdir + "/",
            )
        else:
            rows = graph.run(
                "MATCH (n:Node) WHERE n.content_hash IS NOT NULL "
                "RETURN n.path AS path, n.title AS title, n.layer AS layer "
                "ORDER BY n.path"
            )

        if args.verbose:
            for row in rows:
                path = row["path"]
                links_out = graph.run_scalar(
                    "MATCH (:Node {path: $p})-[r:LINK]->() RETURN count(r)", p=path,
                ) or 0
                links_in = graph.run_scalar(
                    "MATCH ()-[r:LINK]->(:Node {path: $p}) RETURN count(r)", p=path,
                ) or 0
                search_hits = graph.run_scalar(
                    "MATCH (n:Node {path: $p}) RETURN coalesce(n.search_count, 0)",
                    p=path,
                ) or 0
                out({"path": path, "title": row["title"], "layer": row["layer"],
                     "links_out": int(links_out), "links_in": int(links_in),
                     "search_hits": int(search_hits)})
        else:
            for row in rows:
                out({"path": row["path"]})
    finally:
        graph.close()


def _resolve_index_scopes(args) -> tuple[list[tuple[str, str]], bool]:
    """Resolve the list of (root_id, mem_home) scopes for index-drift checks.

    Index discipline is by-construction global: drift in any indexed tree is
    a problem regardless of which "primary" root a caller pinned. So when no
    explicit scope is set, iterate every configured root in
    ``~/.config/memfs/roots.json``. If $HOME is not yet covered by any
    configured root, append a synthetic ``default`` scope at $HOME (covers
    legacy nodes ingested before multi-root tagging).

    With explicit ``--dir`` or ``MEM_HOME`` env, honor the caller's
    single-scope intent and use ``DEFAULT_ROOT_ID`` against that path only.

    Returns ``(scopes, explicit_dir)`` where ``explicit_dir`` is True if the
    caller asked for single-scope behavior (so callers can drop the
    ``root_id:`` key prefix when aggregating).

    Shared by ``cmd_status`` and ``cmd_check_indexes`` — keeping the scope
    resolution in one place is the regression guard for the 2026-05-03
    "status drift_findings: 0 while check-indexes: 702" divergence.

    Synthetic-default guard (added 2026-05-03 — n=2 in
    `verify-detector-premise` watch): the original guard checked
    ``rid == DEFAULT_ROOT_ID and path == home`` — but in production the
    root at $HOME is ``alfred-home`` (not ``default``). The predicate was
    always False, so a synthetic ``default`` scope at $HOME got appended on
    top of alfred-home. Reindex then walked /home/mike under TWO root_ids
    and produced two parallel Node populations mirroring each other
    byte-for-byte (alfred substrate: 4848+4848 nodes; check-indexes:
    1351 drift findings of which 607 were exact alfred-home/default
    duplicates). New guard: skip synthesis whenever ANY existing scope's
    path equals $HOME, regardless of root_id.
    """
    from memfs.roots import load_roots
    from memfs.graph import DEFAULT_ROOT_ID

    explicit_dir = bool(
        (args and getattr(args, "dir", None))
        or os.environ.get("MEM_HOME")
    )

    if explicit_dir:
        return [(DEFAULT_ROOT_ID, get_mem_home(args))], True

    scopes: list[tuple[str, str]] = []
    try:
        for r in load_roots():
            scopes.append((r.id, r.path))
    except RuntimeError:
        pass
    home = os.path.expanduser("~")
    if not any(path == home for _, path in scopes):
        scopes.append((DEFAULT_ROOT_ID, home))
    if not scopes:
        scopes = [(DEFAULT_ROOT_ID, get_mem_home(args))]
    return scopes, False


def cmd_status(args):
    from memfs.index_render import check_all as check_all_indexes

    graph = _connect_or_die()
    try:
        nodes = count_nodes(graph)
        link_edges = count_edges(graph, type="link")
        search_edges = count_edges(graph, type="search")
        queries = count_queries(graph)
        last_index = get_meta(graph, "last_index")
        last_decay = get_meta(graph, "last_decay")
        # Index health (added 2026-05-01): per-directory `index.md` drift.
        # See memfs/index_render.py — memfs is the holistic memory system,
        # so index.md files are part of its responsibility. Iterate every
        # configured root (added 2026-05-03 — see _resolve_index_scopes
        # docstring; status was previously single-scope and silently
        # under-counted drift on multi-root substrates).
        try:
            handcrafted_count = graph.run_one(
                "MATCH (n:Node) WHERE n.is_handcrafted = true RETURN count(n) AS c"
            )["c"]
        except Exception:
            handcrafted_count = 0
        try:
            scopes, explicit_dir = _resolve_index_scopes(args)
            aggregated_drift: dict[str, list[str]] = {}
            for root_id, mh in scopes:
                drift_map = check_all_indexes(graph, mh, root_id=root_id)
                for d, findings in drift_map.items():
                    key = d if explicit_dir else f"{root_id}:{d}"
                    aggregated_drift[key] = findings
            drift_count = sum(len(v) for v in aggregated_drift.values())
            drift_dirs = len(aggregated_drift)
        except Exception:
            drift_count = -1
            drift_dirs = -1
    finally:
        graph.close()

    out({
        "nodes": nodes,
        "edges": {"link": link_edges, "search": search_edges},
        "queries": queries,
        "last_index": last_index,
        "last_decay": last_decay,
        "indexes": {
            "handcrafted": handcrafted_count,
            "drift_findings": drift_count,
            "drifted_dirs": drift_dirs,
        },
    })


def cmd_watch(args):
    mem_home = get_mem_home(args)

    if args.stop:
        stopped = stop_watcher(mem_home)
        out({"action": "watch_stop", "stopped": stopped})
        return

    if args.status:
        status = watcher_status(mem_home)
        out(status)
        return

    # Sanity: server reachable
    graph = _connect_or_die()
    graph.close()

    start_watcher(mem_home, daemon=args.daemon)


def cmd_decay(args):
    graph = _connect_or_die()
    try:
        stats = run_decay(graph, dry_run=args.dry_run)
    finally:
        graph.close()

    out({"action": "decay", "dry_run": args.dry_run, **stats})


def cmd_skills(args):
    """List, output, or install bundled skills."""
    skills_dir = os.path.join(os.path.dirname(__file__), "skills")

    if not os.path.isdir(skills_dir):
        err({"error": "no_skills_dir", "path": skills_dir})
        return

    if args.action == "setup":
        _skills_setup(skills_dir, args)
        return

    if args.action and args.action not in ("list",):
        skill_path = os.path.join(skills_dir, f"{args.action}.md")
        if not os.path.exists(skill_path):
            err({"error": "skill_not_found", "name": args.action,
                 "available": [f.replace(".md", "") for f in os.listdir(skills_dir) if f.endswith(".md")]})
            sys.exit(1)
        with open(skill_path) as f:
            print(f.read())
    else:
        for filename in sorted(os.listdir(skills_dir)):
            if filename.endswith(".md"):
                name = filename.replace(".md", "")
                with open(os.path.join(skills_dir, filename)) as f:
                    lines = f.readlines()
                desc = ""
                for line in lines[1:]:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        desc = line[:120]
                        break
                out({"name": name, "description": desc})


def _skills_setup(skills_dir: str, args):
    harness = args.harness or _detect_harness()

    if harness == "claude-code":
        _setup_claude_code(skills_dir)
    elif harness == "generic":
        _setup_generic(skills_dir)
    else:
        err({"error": "unknown_harness", "harness": harness,
             "supported": ["claude-code", "generic"]})
        sys.exit(1)


def _detect_harness() -> str:
    claude_dir = os.path.expanduser("~/.claude")
    if os.path.isdir(claude_dir):
        return "claude-code"
    return "generic"


def _setup_claude_code(skills_dir: str):
    cwd = os.getcwd()
    project_skills = os.path.join(cwd, ".claude", "skills")
    os.makedirs(project_skills, exist_ok=True)

    installed = []
    for filename in sorted(os.listdir(skills_dir)):
        if not filename.endswith(".md"):
            continue
        name = filename.replace(".md", "")
        skill_dir = os.path.join(project_skills, f"memfs-{name}")
        os.makedirs(skill_dir, exist_ok=True)
        dest = os.path.join(skill_dir, "SKILL.md")

        with open(os.path.join(skills_dir, filename)) as f:
            content = f.read()

        with open(dest, "w") as f:
            f.write(content)

        installed.append({"name": f"memfs-{name}", "path": dest})
        out({"action": "installed", "skill": f"memfs-{name}", "path": dest})

    mem_home = os.environ.get("MEM_HOME", "")
    prompt_fragment = (
        f"Your memory lives in `{mem_home or '$MEM_HOME'}`. "
        "Read and write files normally with any tool. "
        "Use `memfs grep <query>` to search — connections between files "
        "strengthen when you search for them and weaken over time. "
        "Use /memfs-recall before tasks needing context. "
        "Use /memfs-dream at end of sessions to consolidate memory."
    )
    out({"action": "setup_complete", "skills_installed": len(installed),
         "system_prompt": prompt_fragment})


def _setup_generic(skills_dir: str):
    out({"action": "generic_setup", "instructions": "Copy these skills into your agent framework."})
    for filename in sorted(os.listdir(skills_dir)):
        if filename.endswith(".md"):
            name = filename.replace(".md", "")
            with open(os.path.join(skills_dir, filename)) as f:
                content = f.read()
            out({"skill": f"memfs-{name}", "content": content})


def reindex_all_roots(
    graph, scopes: list[tuple[str, str]],
) -> dict:
    """Multi-root-aware reindex. For each ``(root_id, mem_home)`` scope:

    1. ``clear_root_data(graph, root_id)`` — purge that root's Node nodes
       (DETACH DELETE removes attached LINK/SEARCH edges).
    2. ``index_directory(graph, mem_home, root_id=root_id)`` — walk that
       root's filesystem, re-index files under that root_id.
    3. Re-render every directory's ``index.md`` for that root.

    Other roots are untouched.

    Added 2026-05-03 to fix the multi-root-unaware ``cmd_reindex`` bug
    (drift_findings escalating 363 → 640 → 923 → 1702 on the alfred
    substrate). Shared scope resolution with ``cmd_status`` and
    ``cmd_check_indexes`` via ``_resolve_index_scopes`` is the regression
    guard.

    Returns aggregated counts: ``{"per_root": [...], "total_nodes": N,
    "total_indexes_wrote": N}``.
    """
    from memfs.indexer import reindex as _reindex
    from memfs.index_render import render_all

    per_root = []
    total_nodes = 0
    total_wrote = 0
    for root_id, mem_home in scopes:
        node_count = _reindex(graph, mem_home, root_id=root_id)
        index_results = render_all(graph, mem_home, root_id=root_id)
        per_root.append({
            "root_id": root_id,
            "mem_home": mem_home,
            "nodes": node_count,
            "indexes": index_results,
        })
        total_nodes += node_count
        total_wrote += index_results.get("wrote", 0)
    return {
        "per_root": per_root,
        "total_nodes": total_nodes,
        "total_indexes_wrote": total_wrote,
    }


def cmd_reindex(args):
    """Reindex the graph from filesystem.

    Default (no ``--dir``, no ``MEM_HOME`` env): iterate ALL configured
    roots from ``~/.config/memfs/roots.json`` plus a synthetic default
    scope at $HOME, reindex each independently. This is the multi-root-
    aware path added 2026-05-03 — see ``reindex_all_roots`` docstring.

    With explicit ``--dir`` or ``MEM_HOME``: legacy single-root behavior
    against ``DEFAULT_ROOT_ID``. Useful for one-off corpora.
    """
    scopes, explicit_dir = _resolve_index_scopes(args)
    graph = _connect_or_die()
    try:
        result = reindex_all_roots(graph, scopes)
        edges = count_edges(graph)
    finally:
        graph.close()

    out({
        "action": "reindex",
        "scopes": [{"root_id": rid, "mem_home": mh} for rid, mh in scopes],
        "explicit_dir": explicit_dir,
        "per_root": result["per_root"],
        "total_nodes": result["total_nodes"],
        "edges": edges,
    })


def cmd_check_indexes(args):
    """Check per-directory index.md files for drift against graph view.

    With --fix, auto-render any drifted non-handcrafted indexes.
    Exit code: 0 if clean, 1 if any drift remains after optional --fix.

    ⚠ KNOWN DEFECT, confirmed 2026-09-01 (see
    ~/resources/memfs-check-indexes-fix-graph-staleness-defect.md): --fix
    renders each index.md from the Neo4j GRAPH's view of a directory, never
    from a live os.listdir() of disk. When the graph has drifted from disk
    (files moved/deleted outside the watcher, or never ingested), --fix
    faithfully writes the graph's STALE view — confirmed 5/5 wrong on the
    one at-scale run attempted (34 dirs): phantom entries for files that
    don't exist, and real on-disk content silently dropped from 2 already-
    committed indexes. Do not trust this at scale on its self-report alone
    ("wrote: N" is not "wrote correctly") — diff every touched index.md
    against a live directory listing before accepting the result, or run
    reindex first to refresh the graph. The real fix (reconcile graph vs
    disk before rendering, or refuse --fix on a stale scope) is filed in
    ~/areas/research-queue.md, unresolved as of this comment.

    Scope resolution (single-scope vs multi-root iteration) is delegated to
    ``_resolve_index_scopes`` — same helper ``cmd_status`` uses. Keeping the
    two commands sharing one resolver is the regression guard for the
    2026-05-03 divergence (status reported drift_findings: 0 while
    check-indexes reported 702 on the same substrate).

    ``by_root`` (added 2026-08-31): per-root_id drifted_dirs/drift_findings,
    COUNTED AND PRINTED on every run rather than left as a one-off manual
    `details`-key-prefix analysis. WHY: architecture.md invariant 11 already
    documents a 2026-07-29 finding that runtime-state churn (root_id
    `alfred-state`) was 59% of the flat total and drowned the ~126 findings
    that actually cost retrieval (root_id `alfred-home`, under
    `resources*`) — "the alarm was not blind, it was DEAFENING". That
    breakdown was hand-computed once from `details` key prefixes and never
    became a standing field, so every run since re-presented one flat
    number and the same wallpaper risk (gate #72(b): a wall that becomes
    wallpaper). `by_root` makes the split load-bearing: any reader (this
    CLI's human caller, `worklist-digest`, a future dashboard wiring) gets
    the per-root_id counts without re-deriving them from `details`.
    ⛔ Do NOT use this to silently exclude a root from `drifted_dirs`/
    `drift_findings` — those stay the true flat totals; `by_root` is an
    additional, never a replacement, view (the printed-not-silent rule this
    was filed to satisfy).
    """
    from memfs.index_render import check_all, render_all

    scopes, explicit_dir = _resolve_index_scopes(args)
    aggregated_drift: dict[str, list[str]] = {}
    aggregated_fixed: dict[str, int] = {}
    by_root: dict[str, dict[str, int]] = {}
    do_fix = getattr(args, "fix", False)
    graph = _connect_or_die()
    try:
        for root_id, mem_home in scopes:
            if do_fix:
                fixed = render_all(graph, mem_home, root_id=root_id)
                drift_map = check_all(graph, mem_home, root_id=root_id)
                for k, v in fixed.items():
                    aggregated_fixed[k] = aggregated_fixed.get(k, 0) + v
            else:
                drift_map = check_all(graph, mem_home, root_id=root_id)
            root_bucket = by_root.setdefault(
                root_id, {"drifted_dirs": 0, "drift_findings": 0}
            )
            root_bucket["drifted_dirs"] += len(drift_map)
            root_bucket["drift_findings"] += sum(
                len(v) for v in drift_map.values()
            )
            for d, findings in drift_map.items():
                key = d if explicit_dir else f"{root_id}:{d}"
                aggregated_drift[key] = findings
    finally:
        graph.close()

    payload = {
        "action": "check-indexes",
        "scopes": [{"root_id": rid, "mem_home": mh} for rid, mh in scopes],
        "drifted_dirs": len(aggregated_drift),
        "drift_findings": sum(len(v) for v in aggregated_drift.values()),
        "by_root": by_root,
        "details": aggregated_drift,
    }
    if do_fix:
        payload["fixed"] = aggregated_fixed
    out(payload)
    if aggregated_drift:
        sys.exit(1)


# --- M4 commands (calibration ledger + contradictions) ---

def cmd_claim(args):
    from memfs.calibration import record_claim
    mem_home = get_mem_home(args)

    if getattr(args, "auto", False):
        # Batch mode: read one JSON object per stdin line, record each.
        # Each object must have: text, confidence. Optional: scope, to.
        graph = _connect_or_die()
        try:
            n_ok = 0
            n_err = 0
            for lineno, line in enumerate(sys.stdin, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    err({"error": "bad_json", "lineno": lineno, "detail": str(e)})
                    n_err += 1
                    continue
                try:
                    claim_id = record_claim(
                        graph,
                        text=obj["text"],
                        confidence=float(obj["confidence"]),
                        scope=obj.get("scope", args.scope or "general"),
                        claimed_to=obj.get("to", args.to or "log"),
                        source=obj.get("source", args.source),
                        mem_home=mem_home,
                    )
                    out({"action": "claim", "claim_id": claim_id,
                         "text": obj["text"][:80], "confidence": obj["confidence"]})
                    n_ok += 1
                except (KeyError, ValueError, TypeError) as e:
                    err({"error": "bad_record", "lineno": lineno,
                         "detail": str(e), "obj": obj})
                    n_err += 1
            out({"action": "claim_auto_summary", "ok": n_ok, "errors": n_err})
        finally:
            graph.close()
        return

    # Single-claim mode (legacy)
    if not args.text or args.confidence is None or not args.scope:
        err({"error": "missing_args",
             "hint": "provide --text, --confidence, --scope; or pass --auto and pipe JSON lines"})
        sys.exit(2)

    graph = _connect_or_die()
    try:
        claim_id = record_claim(
            graph,
            text=args.text,
            confidence=args.confidence,
            scope=args.scope,
            claimed_to=args.to,
            source=args.source,
            mem_home=mem_home,
        )
    finally:
        graph.close()
    out({"action": "claim", "claim_id": claim_id})


def cmd_verify(args):
    from memfs.calibration import verify_claim
    graph = _connect_or_die()
    try:
        mem_home = get_mem_home(args)
        verify_claim(
            graph, claim_id=args.claim_id, outcome=args.outcome,
            note=args.note, mem_home=mem_home,
        )
    finally:
        graph.close()
    out({"action": "verify", "claim_id": args.claim_id, "outcome": args.outcome})


def cmd_calibration(args):
    from memfs.calibration import (
        calibration_curve, calibration_timeseries,
        rebuild_from_ledger, snapshot_trend,
    )
    graph = _connect_or_die()
    try:
        rebuild_stats = None
        if getattr(args, "rebuild", False):
            rebuild_stats = rebuild_from_ledger(
                graph, mem_home=get_mem_home(args),
            )

        # --trend mode: emit one NDJSON line per bucket instead of a single
        # point-in-time curve. Rolling ECE over `--trend-window` sliced in
        # `--trend-bucket`-day buckets.
        if getattr(args, "trend", False):
            series = calibration_timeseries(
                graph,
                window_days=getattr(args, "trend_window", 90),
                bucket_days=getattr(args, "trend_bucket", 7),
                scope=args.scope,
                source_type=getattr(args, "source_type", None),
            )
            # Optional snapshot write — record the most recent bucket to the
            # durable trend ledger so successive invocations build history.
            if getattr(args, "snapshot", False) and series:
                snapshot_trend(get_mem_home(args), series[-1])
            for s in series:
                out(s)
            return

        curve = calibration_curve(
            graph, window_days=args.window, scope=args.scope,
            source_type=getattr(args, "source_type", None),
            include_source_breakdown=getattr(args, "by_source", False),
        )
    finally:
        graph.close()
    if rebuild_stats is not None:
        curve["rebuild"] = rebuild_stats
    out(curve)


def cmd_contradictions_scan(args):
    """Run S2 contradiction detection across all layer-3+ nodes."""
    from memfs.contradiction import scan_corpus
    graph = _connect_or_die()
    try:
        result = scan_corpus(
            graph,
            overlap_threshold=args.overlap_threshold,
            candidate_limit=args.candidate_limit,
        )
    finally:
        graph.close()
    out(result)


def cmd_ingest_session(args):
    """Ingest a Claude Code session jsonl into memfs."""
    from memfs.ingest import ingest_session
    mem_home = get_mem_home(args)
    jsonl_path = os.path.abspath(args.jsonl_path)
    if not os.path.isfile(jsonl_path):
        err({"error": "jsonl_not_found", "path": jsonl_path})
        sys.exit(2)
    result = ingest_session(jsonl_path, mem_home)
    out(result)


def cmd_dream_briefing(args):
    """Emit NDJSON candidates for a dream consolidation pass."""
    from memfs.dream import run_briefing
    graph = _connect_or_die()
    try:
        candidates = run_briefing(graph, mem_home=get_mem_home(args), args=args)
    finally:
        graph.close()
    for c in candidates:
        out(c)


def cmd_link_suggest(args):
    """Suggest LINK edges via content similarity (for corpora where
    authored [[wikilinks]] are sparse and SEARCH traffic is too low for
    co-search-based candidates). Emits NDJSON link candidates — the same
    shape the dream briefing emits, so downstream tooling is uniform.

    ``--census`` switches to reporting the true uncapped pool size instead
    (D190-1 rec 1) — see that flag's help for why this exists.
    """
    from memfs.dream import count_content_similarity_pool, find_content_similar_unlinked
    graph = _connect_or_die()
    try:
        if args.census:
            pool_size = count_content_similarity_pool(
                graph, min_score=args.min_score, max_score=args.max_score,
            )
            out({"pool_size": pool_size})
            return
        candidates = find_content_similar_unlinked(
            graph,
            limit=args.limit,
            min_score=args.min_score,
            max_score=args.max_score,
        )
    finally:
        graph.close()
    for c in candidates:
        out(c)


def cmd_link_apply(args):
    """Materialize LINK edge(s). Single-pair mode (positional args) or
    batch mode (--from-stdin consumes NDJSON). Idempotent via MERGE.

    In stdin mode, only lines with ``candidate_type == "link"`` are applied;
    other candidate types (merge/split/orphan/...) are skipped so that a
    full dream-briefing stream can be piped in without filtering upstream.
    """
    from memfs.graph import upsert_link_edge, resolve_node_root_id
    graph = _connect_or_die()
    applied = 0
    skipped = 0
    errors = 0
    try:
        if args.from_stdin:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    c = json.loads(raw)
                except json.JSONDecodeError:
                    errors += 1
                    err({"error": "bad_json", "line": raw[:80]})
                    continue
                if c.get("candidate_type") != "link":
                    skipped += 1
                    continue
                nodes = c.get("nodes") or []
                if len(nodes) != 2 or not all(isinstance(n, str) for n in nodes):
                    errors += 1
                    err({"error": "bad_nodes", "candidate": c})
                    continue
                a, b = nodes
                # Carry the candidate's source label onto the edge so that
                # clear_link_edges_from (file re-index) doesn't wipe it.
                edge_source = c.get("source") or "dream"
                # Resolve each path to its REAL existing root_id (2026-08-21
                # fix) -- candidate NDJSON carries bare paths with no
                # root_id, and merging against an unresolved default used to
                # create a phantom duplicate Node disconnected from the real
                # corpus. See resolve_node_root_id's docstring.
                src_root = resolve_node_root_id(graph, a)
                tgt_root = resolve_node_root_id(graph, b)
                upsert_link_edge(graph, a, b, strength=args.strength,
                                 source=edge_source,
                                 src_root_id=src_root, tgt_root_id=tgt_root)
                applied += 1
                out({"applied": [a, b], "strength": args.strength,
                     "source": edge_source,
                     "score": c.get("score"),
                     "cooccur_count": c.get("cooccur_count")})
        else:
            if not args.source or not args.target:
                err({"error": "source_and_target_required",
                     "hint": "memfs link-apply <src> <tgt> OR --from-stdin"})
                sys.exit(2)
            src_root = resolve_node_root_id(graph, args.source)
            tgt_root = resolve_node_root_id(graph, args.target)
            upsert_link_edge(graph, args.source, args.target,
                             strength=args.strength,
                             source=args.link_source,
                             src_root_id=src_root, tgt_root_id=tgt_root)
            applied += 1
            out({"applied": [args.source, args.target],
                 "strength": args.strength,
                 "source": args.link_source})
    finally:
        graph.close()
    # Summary goes to stderr so stdout stays NDJSON-clean for piping
    err({"summary": {"applied": applied, "skipped": skipped, "errors": errors}})


def cmd_access_report(args):
    """Report on grep retrieval patterns.

    Default output = summary (total accesses, hits, empty hits, distinct
    queries, distinct nodes touched) in the window.

    --kind hot          : nodes retrieved most often
    --kind cold         : nodes not retrieved in the window (or never)
    --kind empty        : queries that returned 0 results (memory gaps)
    --kind gap-signals  : combined actionable view — empty-hit queries
                          (what was asked for and not found) plus
                          dead-weight nodes (indexed but never retrieved)
                          plus stale hotspots (retrieved but not updated).
                          Emitted as a single JSON object.
    """
    graph = _connect_or_die()
    try:
        kind = getattr(args, "kind", None)
        window = getattr(args, "window_days", None)
        limit = getattr(args, "limit", 20)
        cold_days = getattr(args, "cold_days", 30)
        min_layer = getattr(args, "min_layer", 3)

        if kind in (None, "summary"):
            out(access_summary(graph, window_days=window))
            return

        if kind == "hot":
            rows = hot_nodes(graph, window_days=window, limit=limit)
        elif kind == "cold":
            rows = cold_nodes(graph, window_days=window, limit=limit)
        elif kind == "empty":
            rows = empty_hit_queries(graph, window_days=window, limit=limit)
        elif kind == "gap-signals":
            out(gap_signals(graph, window_days=window,
                            cold_days=cold_days, min_layer=min_layer,
                            limit=limit))
            return
        else:
            err({"error": "unknown_kind", "kind": kind,
                 "allowed": ["summary", "hot", "cold", "empty",
                             "gap-signals"]})
            sys.exit(2)

        for row in rows:
            out(row)
    finally:
        graph.close()


def cmd_dream_log(args):
    """Record a dream-pass action / start / finish / show / rebuild.

    Modes (``args.mode``):
      start    — open a run with input candidate counts by type
      action   — record one action taken during the run
      finish   — close the run, stamp status_delta
      show     — dump one run by id
      rebuild  — replay the JSONL ledger into Neo4j (after DB wipe)

    Uniform semantics with the calibration ledger: files-as-truth
    (``.mem/dream-log.jsonl``) + Neo4j derived cache. Each write appends
    one ledger line + one/two Cypher statements.
    """
    from memfs.dream_log import (
        start_run, record_action, finish_run, get_run, rebuild_from_ledger,
    )
    mem_home = get_mem_home(args)
    mode = args.mode

    if mode == "rebuild":
        graph = _connect_or_die()
        try:
            stats = rebuild_from_ledger(graph, mem_home=mem_home)
        finally:
            graph.close()
        out({"action": "dream_log_rebuild", **stats})
        return

    if not args.run_id:
        err({"error": "run_id_required",
             "hint": "memfs dream-log <mode> <run-id> ..."})
        sys.exit(2)

    graph = _connect_or_die()
    try:
        if mode == "start":
            cbt = {}
            if args.candidates_json:
                try:
                    cbt = json.loads(args.candidates_json)
                except json.JSONDecodeError as e:
                    err({"error": "bad_candidates_json", "detail": str(e)})
                    sys.exit(2)
            start_run(graph, run_id=args.run_id,
                      candidates_by_type=cbt, mem_home=mem_home)
            out({"action": "dream_log_start", "run_id": args.run_id,
                 "candidates_by_type": cbt})
            return

        if mode == "action":
            if not args.action_kind or not args.candidate_type:
                err({"error": "missing_args",
                     "hint": "--action and --candidate-type are required"})
                sys.exit(2)
            nodes = list(args.nodes or [])
            action_id = record_action(
                graph, run_id=args.run_id,
                action=args.action_kind,
                candidate_type=args.candidate_type,
                nodes=nodes,
                claim_id=args.claim_id,
                note=args.note,
                mem_home=mem_home,
            )
            out({"action": "dream_log_action", "action_id": action_id,
                 "run_id": args.run_id, "kind": args.action_kind,
                 "candidate_type": args.candidate_type, "nodes": nodes})
            return

        if mode == "finish":
            sd = {}
            if args.status_delta_json:
                try:
                    sd = json.loads(args.status_delta_json)
                except json.JSONDecodeError as e:
                    err({"error": "bad_status_delta_json", "detail": str(e)})
                    sys.exit(2)
            finish_run(graph, run_id=args.run_id,
                       status_delta=sd, mem_home=mem_home)
            out({"action": "dream_log_finish", "run_id": args.run_id,
                 "status_delta": sd})
            return

        if mode == "show":
            run = get_run(graph, args.run_id)
            if run is None:
                err({"error": "run_not_found", "run_id": args.run_id})
                sys.exit(1)
            out(run)
            return

        err({"error": "unknown_mode", "mode": mode,
             "allowed": ["start", "action", "finish", "show", "rebuild"]})
        sys.exit(2)
    finally:
        graph.close()


def cmd_dream_report(args):
    """Summary over the last ``--window`` days of dream passes.

    Shows action rate, recurring-ignored candidates, and per-run summary.
    """
    from memfs.dream_log import dream_report
    graph = _connect_or_die()
    try:
        result = dream_report(
            graph, window_days=args.window,
            recurrence_threshold=args.recurrence_threshold,
        )
    finally:
        graph.close()
    out(result)


def cmd_freshness_scan(args):
    """Report nodes whose freshness is stale. Auto-refresh is future work."""
    graph = _connect_or_die()
    try:
        rows = graph.run(
            "MATCH (n:Node) "
            "WHERE n.freshness_verified_at IS NOT NULL "
            "  AND n.freshness_stale_after_days IS NOT NULL "
            "RETURN n.path AS path, n.title AS title, "
            "n.freshness_verified_at AS verified_at, "
            "n.freshness_stale_after_days AS stale_after, "
            "n.freshness_source_url AS source_url "
            "ORDER BY n.path"
        )
    finally:
        graph.close()

    from memfs.search import _freshness_status
    for row in rows:
        status = _freshness_status({
            "freshness_verified_at": row.get("verified_at"),
            "freshness_stale_after_days": row.get("stale_after"),
        })
        if status == "stale":
            out({
                "path": row["path"],
                "title": row["title"],
                "verified_at": row["verified_at"],
                "stale_after_days": row["stale_after"],
                "source_url": row.get("source_url"),
                "status": "stale",
            })


# --- Main ---

def _parse_window(s: str | None) -> int:
    """Parse '30d' / '7d' / '24h' / integer days."""
    if not s:
        return 30
    s = s.strip()
    if s.endswith("d"):
        return int(s[:-1])
    if s.endswith("h"):
        return max(1, int(s[:-1]) // 24)
    return int(s)


def main():
    parser = argparse.ArgumentParser(
        prog="memfs",
        description="Unix-native memory filesystem for LLM agents (Neo4j).",
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init", help="Initialize a memory root")
    p_init.add_argument("dir", nargs="?", default=None, help="Directory to initialize")

    p_grep = sub.add_parser("grep", help="Search memory (agent's primary command)")
    p_grep.add_argument("query", help="Search query")
    p_grep.add_argument("--limit", type=int, default=20, help="Max results")
    p_grep.add_argument("--layer", type=int, default=None, help="Filter by layer (1-5)")
    p_grep.add_argument("--fresh-only", action="store_true", help="Drop stale results")
    p_grep.add_argument("--no-siblings", dest="no_siblings", action="store_true",
                        help="Omit directory siblings from all results. Cuts "
                             "JSON payload ~10x on bulk queries — use when "
                             "piping grep output to downstream tools.")
    p_grep.add_argument("--siblings-top-n", dest="siblings_top_n", type=int,
                        default=3,
                        help="Attach sibling listings only to the first N "
                             "ranked results (default 3). Lower ranks keep "
                             "directory/links/index context but drop "
                             "siblings. Set to 0 to match --no-siblings.")

    p_ls = sub.add_parser("ls", help="List indexed files")
    p_ls.add_argument("subdir", nargs="?", default=None, help="Subdirectory to list")
    p_ls.add_argument("--verbose", "-v", action="store_true", help="Show edge counts")
    p_ls.add_argument("--orphans", action="store_true", help="Show files with no connections and no searches")

    sub.add_parser("status", help="Show index statistics")

    p_watch = sub.add_parser("watch", help="Start filesystem watcher daemon")
    p_watch.add_argument("--daemon", action="store_true", help="Run in background")
    p_watch.add_argument("--stop", action="store_true", help="Stop running daemon")
    p_watch.add_argument("--status", action="store_true", help="Check daemon status")

    p_skills = sub.add_parser("skills", help="List, output, or install agent skills")
    p_skills.add_argument("action", nargs="?", help="setup | list | <skill-name>")
    p_skills.add_argument("--harness", help="Agent framework (claude-code, generic)")

    p_decay = sub.add_parser("_decay", help=argparse.SUPPRESS)
    p_decay.add_argument("--dry-run", action="store_true")

    sub.add_parser("reindex", help="Rebuild index from files")

    # Index health (added 2026-05-01) — drift detection over per-directory
    # `index.md` files. memfs is the holistic memory system; index files are
    # part of its responsibility. See memfs/index_render.py.
    p_check = sub.add_parser(
        "check-indexes",
        help="Check per-directory index.md files for drift against graph view",
    )
    p_check.add_argument("--fix", action="store_true",
                         help="Auto-render any drifted (non-handcrafted) indexes. "
                              "⚠ KNOWN DEFECT: renders from graph, not live disk — "
                              "can corrupt committed indexes on a stale graph. "
                              "Diff output before trusting; see cmd_check_indexes docstring.")
    p_check.add_argument("--dir", default=None,
                         help="Scope to a single root path (overrides multi-root walk)")

    # Calibration ledger (M4)
    p_claim = sub.add_parser(
        "claim",
        help="Record a verifiable claim. "
             "With --auto, read NDJSON from stdin and batch-insert.",
    )
    p_claim.add_argument("--text", default=None,
                         help="Claim text (required unless --auto)")
    p_claim.add_argument("--confidence", type=float, default=None,
                         help="Confidence in [0,1] (required unless --auto)")
    p_claim.add_argument("--scope", default=None,
                         help="Scope label (required unless --auto; "
                              "in --auto mode, used as default)")
    p_claim.add_argument("--to", default=None,
                         help="Recipient label (default 'log'; "
                              "in --auto mode, used as default)")
    p_claim.add_argument("--source", default=None,
                         help="Provenance pointer. Convention: "
                              "'file:<abs-path>', 'tool:<name>', "
                              "'session:<id>', 'llm:<model>', 'manual'. "
                              "Unscoped strings are allowed; the prefix "
                              "before the first ':' is used for "
                              "source-type breakdowns in calibration.")
    p_claim.add_argument("--auto", action="store_true",
                         help="Batch mode: read JSON lines from stdin. "
                              "Each line: {\"text\":..., \"confidence\":..., "
                              "\"scope\":..., \"to\":..., \"source\":...}")

    p_verify = sub.add_parser("verify", help="Verify a claim outcome")
    p_verify.add_argument("claim_id")
    p_verify.add_argument("--outcome", required=True,
                          choices=["correct", "wrong", "partial"])
    p_verify.add_argument("--note", default=None)

    p_cal = sub.add_parser("calibration", help="Report calibration curve")
    p_cal.add_argument("--window", default="30d", type=_parse_window)
    p_cal.add_argument("--scope", default=None)
    p_cal.add_argument("--source-type", dest="source_type", default=None,
                       help="Filter claims by source prefix (e.g. 'file', "
                            "'tool', 'llm', 'manual'). The prefix is the "
                            "token before the first ':' in the source field.")
    p_cal.add_argument("--by-source", dest="by_source", action="store_true",
                       help="Include per-source-type accuracy breakdown in "
                            "the output. Shows n/correct/partial/wrong per "
                            "source so you can see which evidence paths "
                            "produce unreliable claims.")
    p_cal.add_argument("--rebuild", action="store_true",
                       help="Replay JSONL ledger into Neo4j before querying. "
                            "Use after a DB reset or when the cache drifts "
                            "from the durable ledger.")
    p_cal.add_argument("--trend", action="store_true",
                       help="Emit rolling-window ECE timeseries (one NDJSON "
                            "line per bucket) instead of a point-in-time "
                            "curve. Use --trend-window and --trend-bucket "
                            "to size the series.")
    p_cal.add_argument("--trend-window", dest="trend_window", type=int,
                       default=90,
                       help="Total span (in days) of the timeseries "
                            "(default 90).")
    p_cal.add_argument("--trend-bucket", dest="trend_bucket", type=int,
                       default=7,
                       help="Bucket size in days for the timeseries "
                            "(default 7).")
    p_cal.add_argument("--snapshot", action="store_true",
                       help="In --trend mode, append the most recent bucket "
                            "to <MEM_HOME>/.mem/calibration-trend.jsonl as a "
                            "durable record of this moment's ECE. Used by "
                            "the daily cron hook.")

    # Link materialization (Apr 17 — bootstrapping empty juxtaposition surface)
    p_ls = sub.add_parser("link-suggest",
                          help="Suggest LINK edges via content similarity")
    p_ls.add_argument("--limit", type=int, default=50,
                      help="Max candidates to emit (default 50)")
    p_ls.add_argument("--min-score", type=float, default=0.12,
                      help="Minimum token-jaccard score (default 0.12)")
    p_ls.add_argument("--max-score", type=float, default=0.55,
                      help="Score at/above this is a merge candidate, not a "
                           "link candidate (default 0.55)")
    p_ls.add_argument("--census", action="store_true",
                      help="Report the TRUE candidate pool size (uncapped, "
                           "mirror-deduped, before the already-linked filter) "
                           "as {\"pool_size\": N} instead of emitting "
                           "candidates. D190-1 rec 1 (2026-08-10): --limit's "
                           "default of 50 was being read as ~100% coverage "
                           "when the real pool is >=300 -- this is the honest "
                           "denominator, cheap enough to run every pass "
                           "(one node scan, no extra graph queries).")

    p_la = sub.add_parser("link-apply",
                          help="Materialize LINK edge(s). Single pair or NDJSON on stdin.")
    p_la.add_argument("source", nargs="?", default=None,
                      help="Source node path (omitted when --from-stdin)")
    p_la.add_argument("target", nargs="?", default=None,
                      help="Target node path (omitted when --from-stdin)")
    p_la.add_argument("--from-stdin", action="store_true",
                      help="Read NDJSON link candidates from stdin. "
                           "Non-'link' candidate types are silently skipped.")
    p_la.add_argument("--strength", type=float, default=1.0,
                      help="Edge strength (default 1.0)")
    p_la.add_argument("--link-source", default="manual",
                      help="Edge source label for single-pair mode. "
                           "Stdin mode carries each candidate's own source "
                           "('content_similarity' | 'cosearch' | ...). "
                           "Edges NOT labeled 'authored' survive file "
                           "re-indexing. (default 'manual')")

    # Access-frequency report (viable-memory S3 quality control)
    p_access = sub.add_parser(
        "access-report",
        help="Report on retrieval patterns from grep (hot/cold nodes, "
             "empty-hit queries). Each grep call logs one (:Access) node; "
             "this reads that log.",
    )
    p_access.add_argument("--kind", default="summary",
                          choices=["summary", "hot", "cold", "empty",
                                   "gap-signals"],
                          help="summary: totals; hot: most-retrieved nodes; "
                               "cold: nodes not retrieved in window; "
                               "empty: queries that returned no results; "
                               "gap-signals: combined actionable view "
                               "(empty-hits + dead-weight + stale-hotspots)")
    p_access.add_argument("--window-days", dest="window_days", type=int,
                          default=7,
                          help="Time window in days (default 7). 0 or "
                               "negative = all time.")
    p_access.add_argument("--cold-days", dest="cold_days", type=int,
                          default=30,
                          help="Dead-weight threshold in days (gap-signals). "
                               "Nodes not retrieved in this window become "
                               "dead_weight candidates. Default 30.")
    p_access.add_argument("--min-layer", dest="min_layer", type=int,
                          default=3,
                          help="Dead-weight layer floor (gap-signals). "
                               "Only layer >= N is considered dead weight. "
                               "Default 3.")
    p_access.add_argument("--limit", type=int, default=20,
                          help="Max rows to emit (default 20)")

    # Freshness (M5)
    sub.add_parser("freshness-scan", help="Report nodes with stale freshness stamps")

    # Contradiction batch scan (S2 — viable-memory absorption step)
    p_contra = sub.add_parser(
        "contradictions-scan",
        help="Batch-scan all layer-3+ nodes for contradictions. "
             "Complements the watcher-driven detection by running the same "
             "detector over the full corpus — useful after reindex or "
             "periodic consistency checks.",
    )
    p_contra.add_argument("--overlap-threshold", dest="overlap_threshold",
                          type=float, default=0.35,
                          help="Min token-jaccard overlap for a candidate "
                               "pair to be considered (default 0.35)")
    p_contra.add_argument("--candidate-limit", dest="candidate_limit",
                          type=int, default=10,
                          help="Max fulltext candidates to compare per node "
                               "(default 10)")

    # Session ingestion (Apr 17)
    p_ingest = sub.add_parser("ingest-session",
                              help="Ingest a Claude Code session jsonl")
    p_ingest.add_argument("jsonl_path", help="Path to session .jsonl transcript")
    p_ingest.add_argument("--dir", default=None, help="MEM_HOME override")

    # Dream briefing (Apr 17)
    p_dream = sub.add_parser("dream-briefing",
                             help="Emit NDJSON consolidation candidates")
    p_dream.add_argument("--dir", default=None, help="MEM_HOME override")
    p_dream.add_argument("--orphan-days", type=int, default=30,
                         help="Min age (days) for orphan candidates")
    p_dream.add_argument("--bloat-lines", type=int, default=500,
                         help="Line threshold for bloated-file flag")
    p_dream.add_argument("--bloat-bytes", type=int, default=10240,
                         help="Byte threshold for bloated-file flag")
    p_dream.add_argument("--dead-weight-days", type=int, default=60,
                         dest="dead_weight_days",
                         help="Dead-weight age threshold in days "
                              "(default 60). Layer>=3 nodes with no "
                              "retrieval in this window become "
                              "dead_weight candidates.")
    p_dream.add_argument("--dead-weight-min-layer", type=int, default=3,
                         dest="dead_weight_min_layer",
                         help="Minimum layer for dead_weight candidates "
                              "(default 3).")
    p_dream.add_argument("--content-link-limit", type=int, default=50,
                         dest="content_link_limit",
                         help="Emission cap for find_content_similar_unlinked "
                              "'link' candidates (default 50, historical). "
                              "Measured 2026-08-21: cost is dominated by the "
                              "fixed content-similarity pool scan, not this "
                              "limit (28.5s@50 / 31.1s@300 / 45.0s@2000 on "
                              "the live corpus) -- callers chasing "
                              "orphans_remaining down should raise this well "
                              "above 50.")

    # Dream-pass output logging (Apr 19 — close the loop on nightly consolidation)
    p_dlog = sub.add_parser(
        "dream-log",
        help="Record / query a dream-pass run. "
             "Modes: start | action | finish | show | rebuild.",
    )
    p_dlog.add_argument("mode",
                        choices=["start", "action", "finish", "show",
                                 "rebuild"],
                        help="start: open a run; action: record one action; "
                             "finish: close the run; show: dump one run; "
                             "rebuild: replay JSONL → Neo4j.")
    p_dlog.add_argument("run_id", nargs="?", default=None,
                        help="Run identifier (typically ISO timestamp). "
                             "Required for all modes except 'rebuild'.")
    p_dlog.add_argument("--candidates-json", dest="candidates_json",
                        default=None,
                        help="[start] JSON object mapping candidate_type → "
                             "count, e.g. '{\"orphan\": 5, \"merge\": 3}'.")
    p_dlog.add_argument("--action", dest="action_kind", default=None,
                        choices=["merge", "split", "link", "defer", "ignore",
                                 "archive", "other"],
                        help="[action] Action taken on the candidate.")
    p_dlog.add_argument("--candidate-type", dest="candidate_type",
                        default=None,
                        help="[action] The dream-briefing candidate_type "
                             "(orphan|merge|split|link|stale|dead_weight|"
                             "index|...) being actioned.")
    p_dlog.add_argument("--nodes", nargs="*", default=None,
                        help="[action] Paths of the nodes involved. For "
                             "merge/link: two nodes. For split/defer/etc: one.")
    p_dlog.add_argument("--claim-id", dest="claim_id", default=None,
                        help="[action] Calibration-ledger claim_id cross-"
                             "reference, if the action produced a claim.")
    p_dlog.add_argument("--note", default=None,
                        help="[action] Free-text note.")
    p_dlog.add_argument("--status-delta-json", dest="status_delta_json",
                        default=None,
                        help="[finish] JSON object describing post-pass state "
                             "change (e.g. '{\"nodes_before\":197,"
                             "\"nodes_after\":192}').")
    p_dlog.add_argument("--dir", default=None, help="MEM_HOME override")

    p_drep = sub.add_parser(
        "dream-report",
        help="Summary across recent dream passes: candidate volume, action "
             "rate, recurring-ignored candidates.",
    )
    p_drep.add_argument("--window", default="7d", type=_parse_window,
                        help="Window in days (default 7d).")
    p_drep.add_argument("--recurrence-threshold", dest="recurrence_threshold",
                        type=int, default=3,
                        help="Minimum distinct-run count for a candidate to "
                             "qualify as 'recurring ignored' (default 3). "
                             "A candidate counts only if its most-recent "
                             "action was defer/ignore.")
    p_drep.add_argument("--dir", default=None, help="MEM_HOME override")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "init": cmd_init,
        "grep": cmd_grep,
        "ls": cmd_ls,
        "status": cmd_status,
        "watch": cmd_watch,
        "skills": cmd_skills,
        "_decay": cmd_decay,
        "reindex": cmd_reindex,
        "check-indexes": cmd_check_indexes,
        "claim": cmd_claim,
        "verify": cmd_verify,
        "calibration": cmd_calibration,
        "freshness-scan": cmd_freshness_scan,
        "access-report": cmd_access_report,
        "contradictions-scan": cmd_contradictions_scan,
        "ingest-session": cmd_ingest_session,
        "dream-briefing": cmd_dream_briefing,
        "link-suggest": cmd_link_suggest,
        "link-apply": cmd_link_apply,
        "dream-log": cmd_dream_log,
        "dream-report": cmd_dream_report,
    }

    try:
        commands[args.command](args)
    except SystemExit:
        raise
    except Exception as e:
        err({"error": str(e), "type": type(e).__name__})
        sys.exit(2)


if __name__ == "__main__":
    main()
