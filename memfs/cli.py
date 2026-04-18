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
        if subdir:
            subdir = subdir.rstrip("/")
            rows = graph.run(
                "MATCH (n:Node) WHERE n.path STARTS WITH $prefix "
                "RETURN n.path AS path, n.title AS title, n.layer AS layer "
                "ORDER BY n.path",
                prefix=subdir + "/",
            )
        else:
            rows = graph.run(
                "MATCH (n:Node) RETURN n.path AS path, n.title AS title, n.layer AS layer "
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


def cmd_status(args):
    graph = _connect_or_die()
    try:
        nodes = count_nodes(graph)
        link_edges = count_edges(graph, type="link")
        search_edges = count_edges(graph, type="search")
        queries = count_queries(graph)
        last_index = get_meta(graph, "last_index")
        last_decay = get_meta(graph, "last_decay")
    finally:
        graph.close()

    out({
        "nodes": nodes,
        "edges": {"link": link_edges, "search": search_edges},
        "queries": queries,
        "last_index": last_index,
        "last_decay": last_decay,
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


def cmd_reindex(args):
    mem_home = get_mem_home(args)
    graph = _connect_or_die()
    try:
        count = do_reindex(graph, mem_home)
        edges = count_edges(graph)
    finally:
        graph.close()

    out({"action": "reindex", "nodes": count, "edges": edges})


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
    from memfs.calibration import calibration_curve, rebuild_from_ledger
    graph = _connect_or_die()
    try:
        rebuild_stats = None
        if getattr(args, "rebuild", False):
            rebuild_stats = rebuild_from_ledger(
                graph, mem_home=get_mem_home(args),
            )
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
    """
    from memfs.dream import find_content_similar_unlinked
    graph = _connect_or_die()
    try:
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
    from memfs.graph import upsert_link_edge
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
                upsert_link_edge(graph, a, b, strength=args.strength,
                                 source=edge_source)
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
            upsert_link_edge(graph, args.source, args.target,
                             strength=args.strength,
                             source=args.link_source)
            applied += 1
            out({"applied": [args.source, args.target],
                 "strength": args.strength,
                 "source": args.link_source})
    finally:
        graph.close()
    # Summary goes to stderr so stdout stays NDJSON-clean for piping
    err({"summary": {"applied": applied, "skipped": skipped, "errors": errors}})


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

    # Freshness (M5)
    sub.add_parser("freshness-scan", help="Report nodes with stale freshness stamps")

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
        "claim": cmd_claim,
        "verify": cmd_verify,
        "calibration": cmd_calibration,
        "freshness-scan": cmd_freshness_scan,
        "ingest-session": cmd_ingest_session,
        "dream-briefing": cmd_dream_briefing,
        "link-suggest": cmd_link_suggest,
        "link-apply": cmd_link_apply,
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
