"""mvm log — show recall or dream log entries.

Usage:
  mvm log                      # last 10 recall entries
  mvm log --type dream         # dream cycles
  mvm log --last 50            # more entries
  mvm log --since 2026-05-01   # since date
  mvm log --json               # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE = Path(os.environ.get("MVM_STATE", str(Path.home() / "mvm" / "state")))

LOG_FILES = {
    "recall": "recall-log.jsonl",
    "dream": "dream-log.jsonl",
}


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _load_entries(path: Path, since: datetime | None) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None:
            ts = _parse_ts(obj.get("ts"))
            if ts is None or ts < since:
                continue
        entries.append(obj)
    return entries


def _render_recall(e: dict) -> str:
    ts = e.get("ts", "?")[:19]
    src = e.get("decided_source", "?")
    q = (e.get("question") or "")[:70]
    ingested = "✓" if e.get("ingested") else " "
    return f"{ts}  [{src:8s}] {ingested} {q}"


def _render_dream(e: dict) -> str:
    ts = e.get("ts", "?")[:19]
    actions = e.get("actions", {})
    ing = len(actions.get("coverage_ingests", []))
    repaired = len(actions.get("quality_repairs", []))
    transient = len(actions.get("transients", []))
    superseded = len(actions.get("staleness_supersedes", []))
    contested = len(actions.get("contested_resolved", []))
    dur = e.get("duration_ms", 0) / 1000
    return (f"{ts}  ingest={ing}  repair={repaired}  supersede={superseded}  "
            f"contested={contested}  transient={transient}  ({dur:.1f}s)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Show MVM log entries (recall or dream).")
    parser.add_argument("--type", choices=list(LOG_FILES.keys()), default="recall",
                        help="Which log to show (default: recall).")
    parser.add_argument("--last", type=int, default=10,
                        help="Show only the last N entries (default: 10; use 0 for all).")
    parser.add_argument("--since", type=str,
                        help="Only entries on or after this ISO date (e.g. 2026-05-01).")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON array instead of formatted text.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args(argv)

    log_path = args.state / LOG_FILES[args.type]
    since = _parse_ts(args.since) if args.since else None
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    entries = _load_entries(log_path, since)
    if not entries:
        print(f"(no entries in {log_path})")
        return 0

    if args.last > 0:
        entries = entries[-args.last:]

    if args.json:
        print(json.dumps(entries, indent=2))
        return 0

    renderer = _render_recall if args.type == "recall" else _render_dream
    print(f"# {args.type}-log: {len(entries)} entries from {log_path}")
    print()
    for e in entries:
        try:
            print(renderer(e))
        except Exception as ex:
            print(f"(render error: {ex}; entry: {json.dumps(e)[:120]})")
    return 0
