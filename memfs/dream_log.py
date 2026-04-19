"""Dream-pass output logging — close the loop on nightly consolidation.

The dream trigger fires candidates at Karpathy each night; this module records
what Karpathy did with them. Without this instrument, 29 candidates can land
and 0 get actioned and the system has no way to notice. Memory must be able
to watch its own maintenance.

Storage (files-as-truth):
  * ``.mem/dream-log.jsonl`` — durable append-only ledger. Source of truth.
  * ``(:DreamRun)`` nodes + ``(:DreamRun)-[:HAS_ACTION]->(:DreamAction)`` edges
    in Neo4j — derived queryable cache. Rebuildable from the ledger via
    ``rebuild_from_ledger``.

A "run" is a single nightly pass (run_id = caller-supplied, typically an ISO
timestamp). Each run has:
  * ``started_at`` timestamp
  * ``candidates_by_type`` — input count per candidate_type (orphan/merge/...)
  * ``actions`` — list of actions the agent took (merge/split/link/defer/ignore)
  * ``claim_ids`` — calibration-ledger claim IDs logged during the pass
  * ``status_delta`` — free-form dict, post-pass state (e.g. node/edge counts)
  * ``finished_at`` timestamp (optional; a run without finish is "in progress")

Actions have a ``candidate_type`` (what kind of candidate was being actioned)
so that "recurring ignored" detection can match. If the same candidate
(nodes tuple) shows up N nights in a row with action="defer" or "ignore",
the report surfaces it — either it's genuinely low-value or the agent keeps
punting a real decision.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta


VALID_ACTIONS = ("merge", "split", "link", "defer", "ignore", "archive", "other")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ledger_path(mem_home: str) -> str:
    return os.path.join(mem_home, ".mem", "dream-log.jsonl")


def _append_ledger(mem_home: str, record: dict) -> None:
    path = _ledger_path(mem_home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _nodes_key(nodes: list[str] | None) -> str:
    """Stable hashable key for a candidate's node-tuple, used for
    recurring-ignored detection. Sorts members so [a,b] and [b,a] collapse."""
    if not nodes:
        return ""
    return "\x1f".join(sorted(str(n) for n in nodes))


# -------- recording --------

def start_run(graph, *, run_id: str,
              candidates_by_type: dict | None = None,
              mem_home: str | None = None) -> None:
    """Begin recording a dream pass. Creates (:DreamRun {id, started_at,
    candidates_by_type_json}). Idempotent on run_id (MERGE).

    ``candidates_by_type`` is the input side of the pass — a map like
    ``{"orphan": 5, "merge": 10, "dead_weight": 3}``. Stored as a JSON
    string on the node (Neo4j property types don't do nested objects
    ergonomically) so readers always parse it.
    """
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required")

    cbt = candidates_by_type or {}
    if not isinstance(cbt, dict):
        raise ValueError(f"candidates_by_type must be a dict, got {type(cbt).__name__}")
    # Sanitize counts to ints
    cbt_clean = {str(k): int(v) for k, v in cbt.items()}
    total_candidates = sum(cbt_clean.values())

    now = _now()
    graph.run(
        """MERGE (r:DreamRun {id: $id})
           ON CREATE SET r.started_at = $now,
                         r.candidates_by_type_json = $cbt,
                         r.total_candidates = $total,
                         r.status_delta_json = null,
                         r.finished_at = null
           ON MATCH  SET r.candidates_by_type_json = coalesce(r.candidates_by_type_json, $cbt),
                         r.total_candidates = coalesce(r.total_candidates, $total)""",
        id=run_id, now=now, cbt=json.dumps(cbt_clean), total=total_candidates,
    )

    if mem_home:
        _append_ledger(mem_home, {
            "event": "start",
            "run_id": run_id,
            "started_at": now,
            "candidates_by_type": cbt_clean,
        })


def record_action(graph, *, run_id: str, action: str,
                  candidate_type: str,
                  nodes: list[str] | None = None,
                  claim_id: str | None = None,
                  note: str | None = None,
                  mem_home: str | None = None) -> str:
    """Record one action taken during a dream pass.

    ``action`` is one of ``merge|split|link|defer|ignore|archive|other``.
    ``candidate_type`` is the dream-briefing candidate type being actioned
    (``orphan|merge|split|link|stale|dead_weight|index|...``). ``nodes`` is
    the candidate's node tuple. ``claim_id`` optionally links this action
    to a calibration-ledger claim recorded for the same decision.

    Returns the action_id (for cross-referencing). Auto-starts the run if
    the caller didn't call ``start_run`` first, so the trigger prompt can
    just fire action rows without ceremony.
    """
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required")
    if action not in VALID_ACTIONS:
        raise ValueError(
            f"action must be one of {VALID_ACTIONS}, got {action!r}"
        )
    if not candidate_type or not str(candidate_type).strip():
        raise ValueError("candidate_type is required")

    nodes_list = [str(n) for n in (nodes or [])]
    import uuid
    action_id = uuid.uuid4().hex
    now = _now()

    # Ensure the DreamRun exists (auto-start convenience for the trigger
    # prompt — Karpathy can fire actions without an explicit start).
    graph.run(
        """MERGE (r:DreamRun {id: $id})
           ON CREATE SET r.started_at = $now,
                         r.candidates_by_type_json = '{}',
                         r.total_candidates = 0,
                         r.status_delta_json = null,
                         r.finished_at = null""",
        id=run_id, now=now,
    )
    graph.run(
        """MATCH (r:DreamRun {id: $rid})
           CREATE (a:DreamAction {
             id: $aid, run_id: $rid, action: $action,
             candidate_type: $ctype, nodes_key: $nkey,
             nodes_json: $nodes_json, claim_id: $claim_id,
             note: $note, recorded_at: $now
           })
           CREATE (r)-[:HAS_ACTION]->(a)""",
        rid=run_id, aid=action_id, action=action, ctype=candidate_type,
        nkey=_nodes_key(nodes_list), nodes_json=json.dumps(nodes_list),
        claim_id=claim_id, note=note, now=now,
    )

    if mem_home:
        rec = {
            "event": "action",
            "run_id": run_id,
            "action_id": action_id,
            "action": action,
            "candidate_type": candidate_type,
            "nodes": nodes_list,
            "recorded_at": now,
        }
        if claim_id is not None:
            rec["claim_id"] = claim_id
        if note is not None:
            rec["note"] = note
        _append_ledger(mem_home, rec)

    return action_id


def finish_run(graph, *, run_id: str,
               status_delta: dict | None = None,
               mem_home: str | None = None) -> None:
    """Finalize a dream pass. Stamps ``finished_at`` and stores the
    ``status_delta`` (arbitrary JSON-serializable dict) on the node.

    Typical ``status_delta`` entries::

        {"nodes_before": 197, "nodes_after": 192,
         "edges_before": 120, "edges_after": 128,
         "claims_logged": 3}
    """
    if not run_id or not str(run_id).strip():
        raise ValueError("run_id is required")

    sd = status_delta or {}
    if not isinstance(sd, dict):
        raise ValueError(f"status_delta must be a dict, got {type(sd).__name__}")

    now = _now()
    result = graph.run(
        """MATCH (r:DreamRun {id: $id})
           SET r.finished_at = $now,
               r.status_delta_json = $sd
           RETURN r.id AS id""",
        id=run_id, now=now, sd=json.dumps(sd),
    )
    if not result:
        # Auto-create (mirrors record_action's forgiving semantics).
        graph.run(
            """CREATE (r:DreamRun {
                 id: $id, started_at: $now, finished_at: $now,
                 candidates_by_type_json: '{}', total_candidates: 0,
                 status_delta_json: $sd
               })""",
            id=run_id, now=now, sd=json.dumps(sd),
        )

    if mem_home:
        _append_ledger(mem_home, {
            "event": "finish",
            "run_id": run_id,
            "finished_at": now,
            "status_delta": sd,
        })


# -------- querying --------

def get_run(graph, run_id: str) -> dict | None:
    """Fetch a single run with its actions, for ``memfs dream-log show``.
    Returns None if the run_id doesn't exist.
    """
    row = graph.run_one(
        """MATCH (r:DreamRun {id: $id})
           RETURN r.id AS id, r.started_at AS started_at,
                  r.finished_at AS finished_at,
                  r.candidates_by_type_json AS cbt,
                  r.total_candidates AS total_candidates,
                  r.status_delta_json AS sd""",
        id=run_id,
    )
    if not row:
        return None

    actions = graph.run(
        """MATCH (:DreamRun {id: $id})-[:HAS_ACTION]->(a:DreamAction)
           RETURN a.id AS id, a.action AS action,
                  a.candidate_type AS candidate_type,
                  a.nodes_json AS nodes_json,
                  a.claim_id AS claim_id, a.note AS note,
                  a.recorded_at AS recorded_at
           ORDER BY a.recorded_at""",
        id=run_id,
    )
    parsed_actions = []
    for a in actions:
        try:
            nodes = json.loads(a.get("nodes_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            nodes = []
        parsed_actions.append({
            "id": a["id"],
            "action": a["action"],
            "candidate_type": a["candidate_type"],
            "nodes": nodes,
            "claim_id": a.get("claim_id"),
            "note": a.get("note"),
            "recorded_at": a.get("recorded_at"),
        })

    try:
        cbt = json.loads(row.get("cbt") or "{}")
    except (TypeError, json.JSONDecodeError):
        cbt = {}
    try:
        sd = json.loads(row.get("sd") or "null")
    except (TypeError, json.JSONDecodeError):
        sd = None

    return {
        "id": row["id"],
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "candidates_by_type": cbt,
        "total_candidates": int(row.get("total_candidates") or 0),
        "status_delta": sd,
        "actions": parsed_actions,
    }


# -------- reporting --------

_ACTIONED = {"merge", "split", "link", "archive"}
_DEFERRED = {"defer", "ignore"}


def dream_report(graph, *, window_days: int = 7,
                 recurrence_threshold: int = 3) -> dict:
    """Summary across the last ``window_days``.

    Returns::

        {
          "window_days": 7,
          "n_runs": 7,
          "total_candidates": 203,
          "total_actions": 18,
          "action_rate": 0.089,                    # actions / candidates
          "actions_by_kind": {"merge": 5, "defer": 10, ...},
          "candidates_by_type": {"orphan": 40, ...},
          "runs": [{run_id, started_at, finished_at,
                    total_candidates, n_actions}],
          "recurring_ignored": [{candidate_type, nodes,
                                 n_runs, last_action}],
        }

    ``recurring_ignored`` lists candidates (identified by nodes tuple +
    candidate_type) that appeared in ``>= recurrence_threshold`` distinct
    runs within the window AND whose most-recent action was
    ``defer``/``ignore``. The signal the report owes to the agent is
    "you keep punting this — is it junk, or a real decision you're
    avoiding?"
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=int(window_days))).isoformat()

    runs = graph.run(
        """MATCH (r:DreamRun)
           WHERE r.started_at >= $cutoff
           RETURN r.id AS id, r.started_at AS started_at,
                  r.finished_at AS finished_at,
                  r.candidates_by_type_json AS cbt,
                  r.total_candidates AS total_candidates
           ORDER BY r.started_at""",
        cutoff=cutoff,
    )

    # Run-level rollups
    total_candidates = 0
    candidates_by_type: dict = defaultdict(int)
    run_rows: list[dict] = []
    for r in runs:
        try:
            cbt = json.loads(r.get("cbt") or "{}")
        except (TypeError, json.JSONDecodeError):
            cbt = {}
        for k, v in cbt.items():
            candidates_by_type[str(k)] += int(v)
        total_candidates += int(r.get("total_candidates") or 0)
        # n_actions computed below once we have the action rows
        run_rows.append({
            "run_id": r["id"],
            "started_at": r.get("started_at"),
            "finished_at": r.get("finished_at"),
            "total_candidates": int(r.get("total_candidates") or 0),
        })

    # Actions across the same window
    actions = graph.run(
        """MATCH (r:DreamRun)-[:HAS_ACTION]->(a:DreamAction)
           WHERE r.started_at >= $cutoff
           RETURN r.id AS run_id, a.action AS action,
                  a.candidate_type AS candidate_type,
                  a.nodes_json AS nodes_json,
                  a.nodes_key AS nodes_key,
                  a.recorded_at AS recorded_at""",
        cutoff=cutoff,
    )

    actions_by_kind: dict = defaultdict(int)
    per_run_actions: dict = defaultdict(int)
    # For recurring-ignored: (candidate_type, nodes_key) → list of
    # (run_id, action, recorded_at)
    candidate_history: dict = defaultdict(list)

    for a in actions:
        kind = a.get("action") or "other"
        actions_by_kind[kind] += 1
        per_run_actions[a.get("run_id")] += 1
        ctype = a.get("candidate_type") or ""
        nkey = a.get("nodes_key") or ""
        # Only meaningful candidates contribute to recurrence (skip blank keys)
        if nkey:
            candidate_history[(ctype, nkey)].append({
                "run_id": a.get("run_id"),
                "action": kind,
                "nodes_json": a.get("nodes_json"),
                "recorded_at": a.get("recorded_at"),
            })

    total_actions = sum(actions_by_kind.values())

    # Attach n_actions to each run row
    for r in run_rows:
        r["n_actions"] = int(per_run_actions.get(r["run_id"], 0))

    # Recurring-ignored: a candidate seen in N distinct runs where the
    # MOST RECENT action is defer/ignore. Counting distinct runs matters
    # more than total-actions: three defers on the same night is a typo,
    # not recurrence.
    recurring: list[dict] = []
    for (ctype, nkey), hist in candidate_history.items():
        runs_seen = {h["run_id"] for h in hist}
        if len(runs_seen) < recurrence_threshold:
            continue
        # sort by recorded_at to find the most recent action
        hist_sorted = sorted(hist, key=lambda h: h.get("recorded_at") or "")
        last = hist_sorted[-1]
        if last["action"] not in _DEFERRED:
            continue
        try:
            nodes = json.loads(last.get("nodes_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            nodes = []
        recurring.append({
            "candidate_type": ctype,
            "nodes": nodes,
            "n_runs": len(runs_seen),
            "last_action": last["action"],
            "last_recorded_at": last.get("recorded_at"),
        })
    recurring.sort(key=lambda x: -x["n_runs"])

    return {
        "window_days": window_days,
        "n_runs": len(run_rows),
        "total_candidates": total_candidates,
        "total_actions": total_actions,
        "action_rate": (round(total_actions / total_candidates, 4)
                        if total_candidates else 0.0),
        "actions_by_kind": dict(actions_by_kind),
        "candidates_by_type": dict(candidates_by_type),
        "runs": run_rows,
        "recurring_ignored": recurring,
    }


# -------- rebuild-from-ledger (files-as-truth invariant) --------

def rebuild_from_ledger(graph, *, mem_home: str) -> dict:
    """Replay the JSONL ledger to rebuild DreamRun/DreamAction nodes in Neo4j.

    JSONL is the durable record. Neo4j is the queryable cache. After a DB
    wipe + ``memfs reindex``, the dream history must come back from the
    ledger — otherwise "files as truth" is a lie for this surface.

    Idempotent: runs MERGE on id, actions MERGE on id. Returns counts.
    """
    path = _ledger_path(mem_home)
    stats = {"starts_seen": 0, "starts_applied": 0,
             "actions_seen": 0, "actions_applied": 0,
             "finishes_seen": 0, "finishes_applied": 0}
    if not os.path.exists(path):
        return stats

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
            rid = rec.get("run_id")
            if not rid:
                continue

            if event == "start":
                stats["starts_seen"] += 1
                cbt = rec.get("candidates_by_type") or {}
                total = sum(int(v) for v in cbt.values())
                graph.run(
                    """MERGE (r:DreamRun {id: $id})
                       ON CREATE SET r.started_at = $st,
                                     r.candidates_by_type_json = $cbt,
                                     r.total_candidates = $total,
                                     r.status_delta_json = null,
                                     r.finished_at = null
                       ON MATCH  SET r.started_at = coalesce(r.started_at, $st),
                                     r.candidates_by_type_json = coalesce(r.candidates_by_type_json, $cbt),
                                     r.total_candidates = coalesce(r.total_candidates, $total)""",
                    id=rid, st=rec.get("started_at") or "",
                    cbt=json.dumps(cbt), total=total,
                )
                stats["starts_applied"] += 1

            elif event == "action":
                stats["actions_seen"] += 1
                aid = rec.get("action_id")
                if not aid:
                    continue
                nodes = rec.get("nodes") or []
                # Ensure the run exists (actions can predate starts in ledger order)
                graph.run(
                    """MERGE (r:DreamRun {id: $id})
                       ON CREATE SET r.started_at = $now,
                                     r.candidates_by_type_json = '{}',
                                     r.total_candidates = 0,
                                     r.status_delta_json = null,
                                     r.finished_at = null""",
                    id=rid, now=rec.get("recorded_at") or "",
                )
                graph.run(
                    """MATCH (r:DreamRun {id: $rid})
                       MERGE (a:DreamAction {id: $aid})
                       ON CREATE SET a.run_id = $rid, a.action = $action,
                                     a.candidate_type = $ctype,
                                     a.nodes_key = $nkey,
                                     a.nodes_json = $nodes_json,
                                     a.claim_id = $claim_id, a.note = $note,
                                     a.recorded_at = $recorded_at
                       MERGE (r)-[:HAS_ACTION]->(a)""",
                    rid=rid, aid=aid,
                    action=rec.get("action") or "other",
                    ctype=rec.get("candidate_type") or "",
                    nkey=_nodes_key(nodes),
                    nodes_json=json.dumps(nodes),
                    claim_id=rec.get("claim_id"),
                    note=rec.get("note"),
                    recorded_at=rec.get("recorded_at") or "",
                )
                stats["actions_applied"] += 1

            elif event == "finish":
                stats["finishes_seen"] += 1
                sd = rec.get("status_delta") or {}
                graph.run(
                    """MERGE (r:DreamRun {id: $id})
                       ON CREATE SET r.started_at = $fin,
                                     r.candidates_by_type_json = '{}',
                                     r.total_candidates = 0,
                                     r.finished_at = $fin,
                                     r.status_delta_json = $sd
                       ON MATCH  SET r.finished_at = coalesce(r.finished_at, $fin),
                                     r.status_delta_json = coalesce(r.status_delta_json, $sd)""",
                    id=rid, fin=rec.get("finished_at") or "",
                    sd=json.dumps(sd),
                )
                stats["finishes_applied"] += 1

    return stats
