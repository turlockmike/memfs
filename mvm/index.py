"""
mvm-index: walk the knowledge/ tree, parse markdown, build the graph and FTS index.

Outputs:
  state/index.db   — sqlite with files table (metadata + frontmatter) + FTS5 over content
  state/graph.db   — sqlite with edges table (markdown links + frontmatter refs)

v0 uses SQLite FTS5 for text search (BM25-ranked). v0.1 will add a vector
column via sqlite-vec. The schema reserves space for embeddings.

Usage:
  mvm-index                          # INCREMENTAL: only added/changed/deleted docs (default)
  mvm-index --full                   # full rebuild: wipe + reindex + re-embed everything
  mvm-index --root /path/to/kb       # custom root
  mvm-index --root . --state ./state

Incremental (default since 2026-06-10): diffs disk mtimes against the
files.mtime manifest (max of .md and its .tests.yaml), touches only the
delta. A zero-delta run never loads the embedding model — sub-second,
~30MB RSS, vs ~10min/2.5GB for a full re-embed (the OOM-storm suspect
that motivated this).
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import struct
import sys
import time
from pathlib import Path

import yaml

# Vector embeddings via fastembed (ONNX, no torch). Model: BAAI/bge-small-en-v1.5 (384-dim).
# Loaded lazily — first index run downloads ~33MB model to ~/.cache/.
_EMBED_MODEL = None


def _embed_model():
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        from fastembed import TextEmbedding
        _EMBED_MODEL = TextEmbedding()
    return _EMBED_MODEL


def _embed_to_blob(text: str) -> bytes:
    """Embed a single text. Prefer _embed_batch when many docs."""
    emb = next(iter(_embed_model().embed([text])))
    return struct.pack(f"{len(emb)}f", *emb)


def _embed_batch(texts: list[str]) -> list[bytes]:
    """Batch-embed many texts. ~10× faster than one-at-a-time."""
    embs = list(_embed_model().embed(texts))
    return [struct.pack(f"{len(e)}f", *e) for e in embs]

DEFAULT_ROOT = Path(os.environ.get("MVM_KNOWLEDGE", str(Path.home() / "mvm" / "knowledge")))
DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n(.*)", re.DOTALL)
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Frontmatter relation keys that emit typed `frontmatter_<key>` edges. Widened
# S721 to include the forward/peer relations supersedes / in_tension_with /
# derived_from (Gap 1). Edge of any of these types is consumed by `mvm relations`.
RELATION_KEYS = (
    "see_also", "prereq", "superseded_by", "episodic_source",
    "supersedes", "in_tension_with", "derived_from",
)


def normalize_relation_target(item: str, root: Path) -> str:
    """Normalize a frontmatter relation target to knowledge-root-relative form.

    Authors habitually write `~/resources/X.md` (the mirror path) or
    `~/mvm/knowledge/resources/X.md`; both refer to the node stored as
    `resources/X.md` (since ~/resources <-> ~/mvm/knowledge/resources is mirrored
    and root = ~/mvm/knowledge). Without this, the edge dst never matches a node
    and BFS can't traverse it (the S720 dangling-edge data bug). URLs and
    already-relative targets pass through unchanged.
    """
    if URL_RE.match(item):
        return item
    expanded = Path(os.path.expanduser(item))
    if not expanded.is_absolute():
        return item  # already relative — preserve verbatim
    # root/resources/X  and  ~/resources/X (mirror) both -> resources/X
    for base in (root, Path.home()):
        try:
            return str(expanded.relative_to(base))
        except ValueError:
            continue
    return item


def parse_markdown(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body)."""
    text = path.read_text(errors="replace")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, m.group(2)


