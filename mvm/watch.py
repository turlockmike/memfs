"""mvm watch — daemon that embeds new/changed docs after a debounce window.

Watches ~/mvm/knowledge/ via inotify (Linux/Mac). On any *.md change,
schedules `mvm index --embed-only` to run after the debounce expires.
If more changes arrive during the wait, the timer restarts.

The embed-only path is idempotent — it only embeds docs missing from files_vec —
so spurious wakes are cheap.

Usage:
  mvm watch                                 # 30s debounce, default knowledge root
  mvm watch --debounce 60
  mvm watch --root /custom/path
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("MVM_KNOWLEDGE", str(Path.home() / "mvm" / "knowledge")))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Watch the KB and embed new/changed docs after a debounce.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--debounce", type=float, default=30.0,
                        help="Seconds of quiet required before triggering embed (default: 30).")
    parser.add_argument("--mvm-bin", default="mvm",
                        help="Path to mvm CLI (default: mvm on PATH).")
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"ERROR: root not found: {args.root}", file=sys.stderr)
        return 2

    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("ERROR: watchdog not installed. pip install watchdog", file=sys.stderr)
        return 3

    last_event_time = [0.0]   # mutable single-cell so the closure can update it
    timer = [None]            # active threading.Timer instance, if any
    lock = threading.Lock()

    def trigger_embed():
        with lock:
            timer[0] = None
        print(f"[mvm watch] debounce expired; running embed-only...", flush=True)
        try:
            r = subprocess.run([args.mvm_bin, "index", "--embed-only", "--quiet"],
                               check=False, capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                print(f"[mvm watch] embed-only exit {r.returncode}: {r.stderr.strip()}", file=sys.stderr)
            else:
                print(f"[mvm watch] embed-only complete.", flush=True)
        except subprocess.TimeoutExpired:
            print(f"[mvm watch] embed-only timed out (>10min)", file=sys.stderr)

    def schedule_embed():
        with lock:
            if timer[0] is not None:
                timer[0].cancel()
            t = threading.Timer(args.debounce, trigger_embed)
            t.daemon = True
            timer[0] = t
            t.start()

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if event.is_directory:
                return
            p = Path(event.src_path)
            if p.suffix != ".md" or p.name == "INDEX.md" or p.name.endswith(".tests.md"):
                return
            last_event_time[0] = time.time()
            schedule_embed()

    observer = Observer()
    observer.schedule(Handler(), str(args.root), recursive=True)
    observer.start()
    print(f"[mvm watch] watching {args.root} (debounce={args.debounce}s). Ctrl-C to stop.", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n[mvm watch] stopping.", flush=True)
        observer.stop()
    observer.join()
    return 0
