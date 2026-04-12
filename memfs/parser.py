"""File parsing — frontmatter extraction, link detection, content hashing."""

import hashlib
import json
import os
import re
from typing import Optional

import yaml


LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")

# Text-like keys to extract from JSONL objects
_TEXT_KEYS = {"msg", "message", "content", "text", "description", "title", "name",
              "question", "answer", "summary", "body", "comment", "note"}
HEADING_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _parse_jsonl(filepath: str, raw: str, content_hash: str) -> dict:
    """Parse a JSONL file — extract text fields from each line for indexing."""
    title = os.path.splitext(os.path.basename(filepath))[0]
    text_parts = []

    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                for key in _TEXT_KEYS:
                    val = obj.get(key)
                    if isinstance(val, str) and val:
                        text_parts.append(val)
        except json.JSONDecodeError:
            continue

    content = "\n".join(text_parts) if text_parts else raw[:5000]

    return {
        "title": title,
        "description": None,
        "date_hint": None,
        "content": content,
        "content_hash": content_hash,
        "links": [],
        "frontmatter": {},
    }


def compute_hash(content: str) -> str:
    """SHA-256 hash of content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def extract_links(content: str) -> list[str]:
    """Extract all [[wikilink]] targets from content. Deduplicates."""
    matches = LINK_PATTERN.findall(content)
    seen = set()
    result = []
    for m in matches:
        m = m.strip()
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def parse_file(filepath: str) -> dict:
    """Parse a markdown file, extracting frontmatter, title, content, links, and hash.

    Returns dict with keys:
        title, date_hint, content, content_hash, links, frontmatter
    """
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read()

    content_hash = compute_hash(raw)

    # Handle JSONL files — extract text fields from each line
    if filepath.endswith(".jsonl"):
        return _parse_jsonl(filepath, raw, content_hash)

    frontmatter = {}
    content = raw

    # Extract YAML frontmatter
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                frontmatter = {}
            content = parts[2].strip()

    # Extract title: frontmatter > first heading > filename
    title = frontmatter.get("title")
    if not title:
        heading_match = HEADING_PATTERN.search(content)
        if heading_match:
            title = heading_match.group(1).strip()
    if not title:
        title = os.path.splitext(os.path.basename(filepath))[0]

    # Extract description (capped at 200 chars)
    description: Optional[str] = frontmatter.get("description")
    if description:
        description = str(description)[:200]

    # Extract date hint
    date_hint: Optional[str] = None
    date_val = frontmatter.get("date")
    if date_val is not None:
        date_hint = str(date_val)

    # Extract links
    links = extract_links(content)

    return {
        "title": title,
        "description": description,
        "date_hint": date_hint,
        "content": content,
        "content_hash": content_hash,
        "links": links,
        "frontmatter": frontmatter,
    }