def extract_edges(src_path: Path, body: str, fm: dict, root: Path) -> list[tuple[str, str, str]]:
    """Return list of (src, dst, edge_type). dst can be a path or URL."""
    edges: list[tuple[str, str, str]] = []
    src_rel = str(src_path.relative_to(root))

    for _, target in MD_LINK_RE.findall(body):
        target = target.strip()
        if URL_RE.match(target):
            edges.append((src_rel, target, "external_url"))
        elif target.startswith("/") or target.startswith("#"):
            continue
        else:
            resolved = (src_path.parent / target).resolve()
            try:
                dst_rel = str(resolved.relative_to(root))
                edges.append((src_rel, dst_rel, "md_link"))
            except ValueError:
                edges.append((src_rel, str(resolved), "external_path"))

    for target in WIKILINK_RE.findall(body):
        edges.append((src_rel, target.strip(), "wikilink"))

    src_fm = fm.get("source")
    if isinstance(src_fm, str):
        edge_type = "source_url" if URL_RE.match(src_fm) else "source_ref"
        edges.append((src_rel, src_fm, edge_type))

    for key in RELATION_KEYS:
        val = fm.get(key)
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, str):
                dst = normalize_relation_target(item, root)
                edges.append((src_rel, dst, f"frontmatter_{key}"))
    return edges


def init_db(state: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    state.mkdir(parents=True, exist_ok=True)
    idx = sqlite3.connect(state / "index.db", timeout=30.0)
    # Load sqlite-vec extension for vector search
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)

    idx.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            kind TEXT,
            source TEXT,
            ingested_at TEXT,
            last_modified_at TEXT,
            mtime REAL,
            n_tests INTEGER DEFAULT 0,
            frontmatter TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            path UNINDEXED, body, tokenize='porter unicode61'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS files_vec USING vec0(
            path TEXT PRIMARY KEY, embedding float[384]
        );
    """)

    g = sqlite3.connect(state / "graph.db", timeout=30.0)
    g.executescript("""
        CREATE TABLE IF NOT EXISTS edges (
            src TEXT NOT NULL,
            dst TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            PRIMARY KEY (src, dst, edge_type)
        );
        CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src);
        CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst);
        CREATE INDEX IF NOT EXISTS idx_edges_type ON edges (edge_type);
    """)
    return idx, g


def _doc_mtime(md: Path) -> float:
    """Change-detection mtime for a doc unit: max of the .md and its
    .tests.yaml — n_tests derives from the yaml, so a tests-only change
    must mark the doc dirty or n_tests goes stale until the next --full."""
    m = md.stat().st_mtime
    t = md.with_suffix(".tests.yaml")
    if t.exists():
        m = max(m, t.stat().st_mtime)
    return m


def index_doc(idx: sqlite3.Connection, g: sqlite3.Connection,
              md: Path, root: Path) -> tuple[str, str, int]:
    """Parse one md file and insert its files + files_fts rows and edges.
    Caller is responsible for having deleted stale rows first (incremental)
    or wiped the tables (full). Returns (rel, body, n_edges)."""
    fm, body = parse_markdown(md)
    rel = str(md.relative_to(root))
    idx.execute(
        "INSERT INTO files (path, kind, source, ingested_at, last_modified_at, mtime, n_tests, frontmatter) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (rel, str(fm.get("kind", "")), str(fm.get("source", "")),
         str(fm.get("ingested_at", "")), str(fm.get("last_modified_at", "")),
         _doc_mtime(md), count_tests(md), yaml.safe_dump(fm)),
    )
    idx.execute("INSERT INTO files_fts (path, body) VALUES (?, ?)", (rel, body))
    n_edges = 0
    for src, dst, etype in extract_edges(md, body, fm, root):
        try:
            g.execute("INSERT OR IGNORE INTO edges (src, dst, edge_type) VALUES (?, ?, ?)",
                      (src, dst, etype))
            n_edges += 1
        except sqlite3.Error:
            pass
    return rel, body, n_edges


def count_tests(md_path: Path) -> int:
    tests_path = md_path.with_suffix(".tests.yaml")
    if not tests_path.exists():
        return 0
    try:
        data = yaml.safe_load(tests_path.read_text()) or []
        return len(data) if isinstance(data, list) else 0
    except yaml.YAMLError:
        return 0


def _embed_only_main(args) -> int:
    """Backfill embeddings on existing index.db files. Idempotent — only embeds docs
    not already in files_vec. Doesn't touch FTS or graph."""
    state = args.state
    idx = sqlite3.connect(state / "index.db", timeout=30.0)
    idx.enable_load_extension(True)
    import sqlite_vec
    sqlite_vec.load(idx)
    idx.enable_load_extension(False)

    cur = idx.cursor()
    # Files in index but not yet embedded
    cur.execute("""
        SELECT f.path FROM files f
        LEFT JOIN files_vec v ON v.path = f.path
        WHERE v.path IS NULL
    """)
    pending = [row[0] for row in cur.fetchall()]
    total = len(pending)
    if total == 0:
        if not args.quiet:
            print("All files already embedded. Nothing to do.")
        idx.close()
        return 0
    if not args.quiet:
        print(f"Backfilling embeddings for {total} files...", flush=True)

    BATCH = 64
    done = 0
    for i in range(0, total, BATCH):
        chunk_paths = pending[i:i + BATCH]
        bodies = []
        valid = []
        for rel in chunk_paths:
            md_path = args.root / rel
            if not md_path.exists():
                continue
            _, body = parse_markdown(md_path)
            bodies.append(body)
            valid.append(rel)
        if not bodies:
            continue
        try:
            blobs = _embed_batch(bodies)
        except Exception as e:
            print(f"  warn: batch embed failed at offset {i}: {e}", file=sys.stderr)
            continue
        for rel, blob in zip(valid, blobs):
            try:
                idx.execute("INSERT INTO files_vec (path, embedding) VALUES (?, ?)", (rel, blob))
            except Exception as e:
                print(f"  warn: insert embed failed for {rel}: {e}", file=sys.stderr)
        idx.commit()
        done = min(i + BATCH, total)
        if not args.quiet:
            print(f"  {done}/{total}", flush=True)

    idx.close()
    if not args.quiet:
        print(f"Backfill complete: {done}/{total}.")
    return 0


