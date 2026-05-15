"""
mvm-stats: read ~/mvm/state/recall-log.jsonl and emit the decoherence dashboard.

Four decoherence types tracked:
  - Coverage:   web-fallback rate (KB doesn't have what users ask)
  - Quality:    cold-clone fail rate on existing canonicals (canonicals can't be retrieved from)
  - Staleness:  KB-vs-web contradictions (substrate aging out)
  - Bloat:      mean search latency / candidate-set size growth (KB getting noisy)

v0 implements the recall-log-based metrics (coverage primarily). Quality + staleness +
bloat hooks reserved for v0.1 (need additional periodic re-verify and consolidate-pass logs).

Usage:
  mvm-stats                    # last 7 days
  mvm-stats --window 30d
  mvm-stats --since 2026-05-01
  mvm-stats --json             # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_LOG = Path(os.environ.get(
    "MVM_RECALL_LOG", str(Path.home() / "mvm" / "state" / "recall-log.jsonl")
))


def parse_window(s: str) -> timedelta:
    """Parse '7d', '30d', '24h' etc."""
    s = s.strip().lower()
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("w"):
        return timedelta(weeks=int(s[:-1]))
    raise ValueError(f"unknown window format: {s}")


def load_entries(path: Path, since: datetime | None) -> list[dict]:
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
        ts_str = obj.get("ts")
        if ts_str and since:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts < since:
                    continue
            except ValueError:
                pass
        entries.append(obj)
    return entries


def aggregate(entries: list[dict]) -> dict:
    n = len(entries)
    if n == 0:
        return {"n": 0}

    sources = Counter(e.get("decided_source", "unknown") for e in entries)
    patterns = Counter(e.get("reconciliation_pattern", "unknown") for e in entries)
    n_ingested = sum(1 for e in entries if e.get("ingested"))

    # Web-fallback by topic
    topic_fallbacks = defaultdict(int)
    topic_recalls = defaultdict(int)
    for e in entries:
        topic = e.get("topic_hint") or "unspecified"
        topic_recalls[topic] += 1
        if e.get("decided_source") == "web":
            topic_fallbacks[topic] += 1

    # Hard misses
    n_hard_miss = sum(1 for e in entries if e.get("decided_source") == "none")

    # Mean duration
    durations = [e.get("duration_ms", 0) for e in entries if e.get("duration_ms")]
    mean_duration = sum(durations) / len(durations) if durations else 0

    return {
        "n": n,
        "sources": dict(sources),
        "patterns": dict(patterns),
        "ingested": n_ingested,
        "ingest_rate": n_ingested / n if n else 0,
        "hard_misses": n_hard_miss,
        "topic_fallbacks": dict(topic_fallbacks),
        "topic_recalls": dict(topic_recalls),
        "mean_duration_ms": mean_duration,
    }


def render_text(stats: dict, window_label: str) -> str:
    if stats["n"] == 0:
        return f"No recalls logged in {window_label}.\n(Recall-log path: {DEFAULT_LOG})"

    n = stats["n"]
    sources = stats["sources"]
    out = [f"Recalls (last {window_label}): {n}", ""]

    out.append("Source mix:")
    for src in ("kb", "web", "weights", "none"):
        c = sources.get(src, 0)
        pct = (c / n * 100) if n else 0
        out.append(f"  {src:10s}  {c:4d}  ({pct:.0f}%)")
    out.append("")

    out.append(f"Ingestions: {stats['ingested']} ({stats['ingest_rate']*100:.0f}% of recalls grew the KB)")
    out.append(f"Hard misses: {stats['hard_misses']}")
    out.append("")

    out.append("Top web-fallback topics (substrate gaps — consider proactive /mvm-ingest):")
    fb = sorted(stats["topic_fallbacks"].items(), key=lambda kv: -kv[1])[:5]
    if not fb:
        out.append("  (none — KB covering all queried topics)")
    for topic, count in fb:
        total = stats["topic_recalls"].get(topic, count)
        out.append(f"  {topic:30s}  {count:3d} fallbacks / {total} total")
    out.append("")

    out.append("Reconciliation patterns:")
    for pat, count in sorted(stats["patterns"].items(), key=lambda kv: -kv[1])[:6]:
        pct = (count / n * 100) if n else 0
        out.append(f"  {pat:40s}  {count:3d}  ({pct:.0f}%)")
    out.append("")

    out.append(f"Mean recall duration: {stats['mean_duration_ms']:.0f} ms")

    # Decoherence signal summary (v0 placeholder)
    out.append("")
    out.append("Decoherence signals (v0 — coverage only; quality/staleness/bloat in v0.1):")
    cov_rate = sources.get("web", 0) / n if n else 0
    cov_status = "OK" if cov_rate < 0.20 else "WARN" if cov_rate < 0.35 else "ALERT"
    out.append(f"  Coverage:   web-fallback rate = {cov_rate*100:.0f}%  → {cov_status}")
    out.append("  Quality:    (not yet measured — needs periodic re-verify)")
    out.append("  Staleness:  (not yet measured — needs KB-vs-web contradiction count)")
    out.append("  Bloat:      (not yet measured — needs search-latency log)")

    return "\n".join(out)


def main(argv = None) -> int:
    parser = argparse.ArgumentParser(description="MVM recall-log decoherence dashboard.")
    parser.add_argument("--window", default="7d", help="Time window (e.g. '7d', '24h', '30d').")
    parser.add_argument("--since", help="Absolute start (ISO date), overrides --window.")
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG, help="Recall-log path.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        window_label = f"since {args.since}"
    else:
        since = datetime.now(timezone.utc) - parse_window(args.window)
        window_label = args.window

    entries = load_entries(args.log, since)
    stats = aggregate(entries)

    if args.json:
        print(json.dumps({"window": window_label, "stats": stats}, indent=2))
    else:
        print(render_text(stats, window_label))

    return 0


