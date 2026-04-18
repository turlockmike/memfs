"""Contradiction detection for layer 3+ nodes.

Two-stage approach:
1. HEURISTIC pre-filter (fast, keyword-based): token overlap + reversal-bigram
   or negation-asymmetry check. Catches candidate pairs cheaply.
2. SEMANTIC judge (LLM, via `infer -r contradiction-judge`): verifies that the
   candidate is a real contradiction about the same subject, not lexical noise.
   Returns (False, ...) by default when infer is unavailable — trading recall
   for precision (trust-erosion prevention is the whole point).

The heuristic was M4's shipped form. The semantic stage was added 2026-04-18
after a coverage-level scan showed the heuristic alone was 0% precision on
Karpathy retrospectives (12 conflicts, 0 true positives) — the `correct/wrong`
bigram fires on shared vocabulary between any two retrospective documents
without subject scoping.

Env overrides:
- MEMFS_CONTRADICTION_SKIP_SEMANTIC=1 — bypass semantic check (heuristic-only).
  Useful in tests and when infer is not installed. Default behavior auto-detects
  infer availability and falls back to heuristic-only if missing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
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
    """Heuristic pre-filter. Return (is_suspected, reason).

    Cheap token-level check. Does NOT confirm the candidate is a real
    contradiction — only flags it for further checking by _semantic_contradiction.
    """
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


def _extract_judge_json(output: str) -> dict | None:
    """Pull the first JSON object out of infer's stdout.

    Models sometimes wrap JSON in commentary, code fences, or trailing text.
    This strips conservatively: finds the first `{` and last `}`, parses.
    Returns None if unparseable.
    """
    if not output:
        return None
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    candidate = output[start:end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _semantic_contradiction(text_a: str, text_b: str, *,
                             timeout_seconds: float = 15.0) -> tuple[bool, str]:
    """Semantic tie-breaker via `infer -r contradiction-judge`.

    Returns (contradicts, subject). On any failure (infer missing, subprocess
    error, parse failure, timeout) returns (False, "semantic_error:<reason>")
    — safe default is NO edge.

    Bypassed entirely if MEMFS_CONTRADICTION_SKIP_SEMANTIC=1 (returns True so
    the heuristic decision stands — test mode).

    Truncates passages to 2000 chars each to keep prompt under control.
    """
    if os.environ.get("MEMFS_CONTRADICTION_SKIP_SEMANTIC") == "1":
        # Test-mode bypass: pretend semantic agrees with heuristic
        return True, "semantic_bypassed"

    infer_bin = shutil.which("infer")
    if not infer_bin:
        return False, "semantic_error:infer_not_found"

    # Check the role file exists — otherwise infer will fail loudly on every call
    role_path = os.path.expanduser("~/.config/infer/roles/contradiction-judge.md")
    if not os.path.exists(role_path):
        return False, "semantic_error:role_missing"

    passage_a = (text_a or "")[:2000]
    passage_b = (text_b or "")[:2000]
    prompt_stdin = f"PASSAGE A:\n{passage_a}\n\nPASSAGE B:\n{passage_b}\n"

    try:
        proc = subprocess.run(
            [infer_bin, "-r", "contradiction-judge",
             "Emit the JSON verdict for these two passages."],
            input=prompt_stdin,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, "semantic_error:timeout"
    except Exception as exc:
        return False, f"semantic_error:exec:{type(exc).__name__}"

    # infer exits 1 on success per a known quirk; always check stdout
    parsed = _extract_judge_json(proc.stdout)
    if parsed is None:
        return False, "semantic_error:parse"

    contradicts = bool(parsed.get("contradicts"))
    subject = str(parsed.get("subject") or "") if parsed.get("subject") else ""
    return contradicts, subject


def scan_corpus(graph, *, overlap_threshold: float = 0.35,
                candidate_limit: int = 10) -> dict:
    """Run contradiction detection across all layer-3+ nodes in the corpus.

    The watcher-path invokes detect_contradictions() incrementally as files
    are written. This function batch-runs the same detection across every
    existing layer-3+ node — useful after a reindex (which doesn't trigger
    the watcher) or periodically as a consistency scan.

    Returns a dict with counts + list of conflict dicts:
      {"scanned": N, "conflicts": [...], "edges_created": K}

    Idempotent: underlying MERGE on the [:CONTRADICTS] edge means re-scans
    don't double-count. Bidirectional edges are counted per-direction in
    Neo4j but the same pair in the conflicts list once.
    """
    # Find all layer-3+ nodes
    paths = [row["path"] for row in graph.run(
        "MATCH (n:Node) WHERE n.layer IS NOT NULL AND n.layer >= 3 "
        "RETURN n.path AS path ORDER BY n.path"
    )]

    # Before-edge count to derive edges_created (accounts for idempotent MERGE)
    before = graph.run_one(
        "MATCH ()-[r:CONTRADICTS]->() RETURN count(r) AS n"
    )["n"]

    conflicts = []
    seen_pairs: set[tuple[str, str]] = set()
    for p in paths:
        for c in detect_contradictions(
            graph, p,
            overlap_threshold=overlap_threshold,
            candidate_limit=candidate_limit,
        ):
            # Dedupe pair ordering — the detector runs both directions by
            # construction (A->B and later B->A would both return the pair).
            key = tuple(sorted((c["new"], c["existing"])))
            if key not in seen_pairs:
                seen_pairs.add(key)
                conflicts.append(c)

    after = graph.run_one(
        "MATCH ()-[r:CONTRADICTS]->() RETURN count(r) AS n"
    )["n"]

    return {
        "scanned": len(paths),
        "conflicts": conflicts,
        "edges_created": after - before,
    }


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

        # Semantic tie-breaker: LLM verifies same-subject opposite claims.
        # Safe default: if the judge says no, or returns any error, DO NOT emit
        # an edge. Precision over recall — a noisy detector erodes trust.
        semantic_ok, subject = _semantic_contradiction(query_text, other_text)
        if not semantic_ok:
            continue

        # Combine heuristic reason + semantic subject for downstream debugging
        final_reason = reason
        if subject and subject not in {"semantic_bypassed", ""}:
            final_reason = f"{reason}|subject:{subject[:120]}"

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
            reason=final_reason, overlap=float(overlap),
        )

        conflicts.append({
            "new": new_node_path,
            "existing": cand["path"],
            "reason": final_reason,
            "overlap": round(overlap, 3),
            "detected_at": now,
        })

    return conflicts
