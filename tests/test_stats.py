"""Stats subcommand: log parsing, aggregation."""
import json
from datetime import datetime, timezone

from mvm.stats import load_entries, aggregate, parse_window


def test_parse_window_days():
    assert parse_window("7d").days == 7


def test_parse_window_hours():
    assert parse_window("24h").total_seconds() == 24 * 3600


def test_parse_window_weeks():
    assert parse_window("2w").days == 14


def test_load_entries_empty(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("")
    assert load_entries(p, None) == []


def test_load_entries_skip_blank_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text('\n  \n{"ts":"2026-05-08T20:00:00+00:00","decided_source":"kb"}\n\n')
    entries = load_entries(p, None)
    assert len(entries) == 1
    assert entries[0]["decided_source"] == "kb"


def test_load_entries_skip_malformed_json(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(
        'not-json\n'
        '{"ts":"2026-05-08T20:00:00+00:00","decided_source":"web"}\n'
        '{also not valid}\n'
    )
    entries = load_entries(p, None)
    assert len(entries) == 1
    assert entries[0]["decided_source"] == "web"


def test_load_entries_filters_by_since(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(
        '{"ts":"2026-04-01T00:00:00+00:00","decided_source":"kb"}\n'
        '{"ts":"2026-05-08T20:00:00+00:00","decided_source":"web"}\n'
    )
    since = datetime(2026, 5, 1, tzinfo=timezone.utc)
    entries = load_entries(p, since)
    assert len(entries) == 1
    assert entries[0]["decided_source"] == "web"


def test_aggregate_empty():
    stats = aggregate([])
    assert stats == {"n": 0}


def test_aggregate_counts_sources():
    entries = [
        {"decided_source": "kb", "ingested": False, "topic_hint": "poe2"},
        {"decided_source": "web", "ingested": True, "topic_hint": "poe2"},
        {"decided_source": "web", "ingested": True, "topic_hint": "anthropic"},
        {"decided_source": "weights", "ingested": False, "topic_hint": "general"},
    ]
    stats = aggregate(entries)
    assert stats["n"] == 4
    assert stats["sources"]["kb"] == 1
    assert stats["sources"]["web"] == 2
    assert stats["sources"]["weights"] == 1
    assert stats["ingested"] == 2
    assert stats["ingest_rate"] == 0.5
    assert stats["topic_fallbacks"]["poe2"] == 1
    assert stats["topic_fallbacks"]["anthropic"] == 1
