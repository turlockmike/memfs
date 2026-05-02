"""Root configuration — multi-root support for memfs.

memfs originally had a single MEM_HOME (env var). It can now index multiple
roots simultaneously. Each root has:

  - `id`: short stable identifier used in the graph as a Node property
  - `path`: absolute filesystem path

Roots are configured via `~/.config/memfs/roots.json`. Example shape:

    {
      "roots": [
        {"id": "<your-id-1>", "path": "</absolute/path/1>"},
        {"id": "<your-id-2>", "path": "</absolute/path/2>"}
      ]
    }

Resolution rules:

  1. If `MEM_HOME` env var is set → operate on a single ad-hoc root.
     Its id is derived from the basename. (Legacy single-root behavior.)
  2. Else if config file exists → use its roots list.
  3. Else fall back to single root at `os.getcwd()` with id "default".

Composite-key invariant: every Node in the graph has `(root_id, path)` as its
unique identifier. `path` is relative to the root's filesystem path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


CONFIG_PATH = Path.home() / ".config" / "memfs" / "roots.json"


@dataclass(frozen=True)
class Root:
    id: str
    path: str  # absolute filesystem path

    def relpath(self, abs_path: str) -> str:
        """Return abs_path relative to this root, or raise if not under root."""
        rel = os.path.relpath(abs_path, self.path)
        if rel.startswith(".."):
            raise ValueError(f"{abs_path} not under root {self.path}")
        return rel

    def contains(self, abs_path: str) -> bool:
        try:
            return not os.path.relpath(abs_path, self.path).startswith("..")
        except ValueError:
            return False


def _id_from_path(path: str) -> str:
    """Derive a stable root id from a filesystem path. Used for env-var roots."""
    base = os.path.basename(os.path.normpath(path)) or "root"
    # Sanitize: lowercase, replace separators
    return base.lower().replace(" ", "-").replace("_", "-")


def load_roots() -> List[Root]:
    """Resolve the configured roots per the rules in the module docstring."""
    env = os.environ.get("MEM_HOME")
    if env:
        # Legacy env-var path. Single-root mode.
        env_abs = os.path.abspath(os.path.expanduser(env))
        return [Root(id=_id_from_path(env_abs), path=env_abs)]

    if CONFIG_PATH.is_file():
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"failed to read {CONFIG_PATH}: {e}")
        roots = data.get("roots") or []
        if not roots:
            raise RuntimeError(f"{CONFIG_PATH} has no roots configured")
        out: List[Root] = []
        seen_ids: set[str] = set()
        for entry in roots:
            rid = entry.get("id")
            path = entry.get("path")
            if not rid or not path:
                raise RuntimeError(f"each root must have id and path; got {entry!r}")
            if rid in seen_ids:
                raise RuntimeError(f"duplicate root id: {rid}")
            seen_ids.add(rid)
            out.append(Root(id=rid, path=os.path.abspath(os.path.expanduser(path))))
        return out

    # Last resort: cwd as a default root.
    cwd = os.getcwd()
    return [Root(id="default", path=cwd)]


def primary_root() -> Root:
    """First configured root. Used by commands that need a single home (e.g. legacy
    callers, or commands like `init` that need to know which root to operate on)."""
    return load_roots()[0]


def root_for_path(abs_path: str, roots: List[Root] | None = None) -> Root | None:
    """Find the root whose tree contains `abs_path`. Returns None if no match.

    When multiple roots could match (one root nested under another), prefer the
    most-specific (longest-path) match.
    """
    if roots is None:
        roots = load_roots()
    candidates = [r for r in roots if r.contains(abs_path)]
    if not candidates:
        return None
    candidates.sort(key=lambda r: len(r.path), reverse=True)
    return candidates[0]