def regenerate_folder_indexes(root: Path) -> int:
    """Auto-generate <folder>/INDEX.md from children's frontmatter summaries.

    INDEX.md is a DERIVED view; it's rewritten every index run. Don't hand-edit.
    Returns count of INDEX.md files written.
    """
    written = 0
    # Walk every directory (except hidden ones), collect canonicals' summaries
    seen_dirs: set[Path] = set()
    for md in root.rglob("*.md"):
        if md.name == "INDEX.md" or md.name.endswith(".tests.md"):
            continue
        seen_dirs.add(md.parent)

    for d in sorted(seen_dirs):
        children = sorted(p for p in d.glob("*.md")
                          if p.name != "INDEX.md" and not p.name.endswith(".tests.md"))
        if not children:
            continue
        rel_dir = d.relative_to(root) if d != root else Path(".")
        lines = [f"# {rel_dir} — Index", "",
                 "Auto-generated by `mvm index`. Do not hand-edit.", ""]
        for child in children:
            fm, _ = parse_markdown(child)
            summary = fm.get("summary", "").strip()
            kind = fm.get("kind", "")
            tag = f" [{kind}]" if kind else ""
            line = f"- **[{child.stem}]({child.name})**{tag}"
            if summary:
                line += f" — {summary}"
            lines.append(line)
        # Subdirectories
        subs = sorted(p for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))
        if subs:
            lines.append("")
            lines.append("## Subfolders")
            for sub in subs:
                lines.append(f"- [{sub.name}/]({sub.name}/)")
        content = "\n".join(lines) + "\n"
        out = d / "INDEX.md"
        # Write-if-changed: an unconditional rewrite bumps mtime every run,
        # which would make every INDEX.md look dirty to the incremental
        # delta detector and trigger pointless re-embeds.
        if out.exists() and out.read_text() == content:
            continue
        out.write_text(content)
        written += 1
    return written


