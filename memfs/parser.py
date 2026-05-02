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

# Frontmatter prefixes to strip when resolving links_to: targets to mem_home-relative
# paths. Journals and other artifacts often write home-relative paths
# (`.config/karpathy/areas/USER.md`) for human readability; memfs indexes
# paths relative to MEM_HOME (which IS `~/.config/karpathy/`), so the prefix
# must be stripped or the edge target won't resolve to an existing node.
# Configurable via MEMFS_FRONTMATTER_LINK_PREFIXES env (colon-separated).
_DEFAULT_LINK_PREFIXES = (".config/karpathy/",)


def _link_prefixes() -> tuple[str, ...]:
    raw = os.environ.get("MEMFS_FRONTMATTER_LINK_PREFIXES", "").strip()
    if not raw:
        return _DEFAULT_LINK_PREFIXES
    return tuple(p.strip() for p in raw.split(":") if p.strip())


def extract_frontmatter_links(frontmatter: dict) -> list[str]:
    """Extract link targets from frontmatter `links_to:` array.

    Strips configured home-relative prefixes (e.g. `.config/karpathy/`) so the
    targets resolve against MEM_HOME paths. Skips non-string entries, paths
    starting with `~` (un-resolvable here), and paths containing parens or
    other annotation noise (`USER.md (PREDICTIONS LEDGER #90)`).
    Deduplicates while preserving order.
    """
    fm = frontmatter or {}
    raw = fm.get("links_to")
    if not isinstance(raw, list):
        return []
    prefixes = _link_prefixes()
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        t = entry.strip()
        if not t:
            continue
        # Hard skips: tilde paths, directory-trailing-slash, parenthetical
        # annotations are noise, not parseable link targets.
        if t.startswith("~") or t.endswith("/") or "(" in t:
            continue
        for prefix in prefixes:
            if t.startswith(prefix):
                t = t[len(prefix):]
                break
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


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

    # Detect handcrafted-index marker. The string `<!-- handcrafted -->` in the
    # first 200 chars of the BODY (after frontmatter) marks an index.md file
    # that humans curate intentionally; memfs's index renderer must NOT
    # auto-overwrite these. The flag becomes a Node property so it's queryable.
    is_handcrafted = "<!-- handcrafted -->" in content[:200]

    # Extract date hint
    date_hint: Optional[str] = None
    date_val = frontmatter.get("date")
    if date_val is not None:
        date_hint = str(date_val)

    # Extract links: wikilinks from body + targets from frontmatter `links_to:`.
    # Frontmatter links are appended after wikilinks; dedup preserves first-seen order.
    body_links = extract_links(content)
    fm_links = extract_frontmatter_links(frontmatter)
    seen_links: set[str] = set()
    links: list[str] = []
    for t in body_links + fm_links:
        if t in seen_links:
            continue
        seen_links.add(t)
        links.append(t)

    # M2: Extract layer + source (validation happens in indexer.index_file)
    layer = frontmatter.get("layer")
    source = frontmatter.get("source")

    # M5: freshness stamps (coerce yaml datetime → ISO string)
    freshness_verified_at = frontmatter.get("freshness_verified_at")
    if freshness_verified_at is not None:
        freshness_verified_at = str(freshness_verified_at)
    freshness_source_url = frontmatter.get("freshness_source_url")
    if freshness_source_url is not None:
        freshness_source_url = str(freshness_source_url)
    freshness_stale_after_days = frontmatter.get("freshness_stale_after_days")
    if freshness_stale_after_days is not None:
        try:
            freshness_stale_after_days = int(freshness_stale_after_days)
        except (TypeError, ValueError):
            freshness_stale_after_days = None

    return {
        "title": title,
        "description": description,
        "date_hint": date_hint,
        "content": content,
        "content_hash": content_hash,
        "links": links,
        "frontmatter": frontmatter,
        "layer": layer,
        "source": source,
        "freshness_verified_at": freshness_verified_at,
        "freshness_source_url": freshness_source_url,
        "freshness_stale_after_days": freshness_stale_after_days,
        "is_handcrafted": is_handcrafted,
    }
