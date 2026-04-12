"""Power-law decay engine and spacing-effect increments.

Neuroscience-informed: single exponential is the one model forgetting data
consistently rejects. Power law decays fast early, levels off.
"""

import math
from datetime import datetime, timezone

# Decay parameters
PRUNE_THRESHOLD = 0.05  # Edges below this get deleted
LINK_FLOOR = 0.5        # Link edges never decay below this
MAX_STRENGTH = 5.0      # Cap on edge strength
SCHEMA_MULTIPLIER = 1.5 # Bonus for same-directory edges


def decayed_strength(strength: float, days_since: float) -> float:
    """Apply power-law decay to an edge strength.

    Formula: strength * (1 + 0.1 * days)^-0.5

    Half-life is ~30 days for strength 1.0.
    """
    if days_since <= 0:
        return strength
    return strength * (1 + 0.1 * days_since) ** -0.5


def spacing_increment(days_gap: float, same_dir: bool = False) -> float:
    """Compute the spacing-effect increment for a co-access event.

    Per neuroscience: accessing things together repeatedly in the same session
    gives diminishing returns. Accessing them after a gap is a stronger signal.

    Formula: 0.05 * (1 + log(1 + days_gap)) * schema_multiplier
    """
    multiplier = SCHEMA_MULTIPLIER if same_dir else 1.0
    return 0.05 * (1 + math.log(1 + days_gap)) * multiplier


def run_decay(conn, dry_run: bool = False) -> dict:
    """Run power-law decay sweep across all edges.

    - Search edges decay fully and get pruned below PRUNE_THRESHOLD.
    - Link edges respect LINK_FLOOR.
    - Returns stats: {updated, pruned}.
    """
    now = datetime.now(timezone.utc)
    cursor = conn.execute(
        "SELECT source, target, type, strength, last_activated FROM edges"
    )

    to_update = []
    to_prune = []

    for row in cursor:
        source, target, etype, strength, last_activated = row
        if last_activated:
            last_dt = datetime.fromisoformat(last_activated)
            # Ensure timezone-aware comparison
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            days = max(0, (now - last_dt).total_seconds() / 86400)
        else:
            days = 0

        new_strength = decayed_strength(strength, days)

        # Apply floor for link edges
        if etype == "link":
            new_strength = max(new_strength, LINK_FLOOR)

        if new_strength < PRUNE_THRESHOLD and etype != "link":
            to_prune.append((source, target, etype))
        else:
            to_update.append((new_strength, now.isoformat(), source, target, etype))

    if not dry_run:
        conn.executemany(
            "UPDATE edges SET strength=?, last_activated=? WHERE source=? AND target=? AND type=?",
            to_update,
        )
        for source, target, etype in to_prune:
            conn.execute(
                "DELETE FROM edges WHERE source=? AND target=? AND type=?",
                (source, target, etype),
            )
        # Record last decay time
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_decay', ?)",
            (now.isoformat(),),
        )
        conn.commit()

    return {"updated": len(to_update), "pruned": len(to_prune)}
