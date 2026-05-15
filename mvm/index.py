"""
mvm-index: walk the knowledge/ tree, parse markdown, build the graph and FTS index.

Outputs:
  state/index.db   — sqlite with files table (metadata + frontmatter) + FTS5 over content
  state/graph.db   — sqlite with edges table (markdown links + frontmatter refs)

v0 uses SQLite FTS5 for text search (BM25-ranked). v0.1 will add a vector
column via sqlite-vec. The schema reserves space for embeddings.

Usage:
  mvm-index                          # index ~/mvm/knowledge by default
  mvm-index --root /path/to/kb       # custom root
  mvm-index --root . --state ./state
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

    for key in ("see_also", "prereq", "superseded_by", "episodic_source"):
        val = fm.get(key)
        if val is None:
            continue
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, str):
                edges.append((src_rel, item, f"frontmatter_{key}"))
    return edges


def init_db(state: Path) -> tuple[sqlite3.Connection, sqlite3.Connection]:
    state.mkdir(parents=True, exist_ok=True)
    idx = sqlite3.connect(state / "index.db")
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

    g = sqlite3.connect(state / "graph.db")
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
    idx = sqlite3.connect(state / "index.db")
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
        (d / "INDEX.md").write_text("\n".join(lines) + "\n")
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
    idx.execute("DELETE FROM files")
    idx.execute("DELETE FROM files_fts")
    idx.execute("DELETE FROM files_vec")
    g.execute("DELETE FROM edges")

    md_files = [p for p in args.root.rglob("*.md") if not p.name.endswith(".tests.md")]
    n_files = 0
    n_edges = 0
    t0 = time.time()

    # Phase 1: parse all files + insert FTS rows + collect graph edges
    parsed = []  # list of (rel, body, fm) for embedding pass
    for md in md_files:
        fm, body = parse_markdown(md)
        rel = str(md.relative_to(args.root))
        kind = fm.get("kind", "")
        source = fm.get("source", "")
        ingested_at = fm.get("ingested_at", "")
        last_modified_at = fm.get("last_modified_at", "")
        mtime = md.stat().st_mtime
        n_tests = count_tests(md)

        idx.execute(
            "INSERT INTO files (path, kind, source, ingested_at, last_modified_at, mtime, n_tests, frontmatter) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rel, str(kind), str(source), str(ingested_at), str(last_modified_at), mtime, n_tests, yaml.safe_dump(fm)),
        )
        idx.execute("INSERT INTO files_fts (path, body) VALUES (?, ?)", (rel, body))

        edges = extract_edges(md, body, fm, args.root)
        for src, dst, etype in edges:
            try:
                g.execute(
                    "INSERT OR IGNORE INTO edges (src, dst, edge_type) VALUES (?, ?, ?)",
                    (src, dst, etype),
                )
                n_edges += 1
            except sqlite3.Error:
                pass
        parsed.append((rel, body))
        n_files += 1

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
        print(f"indexed {n_files} files, {n_edges} edges, regenerated {n_indexes} INDEX.md in {dt:.2f}s")
        print(f"  index.db: {args.state / 'index.db'}")
        print(f"  graph.db: {args.state / 'graph.db'}")

    return 0


