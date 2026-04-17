#!/usr/bin/env bash
# backfill-sessions.sh — One-off: ingest all recent Claude Code sessions
# for the -home-mike project into memfs.
#
# Usage: bash scripts/backfill-sessions.sh [DAYS]
#   DAYS — how many days back to scan (default 7)
#
# Writes one markdown file per session under $MEM_HOME/sessions/<date>/.
# Idempotent: re-running skips duplicates (ingest-session is idempotent).

set -u

DAYS="${1:-7}"
PROJECT_DIR="${PROJECT_DIR:-$HOME/.claude/projects/-home-mike}"
MEMFS_BIN="${MEMFS_BIN:-$HOME/.local/bin/memfs}"
export MEM_HOME="${MEM_HOME:-$HOME/.config/karpathy}"

if [[ ! -x "$MEMFS_BIN" ]]; then
  echo "ERROR: memfs binary not found at $MEMFS_BIN" >&2
  exit 1
fi
if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "ERROR: project dir not found: $PROJECT_DIR" >&2
  exit 1
fi

echo "Backfill: scanning $PROJECT_DIR for jsonl modified within $DAYS days"
echo "MEM_HOME = $MEM_HOME"

total=0
ok=0
fail=0
dup=0
while IFS= read -r -d '' jsonl; do
  total=$((total+1))
  result=$("$MEMFS_BIN" ingest-session "$jsonl" 2>&1 || true)
  if printf '%s' "$result" | grep -q '"ok": true'; then
    ok=$((ok+1))
    if printf '%s' "$result" | grep -q '"duplicate": true'; then
      dup=$((dup+1))
    fi
  else
    fail=$((fail+1))
    echo "  FAIL $jsonl : $result" >&2
  fi
done < <(find "$PROJECT_DIR" -maxdepth 1 -name "*.jsonl" -mtime -"$DAYS" -print0 2>/dev/null)

echo ""
echo "Backfill complete."
echo "  scanned:     $total"
echo "  ingested ok: $ok  (of which re-ingested duplicates: $dup)"
echo "  failed:      $fail"
