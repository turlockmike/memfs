"""Session ingestion — parse Claude Code session jsonl transcripts into memfs nodes.

Each session produces ONE distilled markdown file under
`<MEM_HOME>/sessions/<YYYY-MM-DD>/<session-id-short>.md`. The watcher then
auto-indexes it.

Idempotent: the filename is derived from the session_id, so re-running on the
same jsonl produces the same file (content rewritten on change).

Layer: sessions are always layer 2 (operational episodic memory).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Iterator

# Keys commonly holding human/assistant text on a `type: user` or
# `type: assistant` content block.
_TEXT_BLOCK_TYPES = ("text",)

# Truncate lengths
_MAX_SUMMARY_BYTES = 2048
_FIRST_PROMPT_CAP = 200
_USER_PROMPT_CAP = 400
_ASSISTANT_TEXT_CAP = 300

# Patterns that indicate a user message is a system/automation wrapper, not a
# real human prompt. These are filtered from user_prompts.
_SYSTEM_USER_PATTERNS = (
    "Session rotated",
    "<system-reminder>",
    "Caveat: The messages below were generated",
    "[Request interrupted",
)


def _iter_jsonl(path: str) -> Iterator[dict]:
    """Yield dict entries from a jsonl file, skipping malformed lines."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _is_system_user_msg(text: str) -> bool:
    head = text[:200]
    for p in _SYSTEM_USER_PATTERNS:
        if p in head:
            return True
    return False


def _extract_user_text(entry: dict) -> str | None:
    """Pull user-visible text out of a `type: user` entry. Returns None if
    it's a system wrapper / tool_result / empty."""
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
        text = "\n".join(parts).strip()
    else:
        return None
    if not text:
        return None
    if _is_system_user_msg(text):
        return None
    return text


def _iter_assistant_blocks(entry: dict) -> Iterator[dict]:
    msg = entry.get("message") or {}
    content = msg.get("content") or []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict):
                yield c


def _extract_file_path_from_input(inp) -> str | None:
    if not isinstance(inp, dict):
        return None
    for key in ("file_path", "path", "notebook_path"):
        v = inp.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _count_minutes(start_iso: str | None, end_iso: str | None) -> int | None:
    if not start_iso or not end_iso:
        return None
    try:
        a = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (b - a).total_seconds() / 60.0
    return max(0, int(round(delta)))


def distill_session(jsonl_path: str) -> dict | None:
    """Parse a session jsonl and return a distilled summary dict.

    Returns None if the file is empty / has no session_id.
    """
    session_id: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    agent_setting: str | None = None
    version: str | None = None

    user_prompts: list[str] = []
    first_user_prompt: str | None = None
    assistant_texts: list[str] = []
    tool_name_counts: dict[str, int] = {}
    files_touched: set[str] = set()
    tool_call_count = 0

    for entry in _iter_jsonl(jsonl_path):
        t = entry.get("type")
        ts = entry.get("timestamp")
        sid = entry.get("sessionId")

        if sid and not session_id:
            session_id = sid
        if isinstance(ts, str):
            if not start_ts or ts < start_ts:
                start_ts = ts
            if not end_ts or ts > end_ts:
                end_ts = ts
        if not cwd and entry.get("cwd"):
            cwd = entry.get("cwd")
        if not git_branch and entry.get("gitBranch"):
            git_branch = entry.get("gitBranch")
        if not version and entry.get("version"):
            version = entry.get("version")
        if t == "agent-setting" and not agent_setting:
            agent_setting = entry.get("agentSetting")

        if t == "user":
            text = _extract_user_text(entry)
            if text:
                user_prompts.append(text)
                if first_user_prompt is None:
                    first_user_prompt = text
        elif t == "assistant":
            for c in _iter_assistant_blocks(entry):
                ct = c.get("type")
                if ct == "text":
                    txt = c.get("text")
                    if isinstance(txt, str) and txt.strip():
                        assistant_texts.append(txt.strip())
                elif ct == "tool_use":
                    tool_call_count += 1
                    name = c.get("name")
                    if isinstance(name, str):
                        tool_name_counts[name] = tool_name_counts.get(name, 0) + 1
                    fp = _extract_file_path_from_input(c.get("input"))
                    if fp:
                        files_touched.add(fp)

    if not session_id:
        return None

    duration_min = _count_minutes(start_ts, end_ts)

    return {
        "session_id": session_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "duration_minutes": duration_min,
        "cwd": cwd,
        "git_branch": git_branch,
        "agent_setting": agent_setting,
        "version": version,
        "user_prompts": user_prompts,
        "first_user_prompt": first_user_prompt,
        "assistant_texts": assistant_texts,
        "tool_name_counts": tool_name_counts,
        "tool_call_count": tool_call_count,
        "files_touched": sorted(files_touched),
    }


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rstrip() + "…"


def _session_date(distilled: dict) -> str:
    """YYYY-MM-DD — prefer start_ts, fallback to today (UTC)."""
    ts = distilled.get("start_ts")
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _yaml_quote(s: str | None) -> str:
    """Single-line YAML-safe string, double-quoted with escapes."""
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{s}"'


