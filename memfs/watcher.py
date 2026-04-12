"""Filesystem watcher daemon — keeps .mem/memory.db in sync with file changes."""

import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from memfs.db import connect
from memfs.indexer import (
    index_file,
    remove_file,
    rename_path,
    upgrade_broken_links,
    load_memignore,
    is_ignored,
    update_node,
)
from memfs.paths import normalize_path


class MemfsEventHandler(FileSystemEventHandler):
    """Handles filesystem events and updates the SQLite index."""

    def __init__(self, mem_home: str, db_path: str):
        super().__init__()
        self.mem_home = mem_home
        self.db_path = db_path
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
        return path.endswith(".md")

    def _log(self, event: str, path: str, **kwargs):
        print(
            json.dumps({"event": event, "path": self._rel_path(path), **kwargs}),
            file=sys.stderr,
            flush=True,
        )

    # --- Public methods (called directly by tests and by watchdog events) ---

    def on_created_file(self, abs_path: str) -> None:
        """Handle file creation."""
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        conn = connect(self.db_path)
        try:
            index_file(conn, self.mem_home, rel)
            upgraded = upgrade_broken_links(conn, rel)
            self._log("created", abs_path, indexed=True, broken_links_upgraded=upgraded)
        finally:
            conn.close()

    def on_modified_file(self, abs_path: str) -> None:
        """Handle file modification."""
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        conn = connect(self.db_path)
        try:
            changed = update_node(conn, self.mem_home, rel)
            if changed:
                self._log("modified", abs_path, indexed=True)
        finally:
            conn.close()

    def on_deleted_file(self, abs_path: str) -> None:
        """Handle file deletion."""
        if not self._is_md(abs_path) or self._should_ignore(abs_path):
            return
        rel = self._rel_path(abs_path)
        conn = connect(self.db_path)
        try:
            remove_file(conn, rel)
            self._log("deleted", abs_path, indexed=True)
        finally:
            conn.close()

    def on_moved_file(self, src_abs: str, dest_abs: str) -> None:
        """Handle file rename/move."""
        if not self._is_md(src_abs) and not self._is_md(dest_abs):
            return
        if self._should_ignore(dest_abs):
            # Moved to ignored location — treat as delete
            self.on_deleted_file(src_abs)
            return

        old_rel = self._rel_path(src_abs)
        new_rel = self._rel_path(dest_abs)
        conn = connect(self.db_path)
        try:
            # Remove old entry and reindex at new path
            remove_file(conn, old_rel)
            if os.path.exists(dest_abs):
                index_file(conn, self.mem_home, new_rel)
                upgrade_broken_links(conn, new_rel)
            self._log("moved", dest_abs, from_path=old_rel)
        finally:
            conn.close()

    def on_moved_directory(self, src_abs: str, dest_abs: str) -> None:
        """Handle directory rename — update all paths with old prefix."""
        old_prefix = self._rel_path(src_abs)
        new_prefix = self._rel_path(dest_abs)
        conn = connect(self.db_path)
        try:
            rename_path(conn, old_prefix, new_prefix)
            self._log("dir_moved", dest_abs, from_path=old_prefix)
        finally:
            conn.close()

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


def start_watcher(mem_home: str, db_path: str, daemon: bool = False) -> None:
    """Start the filesystem watcher."""
    if daemon:
        pid_file = os.path.join(mem_home, ".mem", "watch.pid")
        # Fork to background
        pid = os.fork()
        if pid > 0:
            # Parent — write PID and exit
            with open(pid_file, "w") as f:
                f.write(str(pid))
            print(
                json.dumps({"action": "watch", "daemon": True, "pid": pid}),
                flush=True,
            )
            return
        # Child continues below
        os.setsid()

    handler = MemfsEventHandler(mem_home, db_path)
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
    """Stop a running daemon by PID file. Returns True if stopped."""
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
    """Check if watcher daemon is running."""
    pid_file = os.path.join(mem_home, ".mem", "watch.pid")
    if not os.path.exists(pid_file):
        return {"running": False}
    with open(pid_file) as f:
        pid = int(f.read().strip())
    try:
        os.kill(pid, 0)  # Check if process exists
        return {"running": True, "pid": pid}
    except ProcessLookupError:
        os.unlink(pid_file)
        return {"running": False}
