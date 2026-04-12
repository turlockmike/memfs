"""Path resolution and normalization for memfs."""

import os


def normalize_path(path: str, mem_home: str) -> str:
    """Normalize a path to be relative to MEM_HOME. Rejects '..' components."""
    if ".." in path.split(os.sep):
        raise ValueError(f"'..' in paths is not allowed: {path}")

    # Convert absolute to relative
    if os.path.isabs(path):
        path = os.path.relpath(path, mem_home)

    # Strip leading/trailing slashes
    path = path.strip("/")

    # Final check for ..
    if ".." in path.split(os.sep):
        raise ValueError(f"'..' in paths is not allowed: {path}")

    return path


def resolve_link(target: str, source_path: str, mem_home: str) -> str:
    """Resolve a [[link]] target relative to the source file's directory.

    If target contains a path separator, it's treated as relative to MEM_HOME.
    Otherwise, it's relative to the source file's directory.
    Adds .md extension if missing.
    """
    if not target.endswith(".md"):
        target = target + ".md"

    if os.sep in target or "/" in target:
        # Absolute-style link (relative to MEM_HOME)
        return target

    # Relative to source file's directory
    source_dir = os.path.dirname(source_path)
    if source_dir:
        return os.path.join(source_dir, target)
    return target
