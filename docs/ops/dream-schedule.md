---
title: "Dream consolidation schedule"
date: 2026-04-17
layer: 2
---

# Dream consolidation schedule

Nightly cron that triggers a `/memfs-dream` consolidation pass inside
Karpathy's persistent session.

## Invocation

- **Cron:** `0 3 * * * CRON_TZ=America/Chicago /home/mike/.local/bin/karpathy-dream-trigger.sh`
- **Script:** `~/.local/bin/karpathy-dream-trigger.sh` (versioned via
  `karpathy-snapshot.sh` hourly auto-commit)
- **Log:** `~/.local/share/karpathy-dream.log`

## Flow

```
cron  →  karpathy-dream-trigger.sh
           │
           ├── primary:  claude-loop send karpathy "<dream prompt>" --from dream-cron
           │             (queues inbox message + wakes Karpathy; non-blocking)
           │
           └── fallback: CLAUDECODE= timeout 600 claude -p "<dream prompt>"
                         (only used if claude-loop binary is missing)
```

Queue-and-wake is preferred because Karpathy is a persistent session managed
by claude-loop; a direct `claude -p` would race with it.

## What the dream prompt asks for

The agent is instructed to:
1. Run `memfs dream-briefing` to collect candidates (orphan / merge / split
   / link / stale / index).
2. Prioritize 3-5 candidates.
3. Act where the right move is obvious (create an index.md, delete a true
   orphan, add a [[wikilink]]).
4. Log uncertain decisions via `memfs claim` so calibration can track them.

This is deliberately small: the cron fires the thought, the agent decides
what to do. The LLM loop lives in Karpathy's skill, not in this script.
