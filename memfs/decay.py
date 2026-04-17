"""Power-law decay engine and spacing-effect increments.

Neuroscience-informed: power law decays fast early, levels off.
"""

import math
from datetime import datetime, timezone

from memfs import graph as graph_mod

# Decay parameters
PRUNE_THRESHOLD = 0.05  # Edges below this get deleted (search only)
LINK_FLOOR = 0.5        # Link edges never decay below this
MAX_STRENGTH = 5.0      # Cap on edge strength
SCHEMA_MULTIPLIER = 1.5 # Bonus for same-directory edges


def decayed_strength(strength: float, days_since: float) -> float:
    """Apply power-law decay to an edge strength.

    Formula: strength * (1 + 0.1 * days)^-0.5
    """
    if days_since <= 0:
        return strength
    return strength * (1 + 0.1 * days_since) ** -0.5


def spacing_increment(days_gap: float, same_dir: bool = False) -> float:
    """Compute the spacing-effect increment for a co-access event."""
    multiplier = SCHEMA_MULTIPLIER if same_dir else 1.0
    return 0.05 * (1 + math.log(1 + days_gap)) * multiplier


def run_decay(graph, dry_run: bool = False) -> dict:
    """Run power-law decay sweep across all edges.

    - Search edges decay fully and get pruned below PRUNE_THRESHOLD.
    - Link edges respect LINK_FLOOR.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    link_updates: list[tuple[str, str, float, str]] = []
    search_updates: list[tuple[str, str, float, str]] = []
    link_prunes: list[tuple[str, str]] = []
    search_prunes: list[tuple[str, str]] = []

    # We issue two separate queries for LINK and SEARCH to avoid the shared
    # iterator pattern's awkward "source_qid" thing.
    link_rows = graph.run(
        "MATCH (s:Node)-[r:LINK]->(t:Node) "
        "RETURN s.path AS source, t.path AS target, "
        "r.strength AS strength, r.last_activated AS last_activated"
    )
    search_rows = graph.run(
        "MATCH (q:Query)-[r:SEARCH]->(t:Node) "
        "RETURN q.id AS source, t.path AS target, "
        "r.strength AS strength, r.last_activated AS last_activated"
    )

    def _days_since(last_activated):
        if not last_activated:
            return 0
        last_dt = datetime.fromisoformat(str(last_activated))
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return max(0, (now - last_dt).total_seconds() / 86400)

    for row in link_rows:
        days = _days_since(row["last_activated"])
        strength = float(row["strength"] or 0.0)
        new_strength = max(decayed_strength(strength, days), LINK_FLOOR)
        link_updates.append((row["source"], row["target"], new_strength, now_iso))

    for row in search_rows:
        days = _days_since(row["last_activated"])
        strength = float(row["strength"] or 0.0)
        new_strength = decayed_strength(strength, days)
        if new_strength < PRUNE_THRESHOLD:
            search_prunes.append((row["source"], row["target"]))
        else:
            search_updates.append((row["source"], row["target"], new_strength, now_iso))

    updated_count = len(link_updates) + len(search_updates)
    pruned_count = len(link_prunes) + len(search_prunes)

    if not dry_run:
        graph_mod.apply_decay_updates(
            graph,
            link_updates=link_updates,
            search_updates=search_updates,
            link_prunes=link_prunes,
            search_prunes=search_prunes,
        )
        graph_mod.set_meta(graph, "last_decay", now_iso)

    return {"updated": updated_count, "pruned": pruned_count}
