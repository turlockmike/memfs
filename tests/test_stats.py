"""Stats subcommand: log parsing, aggregation."""
import json
from datetime import datetime, timezone

from mvm.stats import load_entries, aggregate, parse_window, render_text


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


# --- source-mix accounting (2026-07-28 audit specimen) -----------------------
# The live log writes decided_source values the renderer never knew about
# ('mixed', 'engine'). Every pre-existing test above used only the four
# canonical values, so the suite shared the renderer's blind spot and 45% of a
# real 7d window vanished from the display while `n` still counted it.

def _mix(text):
    """Parse the 'Source mix:' block into {bucket: count}."""
    lines = text.splitlines()
    i = lines.index("Source mix:")
    out = {}
    for ln in lines[i + 1:]:
        if not ln.strip():
            break
        parts = ln.split()
        out[parts[0]] = int(parts[1].split("/")[0])
    return out


def test_render_shows_non_canonical_source_buckets():
    entries = [{"decided_source": s} for s in
               ("kb", "kb", "mixed", "mixed", "mixed", "engine", "web")]
    mix = _mix(render_text(aggregate(entries), "7d"))
    assert mix["mixed"] == 3, "non-canonical bucket must be rendered, not dropped"
    assert mix["engine"] == 1


def test_render_source_mix_accounts_for_every_entry():
    """The oracle the renderer lacked: buckets must sum to n."""
    entries = [{"decided_source": s} for s in
               ("kb", "mixed", "engine", "web", "none", "weights", "surprise")]
    text = render_text(aggregate(entries), "7d")
    mix = _mix(text)
    assert "ACCOUNTING GAP" not in text
    assert mix["accounted"] == len(entries)
    assert sum(v for k, v in mix.items() if k != "accounted") == len(entries)


def test_kb_alone_insufficient_counts_mixed():
    entries = [{"decided_source": s} for s in ("kb", "kb", "mixed", "web")]
    text = render_text(aggregate(entries), "7d")
    assert "KB-alone-insufficient (web+mixed) = 50%" in text
    # ...and the strict signal keeps its original calibration untouched
    assert "strict web-only rate = 25%" in text


# --- coverage n-floor (auditor D180-1, 2026-08-07) -------------------------
# The live specimen: n=5 with 2 web recalls rendered "40% -> ALERT", while the
# same corpus is 20% at n=20, 16% at n=50 and 12% at n=200. The alarm was
# describing the window, not the KB. Floor convention ported from
# kalshi-calibration (kill floor n>=20, explicit below-floor verdict).

def _cov_line(text):
    return next(l for l in text.splitlines() if "strict web-only rate" in l)


def test_coverage_below_n_floor_issues_no_verdict():
    """The exact D180-1 specimen: n=5, 2 web. Must NOT say ALERT."""
    entries = [{"decided_source": s} for s in ("kb", "kb", "kb", "web", "web")]
    line = _cov_line(render_text(aggregate(entries), "7d"))
    assert "40%" in line, "the rate is still reported — only the verdict is withheld"
    assert "WATCH 5/20" in line
    assert "NO VERDICT" in line
    # A suppressed alarm and a passing one must never print the same word.
    for verdict in ("ALERT", "WARN", "OK"):
        assert verdict not in line.split("→")[1].replace("WATCH", "")


def test_coverage_at_n_floor_issues_a_verdict():
    """At exactly n=20 the floor is satisfied and the calibration applies."""
    entries = [{"decided_source": "web"}] * 8 + [{"decided_source": "kb"}] * 12
    line = _cov_line(render_text(aggregate(entries), "7d"))
    assert "40%" in line and "ALERT" in line and "WATCH" not in line


def test_coverage_floor_does_not_swallow_a_healthy_verdict():
    """Blast-radius half: an OK verdict above the floor stays OK."""
    entries = [{"decided_source": "web"}] * 2 + [{"decided_source": "kb"}] * 23
    line = _cov_line(render_text(aggregate(entries), "7d"))
    assert "→ OK" in line and "WATCH" not in line


def test_coverage_floor_is_not_a_default_to_ok():
    """n=19 with a 100% web rate must still refuse a verdict, not pass."""
    entries = [{"decided_source": "web"}] * 19
    line = _cov_line(render_text(aggregate(entries), "7d"))
    assert "WATCH 19/20" in line and "ALERT" not in line and "→ OK" not in line