def main(argv = None) -> int:
    parser = argparse.ArgumentParser(description="Build the mvm graph + FTS index.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"Knowledge root (default: {DEFAULT_ROOT}).")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE,
                        help=f"State directory (default: {DEFAULT_STATE}).")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-embed", action="store_true",
                        help="Skip vector embeddings (FTS+graph only). Useful for bulk migration; backfill later.")
    parser.add_argument("--embed-only", action="store_true",
                        help="Backfill embeddings on existing files without rebuilding FTS/graph. Idempotent.")
    parser.add_argument("--full", action="store_true",
                        help="Force a full rebuild (wipe + reindex + re-embed the entire corpus, "
                             "~minutes + ~2.5GB RAM). Default is incremental: only added/changed/"
                             "deleted docs are touched (seconds; embedding model not even loaded "
                             "on a zero-delta run).")
    args = parser.parse_args(argv)

    # Backfill-only path: read existing files table, embed docs that don't have embeddings yet.
    if args.embed_only:
        return _embed_only_main(args)

    if not args.root.exists():
        print(f"ERROR: root not found: {args.root}", file=sys.stderr)
        return 2

    # Single-writer lock — `mvm index` is not concurrent-safe (full rebuild deletes rows).
    # Second invocation refuses immediately rather than corrupting the index.
    args.state.mkdir(parents=True, exist_ok=True)
    lock_path = args.state / ".index.lock"
    import fcntl
    lock_fp = open(lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"ERROR: another `mvm index` is already running (lock at {lock_path}). "
              f"Wait for it to finish, or remove the lock if stale.", file=sys.stderr)
        return 3

    # Regenerate folder INDEX.md views from children's summary frontmatter
    # before populating the search index — INDEX.md will be indexed with the rest.
    n_indexes = regenerate_folder_indexes(args.root)

    idx, g = init_db(args.state)
    md_files = [p for p in args.root.rglob("*.md") if not p.name.endswith(".tests.md")]
    t0 = time.time()

    # Mode selection: incremental by default (delta vs the files.mtime manifest);
    # --full forces the old wipe-everything path; an empty files table degrades
    # to full automatically (nothing to diff against).
    existing = dict(idx.execute("SELECT path, mtime FROM files").fetchall())
    full = args.full or not existing
    n_deleted = 0
    unembedded = []  # docs present but missing a vec row (incremental self-heal)

    if full:
        idx.execute("DELETE FROM files")
        idx.execute("DELETE FROM files_fts")
        # Preserve files_vec on --no-embed rebuilds. Wiping it with no embed
        # phase to repopulate left the vector index permanently empty, silently
        # demoting recall to FTS-only (search.py is vector-first). Only wipe
        # when we will actually re-embed. Orphan rows are harmless — vec_search
        # joins files_vec→files, so they never surface.
        if not args.no_embed:
            idx.execute("DELETE FROM files_vec")
        g.execute("DELETE FROM edges")
        to_index = md_files
    else:
        disk = {str(p.relative_to(args.root)): p for p in md_files}
        added = [r for r in disk if r not in existing]
        changed = [r for r, p in disk.items()
                   if r in existing and _doc_mtime(p) != existing[r]]
        deleted = [r for r in existing if r not in disk]
        # Self-heal embedding ghosts: a doc whose embed batch silently failed
        # once lands in files/FTS with current mtime but NO files_vec row. The
        # mtime diff above never flags it again, so plain `mvm index` would
        # report "up to date" while the doc stays unsearchable until a manual
        # --embed-only/--full. Treat missing-embedding as dirty-for-embed (same
        # criterion as _embed_only_main) — but embed-only, no FTS/graph rework.
        if not args.no_embed:
            dirty = set(added) | set(changed)
            unembedded = [r for (r,) in idx.execute(
                "SELECT f.path FROM files f "
                "LEFT JOIN files_vec v ON v.path = f.path "
                "WHERE v.path IS NULL").fetchall()
                if r in disk and r not in dirty]
        if not added and not changed and not deleted and not unembedded:
            idx.close()
            g.close()
            if not args.quiet:
                print(f"index up to date ({len(existing)} files, "
                      f"{time.time() - t0:.2f}s) — nothing to do")
            return 0
        for rel in deleted:
            idx.execute("DELETE FROM files WHERE path = ?", (rel,))
            idx.execute("DELETE FROM files_fts WHERE path = ?", (rel,))
            idx.execute("DELETE FROM files_vec WHERE path = ?", (rel,))
            g.execute("DELETE FROM edges WHERE src = ?", (rel,))
        for rel in changed:
            idx.execute("DELETE FROM files WHERE path = ?", (rel,))
            idx.execute("DELETE FROM files_fts WHERE path = ?", (rel,))
            g.execute("DELETE FROM edges WHERE src = ?", (rel,))
            # The stale vec row is NOT deleted here. Embed mode replaces it
            # atomically in Phase 2 (delete-just-before-insert), so a failed
            # batch embed leaves the old embedding in place — stale-but-present
            # ranks roughly right; missing drops the doc from vector search.
            # --no-embed keeps it for the same reason.
        n_deleted = len(deleted)
        to_index = [disk[r] for r in added + changed]

    # Phase 1: parse dirty files + insert FTS rows + graph edges
    n_files = 0
    n_edges = 0
    parsed = []  # list of (rel, body) for embedding pass
    for md in to_index:
        rel, body, ne = index_doc(idx, g, md, args.root)
        n_edges += ne
        parsed.append((rel, body))
        n_files += 1

    # Ghost backfill: embed docs present-but-unembedded WITHOUT re-touching
    # their FTS/graph rows (Phase 1 left them intact). Phase 2's
    # delete-just-before-insert upserts the vec row (the prior DELETE is a
    # no-op since these have no vec row — that's why they're here).
    for rel in unembedded:
        md = args.root / rel
        if md.exists():
            _, body = parse_markdown(md)
            parsed.append((rel, body))

    # Phase 2: batch-embed (skipped if --no-embed). ~10× faster than per-doc.
    if not args.no_embed and parsed:
        if not args.quiet:
            print(f"  embedding {len(parsed)} docs in batches...", flush=True)
        BATCH = 64
        for i in range(0, len(parsed), BATCH):
            chunk = parsed[i:i + BATCH]
            try:
                blobs = _embed_batch([body for _, body in chunk])
            except Exception as e:
                print(f"  warn: batch embed failed at offset {i}: {e}", file=sys.stderr)
                continue
            for (rel, _), blob in zip(chunk, blobs):
                try:
                    # Delete-just-before-insert: replaces a changed doc's old
                    # embedding only once its new one is in hand (vec0 has no
                    # ON CONFLICT, so this is the upsert). No-op in full mode
                    # (table wiped) and for added docs (no prior row).
                    idx.execute("DELETE FROM files_vec WHERE path = ?", (rel,))
                    idx.execute(
                        "INSERT INTO files_vec (path, embedding) VALUES (?, ?)",
                        (rel, blob),
                    )
                except Exception as e:
                    print(f"  warn: insert embed failed for {rel}: {e}", file=sys.stderr)
            if not args.quiet:
                print(f"    {min(i + BATCH, len(parsed))}/{len(parsed)}", flush=True)

    idx.commit()
    g.commit()
    idx.close()
    g.close()

    if not args.quiet:
        dt = time.time() - t0
        mode = "full" if full else "incremental"
        extra = f", removed {n_deleted}" if n_deleted else ""
        print(f"[{mode}] indexed {n_files} files, {n_edges} edges{extra}, "
              f"regenerated {n_indexes} INDEX.md in {dt:.2f}s")
        print(f"  index.db: {args.state / 'index.db'}")
        print(f"  graph.db: {args.state / 'graph.db'}")

    return 0


