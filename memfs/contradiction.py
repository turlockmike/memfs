"""Contradiction detection for layer 3+ nodes.

Heuristic approach (M4):
1. When a layer-3+ file is indexed, fulltext-search its content for existing
   layer-3+ nodes covering overlapping topics.
2. For each high-overlap match (score > threshold), look for negation markers
   that suggest contradiction (e.g. "no", "never", "not", "wrong", "incorrect",
   opposing quantifiers).
3. If suspected, create a [:CONTRADICTS] relationship and emit an NDJSON
   `conflict` event.

This is intentionally dumb — better than nothing, much worse than semantic.
The design lets a future semantic adapter slot in via detect_contradictions()
while keeping the observable interface stable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from memfs import graph as graph_mod
from memfs.search import _escape_lucene


_NEGATION_TOKENS = {
    "no", "not", "never", "nope", "cannot", "can't", "won't", "isn't", "wasn't",
    "aren't", "weren't", "doesn't", "didn't", "wrong", "incorrect", "false",
    "denied", "deprecated", "obsolete", "superseded", "forbidden",
}

# Very rough "contradiction bigrams" that suggest a reversal between two texts
_REVERSAL_BIGRAMS = {
    ("always", "never"), ("never", "always"),
    ("correct", "wrong"), ("wrong", "correct"),
    ("enabled", "disabled"), ("disabled", "enabled"),
    ("supported", "unsupported"), ("unsupported", "supported"),
    ("works", "broken"), ("broken", "works"),
    ("active", "stopped"), ("stopped", "active"),
    ("true", "false"), ("false", "true"),
}


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _overlap_ratio(a: str, b: str) -> float:
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _suspected_contradiction(text_a: str, text_b: str) -> tuple[bool, str]:
    """Return (is_suspected, reason)."""
    ta = _tokenize(text_a)
    tb = _tokenize(text_b)

    # Reversal bigrams
    for pos, neg in _REVERSAL_BIGRAMS:
        if pos in ta and neg in tb:
            return True, f"reversal:{pos}->{neg}"
        if neg in ta and pos in tb:
            return True, f"reversal:{neg}->{pos}"

    # Negation asymmetry: one mentions negation, other doesn't, but they share
    # significant topic overlap
    a_has_neg = bool(ta & _NEGATION_TOKENS)
    b_has_neg = bool(tb & _NEGATION_TOKENS)
    if a_has_neg != b_has_neg:
        overlap = _overlap_ratio(text_a, text_b)
        if overlap > 0.35:
            return True, f"negation_asymmetry:overlap={overlap:.2f}"

    return False, ""


def detect_contradictions(graph, new_node_path: str,
                          *, overlap_threshold: float = 0.35,
                          candidate_limit: int = 10) -> list[dict]:
    """Detect contradictions for a newly indexed layer-3+ node.

    Returns a list of conflict dicts. Also creates [:CONTRADICTS] edges.
    """
    # Fetch the new node
    node = graph_mod.get_node(graph, new_node_path)
    if not node:
        return []
    layer = node.get("layer") or 0
    if layer < 3:
        return []  # M4 only triggers at layer 3+

    content = node.get("content") or ""
    title = node.get("title") or ""
    query_text = f"{title} {content}".strip()
    if not query_text:
        return []

    lucene = _escape_lucene(query_text[:500])
    if not lucene:
        return []

    # Search for other layer-3+ nodes covering overlapping topics
    candidates = graph_mod.fulltext_search(graph, lucene, limit=candidate_limit)
    now = datetime.now(timezone.utc).isoformat()

    conflicts = []
    for cand in candidates:
        if cand["path"] == new_node_path:
            continue
        if (cand.get("layer") or 0) < 3:
            continue

        other_text = f"{cand.get('title') or ''} {cand.get('content') or ''}"
        if not other_text.strip():
            continue

        overlap = _overlap_ratio(query_text, other_text)
        if overlap < overlap_threshold:
            continue

        suspected, reason = _suspected_contradiction(query_text, other_text)
        if not suspected:
            continue

        # Create edge
        graph.run(
            """MATCH (a:Node {path: $ap})
               MATCH (b:Node {path: $bp})
               MERGE (a)-[r:CONTRADICTS]-(b)
               ON CREATE SET r.detected_at = $now,
                             r.adjudicated = false,
                             r.reason = $reason,
                             r.overlap = $overlap""",
            ap=new_node_path, bp=cand["path"], now=now,
            reason=reason, overlap=float(overlap),
        )

        conflicts.append({
            "new": new_node_path,
            "existing": cand["path"],
            "reason": reason,
            "overlap": round(overlap, 3),
            "detected_at": now,
        })

    return conflicts
