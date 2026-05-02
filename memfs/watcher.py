"""Filesystem watcher daemon — keeps Neo4j graph in sync with file changes.

Also (added 2026-05-01) keeps per-directory `index.md` files in sync with the
graph view via `index_render.write_index_if_drifted`. memfs is the holistic
memory system: indexes are a first-class output surface, not a peripheral
renderer. Render is synchronous-per-event in v1; if perf becomes an issue,
add a debounce window over a "dirty directories" set.
"""

import json
import os
import signal
import sys

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from memfs.graph import connect
from memfs.indexer import (
    index_file,
    remove_file,
    rename_path,
    upgrade_broken_links,
    load_memignore,
    is_ignored,
    update_node,
)
from memfs.index_render import write_index_if_drifted


class MemfsEventHandler(FileSystemEventHandler):
    """Handles filesystem events and updates the Neo4j graph."""

    def __init__(self, mem_home: str):
        super().__init__()
        self.mem_home = mem_home
        self._ignore_patterns = None
        self._memignore_mtime = None

    @property
    def ignore_patterns(self):
        """Lazy-load and cache ignore patterns, reloading if .memignore changes."""
        ignore_file = os.path.join(self.mem_home, ".memignore")
        current_mtime = os.path.getmtime(ignore_file) if os.path.exists(ignore_file) else None
        if self._ignore_patterns is None or current_mtime != self._memignore_mtime:
            self._ignore_patterns = load_memignore(self.mem_home)
            self._memignore_mtime = current_mtime
        return self._ignore_patterns

    def _rel_path(self, abs_path: str) -> str:
        return os.path.relpath(abs_path, self.mem_home)

    def _should_ignore(self, abs_path: str) -> bool:
        rel = self._rel_path(abs_path)
        return is_ignored(rel, self.ignore_patterns)

    def _is_md(self, path: str) -> bool:
        return path.endswith(".md") or path.endswith(".jsonl")

    def _maybe_render_dir_index(self, graph, abs_path: str) -> None:
        """Re-render the parent directory's `index.md` if drifted.

        Skips the case where the changed file IS the index.md itself (would be
        a self-trigger; `write_index_if_drifted` would return 'no-change'
        anyway, but skipping avoids the round trip). Skips JSONL files —
        indexes only cover markdown.
        """
        if not abs_path.endswith(".md"):
            return
        if os.path.basename(abs_path) == "index.md":
            return
        rel_dir = os.path.relpath(os.path.dirname(abs_path), self.mem_home)
        if rel_dir == ".":
            rel_dir = ""
        try:
            result = write_index_if_drifted(graph, self.mem_home, rel_dir)
            if result == "wrote":
                self._log("index_rendered", os.path.join(self.mem_home, rel_dir, "index.md"))
        except Exception as e:
            # Don't let an index render failure break the indexing flow.
            self._log("index_render_error", abs_path, error=str(e))

    def _log(self, event: str, path: str, **kwargs):
        print(
            json.dumps({"event": event, "path": self._rel_path(path), **kwargs}),
            file=sys.stderr,
            flush=True,
        )

    # --- Public methods (called directly by tests and by watchdog events) ---

    def on_created_file(self, abs_path: str) -> None:
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        graph = connect()
        try:
            index_file(graph, self.mem_home, rel)
            upgraded = upgrade_broken_links(graph, rel)
            self._log("created", abs_path, indexed=True, broken_links_upgraded=upgraded)
            # M4 hook: contradiction detection for layer >= 3 nodes
            _maybe_detect_contradictions(graph, self.mem_home, rel)
            # Index-render hook (2026-05-01): refresh parent dir's index.md
            self._maybe_render_dir_index(graph, abs_path)
        finally:
            graph.close()

    def on_modified_file(self, abs_path: str) -> None:
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        graph = connect()
        try:
            changed = update_node(graph, self.mem_home, rel)
            if changed:
                self._log("modified", abs_path, indexed=True)
                _maybe_detect_contradictions(graph, self.mem_home, rel)
                # Description/title may have changed → re-render parent's index.
                self._maybe_render_dir_index(graph, abs_path)
        finally:
            graph.close()

    def on_deleted_file(self, abs_path: str) -> None:
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        graph = connect()
        try:
            remove_file(graph, rel)
            self._log("deleted", abs_path, indexed=True)
            # Parent index now references a missing file — re-render.
            self._maybe_render_dir_index(graph, abs_path)
        finally:
            graph.close()

    def on_moved_file(self, src_abs: str, dest_abs: str) -> None:
        if not self._is_md(src_abs) and not self._is_md(dest_abs):
            return
        if self._should_ignore(dest_abs):
            self.on_deleted_file(src_abs)
            return

        old_rel = self._rel_path(src_abs)
        new_rel = self._rel_path(dest_abs)
        graph = connect()
        try:
            remove_file(graph, old_rel)
            if os.path.exists(dest_abs):
                index_file(graph, self.mem_home, new_rel)
                upgrade_broken_links(graph, new_rel)
            self._log("moved", dest_abs, from_path=old_rel)
            # Re-render BOTH old and new parent dirs (could be the same dir).
            self._maybe_render_dir_index(graph, src_abs)
            if os.path.dirname(src_abs) != os.path.dirname(dest_abs):
                self._maybe_render_dir_index(graph, dest_abs)
        finally:
            graph.close()

    def on_moved_directory(self, src_abs: str, dest_abs: str) -> None:
        old_prefix = self._rel_path(src_abs)
        new_prefix = self._rel_path(dest_abs)
        graph = connect()
        try:
            rename_path(graph, old_prefix, new_prefix)
            self._log("dir_moved", dest_abs, from_path=old_prefix)
        finally:
            graph.close()

    # --- Watchdog event dispatch ---

    def on_created(self, event):
        if not event.is_directory:
            self.on_created_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.on_modified_file(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.on_deleted_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            self.on_moved_directory(event.src_path, event.dest_path)
        else:
            self.on_moved_file(event.src_path, event.dest_path)


def _maybe_detect_contradictions(graph, mem_home: str, rel_path: str) -> None:
    """Run contradiction detection for layer >= 3 nodes (M4).

    Imported lazily so M1-era installs without contradiction.py still work.
    """
    try:
        from memfs.contradiction import detect_contradictions
    except ImportError:
        return
    try:
        conflicts = detect_contradictions(graph, rel_path)
    except Exception as e:
        print(
            json.dumps({"event": "contradiction_error", "path": rel_path, "error": str(e)}),
            file=sys.stderr,
            flush=True,
        )
        return
    for c in conflicts:
        print(
            json.dumps({"event": "conflict", **c}),
            file=sys.stderr,
            flush=True,
        )


def start_watcher(mem_home: str, daemon: bool = False) -> None:
    """Start the filesystem watcher."""
    if daemon:
        pid_file = os.path.join(mem_home, ".mem", "watch.pid")
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        pid = os.fork()
        if pid > 0:
            with open(pid_file, "w") as f:
                f.write(str(pid))
            print(
                json.dumps({"action": "watch", "daemon": True, "pid": pid}),
                flush=True,
            )
            return
        os.setsid()

    handler = MemfsEventHandler(mem_home)
    observer = Observer()
    observer.schedule(handler, mem_home, recursive=True)
    observer.start()

    print(
        json.dumps({"action": "watch", "mem_home": mem_home, "daemon": daemon}),
        file=sys.stderr,
        flush=True,
    )

    def shutdown(signum, frame):
        observer.stop()
        observer.join()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def stop_watcher(mem_home: str) -> bool:
    pid_file = os.path.join(mem_home, ".mem", "watch.pid")
    if not os.path.exists(pid_file):
        return False
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        os.unlink(pid_file)
        return True
    except ProcessLookupError:
        os.unlink(pid_file)
        return False


def watcher_status(mem_home: str) -> dict:
    pid_file = os.path.join(mem_home, ".mem", "watch.pid")
    if not os.path.exists(pid_file):
        return {"running": False}
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)
        return {"running": True, "pid": pid}
    except ProcessLookupError:
        os.unlink(pid_file)
        return {"running": False}
