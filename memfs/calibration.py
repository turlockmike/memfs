"""Calibration ledger — record verifiable claims, verify outcomes, report curves.

Storage: dual write to (:Claim) nodes in Neo4j PLUS append-only JSONL at
`<MEM_HOME>/.mem/calibration.jsonl`. Dual write is idempotent — the JSONL
is the durable record, Neo4j is the queryable cache.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path(mem_home: str) -> str:
    return os.path.join(mem_home, ".mem", "calibration.jsonl")


def _append_ledger(mem_home: str, record: dict) -> None:
    path = _ledger_path(mem_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_claim(graph, *, text: str, confidence: float, scope: str,
                 claimed_to: str = "log", mem_home: str | None = None) -> str:
    """Record a new claim. Returns claim_id (UUID)."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    if not text or not text.strip():
        raise ValueError("text is required")
    if not scope or not scope.strip():
        raise ValueError("scope is required")

    claim_id = str(uuid.uuid4())
    now = _now()

    graph.run(
        """CREATE (c:Claim {
             id: $id, text: $text, confidence: $conf, scope: $scope,
             claimed_at: $now, claimed_to: $to,
             verified_at: null, outcome: 'unverified'
           })""",
        id=claim_id, text=text, conf=float(confidence), scope=scope,
        now=now, to=claimed_to,
    )

    if mem_home:
        _append_ledger(mem_home, {
            "event": "claim",
            "id": claim_id, "text": text, "confidence": confidence,
            "scope": scope, "claimed_to": claimed_to, "claimed_at": now,
        })

    return claim_id


def verify_claim(graph, *, claim_id: str, outcome: str,
                 note: str | None = None,
                 mem_home: str | None = None) -> None:
    """Mark a claim as verified with outcome correct|wrong|partial."""
    if outcome not in ("correct", "wrong", "partial"):
        raise ValueError(f"outcome must be one of correct|wrong|partial, got {outcome!r}")

    now = _now()
    result = graph.run(
        """MATCH (c:Claim {id: $id})
           SET c.verified_at = $now, c.outcome = $outcome,
               c.note = coalesce($note, c.note)
           RETURN c.id AS id""",
        id=claim_id, now=now, outcome=outcome, note=note,
    )
    if not result:
        raise KeyError(f"claim not found: {claim_id}")

    if mem_home:
        _append_ledger(mem_home, {
            "event": "verify",
            "id": claim_id, "outcome": outcome, "note": note,
            "verified_at": now,
        })


