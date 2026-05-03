"""Index rendering — write per-directory `index.md` files derived from the graph.

memfs is the holistic memory system. Indexes are a first-class output surface,
not a peripheral renderer. This module is the implementation of that
responsibility: given a directory, query the graph for files in it and render
a markdown table-of-contents to `<dir>/index.md`.

Handcrafted indexes (containing the `<!-- handcrafted -->` marker in the body)
are detected via the `is_handcrafted` Node property and never overwritten by
this module. Drift detection still tracks them: if a handcrafted index gets
out of sync with directory contents, that's a signal for the auditor — but
memfs does not modify the file.

Design (proposal at ~/projects/memfs-mkindex-unification/PROPOSAL.md):

- The watcher is the primary integration point: when a markdown-bearing
  directory changes, the watcher debounces and calls `write_index_if_drifted`
  for that directory.
- `render_index_for_dir(graph, mem_home, rel_dir)` is the pure-function core —
  query graph, format markdown.
- `check_drift_for_dir` returns drift findings without writing anything.
- `render_all(graph, mem_home)` is the bulk path called from `memfs reindex`.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Iterator

from memfs import graph as graph_mod


HANDCRAFTED_MARKER = "<!-- handcrafted -->"


# ─────────────────────────────────────────────────────────────────────────────
# Querying the graph for what's in a directory
# ─────────────────────────────────────────────────────────────────────────────


def _list_immediate_children(
    graph: graph_mod.Graph, rel_dir: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> tuple[list[dict], set[str]]:
    """Return (files_in_dir, subdirs_with_indexed_descendants) within a single root.

    Files: list of node dicts (path, title, description, is_handcrafted).
    Subdirs: set of immediate subdirectory names that contain at least one
    indexed file at any depth.
    """
    # Normalize: rel_dir == "" means root; otherwise must end without trailing slash.
    rel_dir = rel_dir.strip("/")
    prefix = "" if rel_dir in ("", ".") else rel_dir + "/"

    # Pull all nodes whose path starts with this prefix WITHIN this root.
    # Without root_id scoping, multi-root indexes would mix content from
    # different roots that happen to share path prefixes.
    #
    # ``content_hash IS NOT NULL`` filters out :Node stubs synthesized by
    # ``upsert_link_edge`` for broken wikilink targets. Without this filter
    # an auto-rendered index.md could list bogus entries like
    # ``../../areas/USER.md`` or ``/home/mike/journal.md`` (cross-root or
    # never-existed paths) — which then triggered "INDEX REFERENCES MISSING
    # FILE" findings in a chain reaction. n=2 in verify-detector-premise.
    rows = graph.run(
        "MATCH (n:Node) WHERE n.root_id = $root_id AND n.path STARTS WITH $prefix "
        "AND n.content_hash IS NOT NULL "
        "RETURN n.path AS path, n.title AS title, n.description AS description, "
        "n.is_handcrafted AS is_handcrafted ORDER BY n.path",
        root_id=root_id, prefix=prefix,
    )

    files: list[dict] = []
    subdirs: set[str] = set()

    for row in rows:
        rel = row["path"][len(prefix):]
        # Skip if rel is empty (shouldn't happen) or the rel_dir/index.md itself.
        if not rel:
            continue
        if "/" not in rel:
            # Immediate file in this directory.
            if rel == "index.md":
                # Don't include index.md in the listing (it IS the listing).
                continue
            files.append({
                "name": rel,
                "path": row["path"],
                "title": row["title"],
                "description": row["description"],
                "is_handcrafted": bool(row.get("is_handcrafted")),
            })
        else:
            # In a subdirectory — extract first segment.
            first = rel.split("/", 1)[0]
            subdirs.add(first)

    return files, subdirs


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────


def _description_for(node: dict) -> str:
    """Pick a one-line description: description > title > fallback."""
    d = node.get("description")
    if d:
        return _clean(d)
    t = node.get("title")
    if t:
        return _clean(t)
    return "(no description)"


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    if len(s) > 200:
        s = s[:197] + "..."
    return s


def render_index_for_dir(
    graph: graph_mod.Graph, mem_home: str, rel_dir: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> str:
    """Return the markdown content that should be at `<rel_dir>/index.md`
    within the given root.

    Pure function — does not write to disk. Caller decides whether to write
    based on handcrafted-marker check or drift detection.
    """
    rel_dir_norm = rel_dir.strip("/")
    title = _title_for_dir(rel_dir_norm)
    files, subdirs = _list_immediate_children(graph, rel_dir_norm, root_id=root_id)

    # Subdirs are rendered with `index.md` if they have one tracked, otherwise
    # plain. We also note whether the subdir's own index is handcrafted.
    subdir_meta: list[dict] = []
    for sd in sorted(subdirs):
        sd_index_path = (rel_dir_norm + "/" + sd + "/index.md").lstrip("/")
        sd_index_node = graph_mod.get_node(graph, sd_index_path, root_id=root_id)
        subdir_meta.append({
            "name": sd,
            "has_index": sd_index_node is not None,
            "is_handcrafted": bool(sd_index_node and sd_index_node.get("is_handcrafted")),
        })

    lines = [f"# {title}", ""]
    lines.append(f"_Auto-rendered by memfs from {(mem_home + '/' + rel_dir_norm).rstrip('/')}/_")
    lines.append("")

    if files:
        lines.append("## Files")
        lines.append("")
        lines.append("| File | Description |")
        lines.append("|---|---|")
        for f in sorted(files, key=lambda x: x["name"]):
            lines.append(f"| `{f['name']}` | {_description_for(f)} |")
        lines.append("")

    if subdir_meta:
        lines.append("## Subdirectories")
        lines.append("")
        lines.append("| Dir | Index |")
        lines.append("|---|---|")
        for sd in subdir_meta:
            if sd["is_handcrafted"]:
                marker = "handcrafted"
            elif sd["has_index"]:
                marker = "auto"
            else:
                marker = "**MISSING**"
            lines.append(f"| `{sd['name']}/` | {marker} |")
        lines.append("")

    if not files and not subdir_meta:
        lines.append("_(empty)_")
        lines.append("")

    return "\n".join(lines)


def _title_for_dir(rel_dir: str) -> str:
    if rel_dir in ("", "."):
        return "Index"
    return rel_dir.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ").title()


# ─────────────────────────────────────────────────────────────────────────────
# Write-if-drifted (the load-bearing entry point for the watcher)
# ─────────────────────────────────────────────────────────────────────────────


def _is_handcrafted_on_disk(abs_index_path: str) -> bool:
    """Cheap pre-check before deciding whether to overwrite."""
    if not os.path.isfile(abs_index_path):
        return False
    try:
        with open(abs_index_path, "r", encoding="utf-8") as f:
            head = f.read(400)
        return HANDCRAFTED_MARKER in head
    except OSError:
        return False


def write_index_if_drifted(
    graph: graph_mod.Graph, mem_home: str, rel_dir: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> str:
    """Render this directory's index.md if drifted from graph view.

    Returns one of:
      "wrote"               — index.md was created or updated
      "skipped-handcrafted" — the on-disk file is handcrafted; left alone
      "no-change"           — content matches; no write needed
      "no-content"          — directory has no indexed markdown; no index needed
    """
    rel_dir_norm = rel_dir.strip("/")
    # Guard against escaping the root (e.g. paths starting with "..", which
    # appear when a symlink inside the tree points outside it). Don't try to
    # write outside the configured root.
    if rel_dir_norm.startswith("..") or "/.." in rel_dir_norm:
        return "no-content"
    abs_dir = os.path.join(mem_home, rel_dir_norm) if rel_dir_norm else mem_home
    # Resolve and re-check (catches symlink escapes)
    abs_dir_real = os.path.realpath(abs_dir)
    mem_home_real = os.path.realpath(mem_home)
    if not abs_dir_real.startswith(mem_home_real):
        return "no-content"
    abs_index = os.path.join(abs_dir, "index.md")

    # If on-disk file is handcrafted, leave it alone regardless of drift.
    if _is_handcrafted_on_disk(abs_index):
        return "skipped-handcrafted"

    files, subdirs = _list_immediate_children(graph, rel_dir_norm, root_id=root_id)
    if not files and not subdirs:
        # Nothing indexed under this dir → no index.md needed. If one exists
        # on disk and isn't handcrafted, leave it alone (could be a stub).
        return "no-content"

    new_text = render_index_for_dir(graph, mem_home, rel_dir_norm, root_id=root_id) + "\n"

    # Compare to existing.
    if os.path.isfile(abs_index):
        try:
            with open(abs_index, "r", encoding="utf-8") as f:
                old_text = f.read()
        except OSError:
            old_text = ""
        if old_text == new_text:
            return "no-change"

    # Make sure parent dir exists (should, since indexed files live under it,
    # but be defensive).
    os.makedirs(abs_dir, exist_ok=True)
    with open(abs_index, "w", encoding="utf-8") as f:
        f.write(new_text)
    return "wrote"


# ─────────────────────────────────────────────────────────────────────────────
# Drift detection (read-only)
# ─────────────────────────────────────────────────────────────────────────────


def check_drift_for_dir(
    graph: graph_mod.Graph, mem_home: str, rel_dir: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> list[str]:
    """Return list of drift findings for this directory's index.md.

    Empty list = no drift. Findings are short, human-readable strings.

    Three drift directions are checked:

    1. Forward (graph → index): every file/subdir the graph knows about
       should be mentioned in index.md.
    2. Reverse (index → filesystem): every backticked `name.md` or
       `subdir/` reference in index.md should exist on disk. **Filesystem
       is ground truth** — graph membership does not absolve a missing
       file. (Pre-2026-05-03 the detector silently filtered out
       index references that the graph also knew about, which made
       phantom-on-disk drift invisible — the n=1 watch
       `memfs-detector-blind-to-fs-drift`.)
    3. Index existence: if the directory has graph content OR an
       index.md on disk, scan. Pre-fix: an empty graph view caused
       early-exit even when index.md held phantom references.

    External references (slash-commands like `/telegram`, bare command
    names like `gog`, home-relative paths like `~/mail/outbox/`, and
    nested paths like `sub/file.md`) are heuristically excluded — they
    appear in handcrafted indexes as documentation, not as local-file
    claims. Only `name.md` and `name/` shapes are checked.
    """
    findings: list[str] = []
    rel_dir_norm = rel_dir.strip("/")
    abs_dir = os.path.join(mem_home, rel_dir_norm) if rel_dir_norm else mem_home
    abs_index = os.path.join(abs_dir, "index.md")

    files, subdirs = _list_immediate_children(graph, rel_dir_norm, root_id=root_id)
    index_exists = os.path.isfile(abs_index)

    # If the graph has nothing AND there's no index.md, nothing to check.
    if not files and not subdirs and not index_exists:
        return findings

    # Graph has content but no index → flag missing.
    if not index_exists:
        findings.append(f"MISSING: {abs_index}")
        return findings

    try:
        with open(abs_index, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        findings.append(f"UNREADABLE: {abs_index} ({e})")
        return findings

    # Forward (graph → index): graph-known items should be mentioned.
    for fmeta in files:
        if f"`{fmeta['name']}`" not in text:
            findings.append(f"FILE NOT IN INDEX: {fmeta['path']}")

    for sd in subdirs:
        if f"`{sd}/`" not in text and f"`{sd}`" not in text:
            findings.append(f"SUBDIR NOT IN INDEX: {sd}")

    # Reverse (index → filesystem): every backticked local-file or
    # local-subdir reference must exist on disk. Filesystem is ground
    # truth; graph state is irrelevant to this check.
    for m in re.finditer(r"`([^`]+)`", text):
        name = m.group(1).strip()
        if not name or name == "index.md":
            continue
        # Skip refs that aren't claims about local files/subdirs:
        #   - absolute paths (/foo) — slash-commands or system paths
        #   - home-relative (~/...) — external resources
        #   - parent-relative (../foo) — outside scope
        #   - dot-relative current dir, OK after stripping
        if name.startswith(("/", "~", "..")):
            continue
        if name.startswith("./"):
            name = name[2:]
        # After this normalization, an inner "/" means a nested path
        # like "sub/file.md" — out of scope for this directory's check.
        if name.rstrip("/").count("/") > 0:
            continue
        # Skip command-like refs (contain spaces, quotes, flags).
        if any(ch in name for ch in (" ", '"', "'", "*", "$", "|", "(")):
            continue
        if name.endswith("/"):
            subdir_name = name.rstrip("/")
            if not os.path.isdir(os.path.join(abs_dir, subdir_name)):
                findings.append(f"INDEX REFERENCES MISSING SUBDIR: {name}")
        elif name.endswith(".md"):
            if not os.path.exists(os.path.join(abs_dir, name)):
                findings.append(f"INDEX REFERENCES MISSING FILE: {name}")
        # else: bare token (e.g. `gog`, `tg`) — ambiguous between local
        # subdir-ref and external CLI command; skip to avoid false
        # positives. The graph→index forward check still catches missing
        # mentions of known subdirs.

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# Bulk operations
# ─────────────────────────────────────────────────────────────────────────────


def _walk_indexed_dirs(
    graph: graph_mod.Graph, root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> Iterator[str]:
    """Yield every directory path (relative to mem_home) that contains at least
    one indexed file under the given root. Includes the root ("")."""
    # Filter out :Node stubs synthesized by upsert_link_edge for broken
    # wikilink targets (content_hash IS NULL). Without this filter, stub
    # paths like '../../areas/USER.md' or '/home/mike/journal.md' generate
    # bogus "directories" via the rsplit logic below, and check_drift_for_dir
    # then emits "INDEX REFERENCES MISSING FILE" findings against fictional
    # parent dirs. Was the n=2 sibling of verify-detector-premise watch
    # discovered 2026-05-03 in heartbeat ghost-node investigation.
    rows = graph.run(
        "MATCH (n:Node) WHERE n.root_id = $root_id AND n.content_hash IS NOT NULL "
        "RETURN DISTINCT n.path AS path",
        root_id=root_id,
    )
    seen: set[str] = set()
    for row in rows:
        path = row["path"]
        if "/" in path:
            d = path.rsplit("/", 1)[0]
            # Yield this dir and all ancestor dirs.
            parts = d.split("/")
            for i in range(len(parts)):
                ancestor = "/".join(parts[: i + 1])
                if ancestor not in seen:
                    seen.add(ancestor)
                    yield ancestor
        else:
            # Top-level file → root dir.
            if "" not in seen:
                seen.add("")
                yield ""


def render_all(
    graph: graph_mod.Graph, mem_home: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> dict[str, int]:
    """Re-render index.md across every directory containing indexed content.

    Returns counts: {"wrote": N, "skipped-handcrafted": N, "no-change": N, "no-content": N}.
    """
    counts = {"wrote": 0, "skipped-handcrafted": 0, "no-change": 0, "no-content": 0}
    for rel_dir in _walk_indexed_dirs(graph, root_id=root_id):
        result = write_index_if_drifted(graph, mem_home, rel_dir, root_id=root_id)
        counts[result] = counts.get(result, 0) + 1
    return counts


def check_all(
    graph: graph_mod.Graph, mem_home: str,
    root_id: str = graph_mod.DEFAULT_ROOT_ID,
) -> dict[str, list[str]]:
    """Return drift findings across the entire indexed tree, keyed by directory."""
    out: dict[str, list[str]] = {}
    for rel_dir in _walk_indexed_dirs(graph, root_id=root_id):
        findings = check_drift_for_dir(graph, mem_home, rel_dir, root_id=root_id)
        if findings:
            out[rel_dir or "."] = findings
    return out
