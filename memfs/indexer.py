"""Indexer — scan directories, index files, maintain edges.

Neo4j-backed. Takes a Graph object (from memfs.graph.connect()).
"""

import json
import os
import sys
from fnmatch import fnmatch

from memfs.parser import parse_file, compute_hash
from memfs.paths import resolve_link
from memfs import graph as graph_mod

# Always ignored
HARDCODED_IGNORES = [".mem", ".mem/*", ".git", ".git/*", "node_modules",
                     "node_modules/*", "*.log", "*.tmp"]


def load_memignore(mem_home: str) -> list[str]:
    """Load .memignore patterns from MEM_HOME root."""
    patterns = list(HARDCODED_IGNORES)
    ignore_path = os.path.join(mem_home, ".memignore")
    if os.path.exists(ignore_path):
        with open(ignore_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line.rstrip("/"))
                    patterns.append(line.rstrip("/") + "/*")
    return patterns


def is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """Check if a relative path matches any ignore pattern."""
    for pat in patterns:
        if fnmatch(rel_path, pat):
            return True
        # Check each path component
        parts = rel_path.split(os.sep)
        for part in parts:
            if fnmatch(part, pat):
                return True
    return False


def _validate_layer_provenance(parsed: dict) -> tuple[bool, str | None]:
    """Validate frontmatter layer/source.

    Returns (ok, error_message). Layer must be 1-5. Layer >= 3 requires
    a non-empty `source` string.
    """
    fm = parsed.get("frontmatter") or {}
    layer = fm.get("layer")
    if layer is None:
        return True, None  # Default layer applies; no validation
    try:
        layer_int = int(layer)
    except (TypeError, ValueError):
        return False, f"invalid layer {layer!r}: must be integer 1-5"
    if layer_int < 1 or layer_int > 5:
        return False, f"invalid layer {layer_int}: must be in range 1-5"
    if layer_int >= 3:
        source = fm.get("source")
        if not source or not isinstance(source, str) or not source.strip():
            return False, (
                f"layer {layer_int} requires a non-empty `source` field "
                "in frontmatter (provenance)"
            )
    return True, None


def index_file(graph, mem_home: str, rel_path: str) -> bool:
    """Index a single file into the graph. Returns True if indexed, False if
    quarantined due to validation failure.
    """
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.exists(abs_path):
        return False

    parsed = parse_file(abs_path)
    fm = parsed.get("frontmatter") or {}

    ok, err_msg = _validate_layer_provenance(parsed)
    if not ok:
        # Emit NDJSON error to stderr and refuse to index
        print(
            json.dumps({
                "event": "quarantine",
                "path": rel_path,
                "error": err_msg,
            }),
            file=sys.stderr,
            flush=True,
        )
        return False

    # Layer + source + freshness fields
    layer = int(fm["layer"]) if fm.get("layer") is not None else 2
    source = fm.get("source")
    if source is not None:
        source = str(source)

    # Freshness
    fv = fm.get("freshness_verified_at")
    if fv is not None:
        fv = str(fv)
    fs = fm.get("freshness_source_url")
    if fs is not None:
        fs = str(fs)
    fd = fm.get("freshness_stale_after_days")
    if fd is not None:
        try:
            fd = int(fd)
        except (TypeError, ValueError):
            fd = None

    # Upsert node
    graph_mod.upsert_node(
        graph,
        rel_path,
        title=parsed["title"],
        content_hash=parsed["content_hash"],
        date_hint=parsed["date_hint"],
        description=parsed.get("description"),
        content=parsed["content"],
        layer=layer,
        source=source,
        freshness_verified_at=fv,
        freshness_source_url=fs,
        freshness_stale_after_days=fd,
    )

    # Process links — clear old link edges, re-add
    graph_mod.clear_link_edges_from(graph, rel_path)
    for link_target in parsed["links"]:
        resolved = resolve_link(link_target, rel_path, mem_home)
        target_exists = os.path.exists(os.path.join(mem_home, resolved))
        strength = 1.0 if target_exists else 0.0
        graph_mod.upsert_link_edge(graph, rel_path, resolved, strength=strength)

    return True


def update_node(graph, mem_home: str, rel_path: str) -> bool:
    """Update a node if its content has changed. Returns True if updated."""
    abs_path = os.path.join(mem_home, rel_path)
    if not os.path.exists(abs_path):
        return False

    with open(abs_path, encoding="utf-8") as f:
        current_hash = compute_hash(f.read())

    existing = graph_mod.get_node(graph, rel_path)
    if existing and existing.get("content_hash") == current_hash:
        return False  # No change

    return index_file(graph, mem_home, rel_path)


def index_directory(graph, mem_home: str) -> int:
    """Index all .md/.jsonl files in a directory tree. Returns count indexed."""
    patterns = load_memignore(mem_home)
    count = 0

    for dirpath, dirnames, filenames in os.walk(mem_home):
        rel_dir = os.path.relpath(dirpath, mem_home)
        if rel_dir != "." and is_ignored(rel_dir, patterns):
            dirnames.clear()
            continue

        dirnames[:] = [
            d for d in dirnames
            if not is_ignored(
                os.path.relpath(os.path.join(dirpath, d), mem_home), patterns
            )
        ]

        for filename in filenames:
            if not (filename.endswith(".md") or filename.endswith(".jsonl")):
                continue
            rel_path = os.path.relpath(os.path.join(dirpath, filename), mem_home)
            if is_ignored(rel_path, patterns):
                continue
            if index_file(graph, mem_home, rel_path):
                count += 1

    return count


def reindex(graph, mem_home: str) -> int:
    """Drop all node/query/edge data and reindex from scratch. Preserves Claims + Meta."""
    graph_mod.clear_data(graph)
    return index_directory(graph, mem_home)


def remove_file(graph, rel_path: str) -> None:
    """Remove a file from the index."""
    graph_mod.remove_node(graph, rel_path)


def rename_path(graph, old_prefix: str, new_prefix: str) -> None:
    """Update all paths matching old_prefix to new_prefix (for directory renames)."""
    graph_mod.rename_prefix(graph, old_prefix, new_prefix)


def upgrade_broken_links(graph, rel_path: str) -> int:
    """When a file is created, upgrade any broken link edges pointing to it."""
    return graph_mod.upgrade_broken_links(graph, rel_path)