def calibration_curve(graph, *, window_days: int = 30,
                      scope: str | None = None) -> dict:
    """Return the calibration curve over the last `window_days`.

    Buckets verified claims by confidence (0.0-1.0 in 0.1 bins), then computes
    actual correctness rate per bin. A well-calibrated agent has
    correctness_rate ≈ bin_center.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()

    cypher = (
        "MATCH (c:Claim) "
        "WHERE c.verified_at IS NOT NULL "
        "  AND c.verified_at >= $cutoff "
        + ("  AND c.scope = $scope " if scope else "")
        + "RETURN c.confidence AS confidence, c.outcome AS outcome, "
        "c.scope AS scope"
    )
    params = {"cutoff": cutoff}
    if scope:
        params["scope"] = scope

    rows = graph.run(cypher, **params)

    # Bins: [0.0-0.1), [0.1-0.2), ..., [0.9-1.0]
    bins = defaultdict(lambda: {"n": 0, "correct": 0, "partial": 0, "wrong": 0})
    for row in rows:
        conf = row["confidence"] or 0.0
        bin_key = min(int(conf * 10) / 10.0, 0.9)  # 0.0, 0.1, ..., 0.9
        bins[bin_key]["n"] += 1
        outcome = row["outcome"]
        if outcome == "correct":
            bins[bin_key]["correct"] += 1
        elif outcome == "partial":
            bins[bin_key]["partial"] += 1
        elif outcome == "wrong":
            bins[bin_key]["wrong"] += 1

    curve = []
    for bin_start in sorted(bins.keys()):
        b = bins[bin_start]
        if b["n"] > 0:
            # Correct + 0.5 * partial as the observed accuracy
            obs = (b["correct"] + 0.5 * b["partial"]) / b["n"]
        else:
            obs = 0.0
        curve.append({
            "bin_low": round(bin_start, 2),
            "bin_high": round(bin_start + 0.1, 2),
            "n": b["n"],
            "correct": b["correct"],
            "partial": b["partial"],
            "wrong": b["wrong"],
            "observed_accuracy": round(obs, 4),
        })

    total = sum(b["n"] for b in bins.values())
    total_correct = sum(b["correct"] for b in bins.values())
    total_partial = sum(b["partial"] for b in bins.values())

    # Expected Calibration Error (weighted mean abs diff between conf and obs)
    ece = 0.0
    if total > 0:
        for row in curve:
            mid = (row["bin_low"] + row["bin_high"]) / 2
            ece += (row["n"] / total) * abs(mid - row["observed_accuracy"])

    return {
        "window_days": window_days,
        "scope": scope,
        "total_verified": total,
        "total_correct": total_correct,
        "total_partial": total_partial,
        "overall_accuracy": round((total_correct + 0.5 * total_partial) / total, 4) if total else 0.0,
        "expected_calibration_error": round(ece, 4),
        "curve": curve,
    }


def rebuild_from_ledger(graph, *, mem_home: str) -> dict:
    """Replay the JSONL ledger to rebuild Claim nodes in Neo4j.

    JSONL is the durable record (append-only, survives DB wipes). Neo4j
    is the queryable cache. When they drift (observed Apr 17 after the
    conftest wipe incident), this replays the ledger forward to restore
    the cache.

    Idempotent: claims use MERGE on id. Verifies SET only if verified_at
    is null or differs, preserving the first verification.

    Returns counts: {claims_seen, claims_written, verifies_seen,
    verifies_applied, verifies_skipped_no_claim}.
    """
    path = _ledger_path(mem_home)
    if not os.path.exists(path):
        return {"claims_seen": 0, "claims_written": 0,
                "verifies_seen": 0, "verifies_applied": 0,
                "verifies_skipped_no_claim": 0}

    stats = {"claims_seen": 0, "claims_written": 0,
             "verifies_seen": 0, "verifies_applied": 0,
             "verifies_skipped_no_claim": 0}

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event")

            if event == "claim":
                stats["claims_seen"] += 1
                cid = rec.get("id")
                if not cid:
                    continue
                # MERGE idempotently on id; first-writer-wins on fields
                graph.run(
                    """MERGE (c:Claim {id: $id})
                       ON CREATE SET c.text = $text, c.confidence = $conf,
                                     c.scope = $scope, c.claimed_at = $claimed_at,
                                     c.claimed_to = $to, c.outcome = 'unverified',
                                     c.verified_at = null
                       ON MATCH SET c.text = coalesce(c.text, $text),
                                    c.confidence = coalesce(c.confidence, $conf),
                                    c.scope = coalesce(c.scope, $scope),
                                    c.claimed_at = coalesce(c.claimed_at, $claimed_at),
                                    c.claimed_to = coalesce(c.claimed_to, $to)""",
                    id=cid,
                    text=rec.get("text", ""),
                    conf=float(rec.get("confidence", 0.0)),
                    scope=rec.get("scope", ""),
                    claimed_at=rec.get("claimed_at", ""),
                    to=rec.get("claimed_to", "log"),
                )
                stats["claims_written"] += 1

            elif event == "verify":
                stats["verifies_seen"] += 1
                cid = rec.get("id")
                if not cid:
                    continue
                result = list(graph.run(
                    """MATCH (c:Claim {id: $id})
                       SET c.verified_at = coalesce(c.verified_at, $va),
                           c.outcome = CASE
                             WHEN c.outcome = 'unverified' OR c.outcome IS NULL
                             THEN $outcome ELSE c.outcome END,
                           c.note = coalesce(c.note, $note)
                       RETURN c.id AS id""",
                    id=cid, va=rec.get("verified_at", ""),
                    outcome=rec.get("outcome", "unverified"),
                    note=rec.get("note"),
                ))
                if result:
                    stats["verifies_applied"] += 1
                else:
                    stats["verifies_skipped_no_claim"] += 1

    return stats