def render_summary_md(distilled: dict) -> str:
    """Render a compact markdown file from the distilled dict.

    Kept under ~2KB: prompts trimmed, tool list collapsed to names+counts,
    assistant narrative truncated to last handful of short turns.
    """
    sid = distilled["session_id"]
    date = _session_date(distilled)
    duration = distilled.get("duration_minutes")
    first_prompt = distilled.get("first_user_prompt") or ""
    first_prompt_short = _truncate(first_prompt.replace("\n", " ").strip(), _FIRST_PROMPT_CAP)

    title_src = first_prompt_short or f"session {sid[:8]}"
    title = _truncate(title_src, 80)

    tool_items = sorted(
        distilled.get("tool_name_counts", {}).items(),
        key=lambda x: (-x[1], x[0]),
    )
    tool_summary = ", ".join(f"{n}×{c}" for n, c in tool_items) or "none"

    user_count = len(distilled.get("user_prompts", []))

    # Frontmatter
    lines: list[str] = []
    lines.append("---")
    lines.append(f"title: {_yaml_quote(title)}")
    lines.append(f"date: {date}")
    lines.append("layer: 2")
    lines.append(f"session_id: {_yaml_quote(sid)}")
    if duration is not None:
        lines.append(f"duration_minutes: {duration}")
    lines.append(f"user_prompt_count: {user_count}")
    lines.append(f"tool_call_count: {distilled.get('tool_call_count', 0)}")
    lines.append(f"first_prompt: {_yaml_quote(first_prompt_short)}")
    if distilled.get("agent_setting"):
        lines.append(f"agent_setting: {_yaml_quote(distilled['agent_setting'])}")
    if distilled.get("cwd"):
        lines.append(f"cwd: {_yaml_quote(distilled['cwd'])}")
    if distilled.get("git_branch"):
        lines.append(f"git_branch: {_yaml_quote(distilled['git_branch'])}")
    lines.append("---")
    lines.append("")

    lines.append(f"# Session {sid[:8]} — {date}")
    lines.append("")
    if first_prompt_short:
        lines.append(f"**First prompt:** {first_prompt_short}")
        lines.append("")
    lines.append(f"**Tools:** {tool_summary}")
    lines.append("")

    # User prompts (joined, short)
    if user_count > 1:
        lines.append("## User prompts")
        for p in distilled["user_prompts"][:15]:
            snippet = _truncate(p.replace("\n", " ").strip(), _USER_PROMPT_CAP)
            lines.append(f"- {snippet}")
        if user_count > 15:
            lines.append(f"- …({user_count - 15} more)")
        lines.append("")

    # Files touched (capped)
    files = distilled.get("files_touched", [])
    if files:
        lines.append("## Files touched")
        for f in files[:25]:
            lines.append(f"- `{f}`")
        if len(files) > 25:
            lines.append(f"- …({len(files) - 25} more)")
        lines.append("")

    # Assistant narrative — sample early + late
    a_texts = distilled.get("assistant_texts", [])
    if a_texts:
        lines.append("## Assistant narrative")
        # Grab up to 3 early + 3 late text blocks, each capped
        sample = a_texts[:3] + (a_texts[-3:] if len(a_texts) > 3 else [])
        seen = set()
        for t in sample:
            key = t[:60]
            if key in seen:
                continue
            seen.add(key)
            snippet = _truncate(t.replace("\n", " ").strip(), _ASSISTANT_TEXT_CAP)
            if snippet:
                lines.append(f"- {snippet}")
        lines.append("")

    body = "\n".join(lines)

    # Hard cap the total summary (post-frontmatter budget)
    if len(body) > _MAX_SUMMARY_BYTES * 3:
        body = body[: _MAX_SUMMARY_BYTES * 3] + "\n\n[...truncated...]\n"
    return body


def sessions_dir(mem_home: str) -> str:
    return os.path.join(mem_home, "sessions")


def session_output_path(mem_home: str, distilled: dict) -> str:
    date = _session_date(distilled)
    sid_short = distilled["session_id"][:8]
    return os.path.join(sessions_dir(mem_home), date, f"{sid_short}.md")


def ingest_session(jsonl_path: str, mem_home: str) -> dict:
    """Distill + write the session summary. Returns an NDJSON-ready dict.

    Idempotent: filename derived from session_id, content is rewritten (it
    may change if the same jsonl is re-parsed after more lines were appended).
    """
    distilled = distill_session(jsonl_path)
    if distilled is None:
        return {
            "action": "ingest",
            "ok": False,
            "reason": "empty_or_unparseable",
            "jsonl_path": jsonl_path,
        }

    out_path = session_output_path(mem_home, distilled)
    existed = os.path.exists(out_path)
    body = render_summary_md(distilled)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    # Atomic write: tmp then rename
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, out_path)

    return {
        "action": "ingest",
        "ok": True,
        "session_id": distilled["session_id"],
        "node_path": out_path,
        "tokens_in_summary": len(body) // 4,  # cheap estimate
        "duplicate": existed,
        "user_prompt_count": len(distilled["user_prompts"]),
        "tool_call_count": distilled["tool_call_count"],
        "files_touched_count": len(distilled["files_touched"]),
    }
