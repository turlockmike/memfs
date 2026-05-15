# mvm — minimum viable memory

A self-verifying, self-growing memory for LLM agents. Markdown KB, cold-clone verification on every claim, tri-mode retrieval (text + graph + hierarchy). Two skills, three CLI primitives. Ask any question — the system either returns a cold-clone-verified answer from the KB, or fetches it from the web, gives it to you, and grows the KB in the background. Recall miss = curriculum signal.

## Thesis

A memory system is **viable** iff it can do three things in a closed loop:

1. **Encode** — take information from the world and put it somewhere
2. **Retrieve** — get it back on demand
3. **Verify** — prove the retrieval matches what was encoded

Below this floor, you have a write-only log (no retrieval check) or hopeful retrieval (no verification). The minimum is closing those three.

`mvm` is the smallest thing that does all three honestly.

## What's in v0

```
~/mvm/
├── README.md
├── knowledge/                # the user-curated KB
└── state/                    # tool-managed indexes
    ├── index.db              # SQLite + FTS5 over content
    └── graph.db              # adjacency from md-links + frontmatter refs

~/.local/bin/                 # CLI primitives (PATH-resident)
├── mvm verify                  # cold-clone subprocess; the load-bearing measurement
├── mvm index                    # rebuild graph + FTS
└── mvm search                # tri-mode retrieval (text + graph + hierarchy)

~/.claude/skills/             # Claude Code skills (orchestrators)
├── mvm-ingest/SKILL.md       # closed-loop write
└── mvm-recall/SKILL.md       # grounded retrieval with refusal
```

## The cold-clone primitive — two paths

**Inside a Claude Code session (canonical path):** the skills use the **Agent tool** to spawn a subagent as the cold-clone. Main session is the grader (it has the source/doc in context). One generative model performs both action and inference — FEP-aligned, no separate evaluator needed.

```
main session (you)               agent-tool subagent (cold-clone)
  │                                  │
  ├── reads source                   │
  ├── writes canonical               │
  ├── authors tests                  │
  ├── spawns -------- Q + content -> │
  │                                  ├── answers from doc only
  │  <---- answer ------------------ │
  ├── grades answer vs ground truth  │
  └── commits or refuses             ⊥
```

**Outside a session — `mvm verify` CLI** (cron, CI, automation): falls back to `claude --print --no-session-persistence --tools "" --system-prompt "..."` subprocess. Two-stage retriever + grader because there's no main session to grade. Less FEP-aligned but self-contained.

The CLI is the **outside-session fallback**, not the primary mechanism. Inside a session, the skills use Agent tool natively.

## The differential framework (`--lift`)

`mvm verify <doc> --lift` runs each test in **two modes** and reports the delta:

- **naked** — no file, no tools. Pure weight prior.
- **injected** — file content in prompt, no tools. The cold-clone.

```
=== KB LIFT REPORT ===
Naked    (weights only):       1/5
Injected (file in prompt):     4/5
KB lift  (delta):              +3
```

This is the **Hassabis weight-leakage detector**. If `kb_lift == 0` and all tests pass, the tests are too easy or the KB overlaps weight knowledge — the substrate isn't actually doing work. If `kb_lift < 0`, the KB is misleading the retriever.

## Tri-mode retrieval

`mvm search` combines three signals:

| Signal | Source | Default weight |
|---|---|---|
| **text**       | SQLite FTS5 (BM25 ranking)                   | 0.60 |
| **graph**      | BFS over markdown-link adjacency             | 0.25 |
| **hierarchy**  | path edit-distance from a seed/subtree       | 0.15 |

```bash
mvm search "leech overhaul"                          # text only
mvm search "leech overhaul" --in poe2/0.5            # subtree filter
mvm search "moonlaif damage" --near moonlaif.md      # graph proximity
mvm search "calguran" --kind canonical               # frontmatter filter
```

The filesystem is treated as a graph database — folder hierarchy carries semantic information (`logs/` vs `wiki/` vs `canonical/`), markdown links are explicit edges, and external URLs are first-class destinations.

## File conventions

Each KB entry is a pair:

```
<topic>.md                # canonical content with frontmatter
<topic>.tests.yaml        # locked Q/A test cases
```

Frontmatter:
```yaml
---
source: <URL or path>           # provenance backpointer (Hassabis)
kind: canonical|log|opinion|reference|synthesis
ingested_at: YYYY-MM-DD
---
```

Tests:
```yaml
- id: 1
  q: "What's the leech overhaul cap based on?"
  a: "single-source, biggest-hit only"
- id: 2
  q: "Negative test: what's the maximum number of curses?"
  a: "DONT-KNOW"
```

**Tests are immutable post-authoring.** During a verify-failure rewrite cycle, only the doc is edited. If you find yourself rewriting tests to make them pass, that's the Goodhart collapse — stop.

## Install

```bash
# 1. Make sure ~/.local/bin is on PATH
echo $PATH | grep -q "$HOME/.local/bin" || echo 'add ~/.local/bin to PATH'

# 2. Install Python deps
pip install --break-system-packages pyyaml fastembed sqlite-vec pytest

# 3. Symlink the single mvm CLI
ln -sf ~/mvm/bin/mvm ~/.local/bin/mvm

# 4. Verify the install
mvm --help
cd ~/mvm && python3 -m pytest tests/  # 31 tests, ~0.2s

# 5. Skills are in place at ~/.claude/skills/mvm-{ingest,recall,dream}
#    They auto-load in any new Claude Code session.
```

## Usage

### Add knowledge

```
/mvm-ingest https://youtube.com/watch?v=...
```

Or directly:
```bash
# write knowledge/<topic>.md and knowledge/<topic>.tests.yaml manually
mvm verify knowledge/<topic>.md          # gate the write
mvm index                                # register in graph + FTS
```

### Recall

```
/mvm-recall "what's the leech cap based on?"
```

Or directly:
```bash
mvm search "leech cap"
mvm verify knowledge/<top-result>.md
```

### Diagnostics

```bash
mvm verify <doc>           # all tests, injected mode
mvm verify <doc> --mode naked   # weight-prior baseline
mvm verify <doc> --lift    # differential (KB lift)
mvm search "<query>" --json     # machine-readable
```

## The viability test

Install. Then:

1. `/mvm-ingest <some source>` — get a verified canonical written.
2. `/mvm-recall "<question whose answer is in that source>"` — get the answer back, verified.
3. Wait two weeks. Run `mvm verify <topic>.md` again. Trust the pass/fail.
4. Re-run `/mvm-recall` — confirm the answer is unchanged unless you ingested an update.

If those four steps work end-to-end, the system is viable. Everything else is hardening.

## Roadmap

**v0 (this version):** the closed loop. SQLite FTS5 for text. Markdown link parsing for the graph. Cold-clone via subprocess. Differential KB-lift framework.

**v0.1 (planned):**
- Vector embeddings (Voyage AI) replacing FTS5 for semantic search
- `--bare` strict isolation mode (requires `ANTHROPIC_API_KEY`)
- Two test cohorts (source-derived + blind)
- Free-recall grader mode (Tulving's recognition-vs-recall fix)
- Recency penalty + decay timestamps (Hassabis's stale-but-confident defense)

**v0.2 (planned):**
- `/mvm-dream` — offline pass: replay, consolidate, status-tag (current/superseded/archived)
- Calibration monitoring + algedonic alarm on inversion
- Cohort regeneration during dream

**v0.3 (planned):**
- Reconstruction-mode verify (canonical → source claims, entailment-graded)
- Hooks for auto-fire on curated domains
- Pressure-index dashboard

## Theory

Built on three converging frames:

- **FEP (Friston):** /ingest is the action arm (substrate update). /recall is the perception arm (belief update). The cold-clone measures prediction error directly.
- **RL (Sutton):** the LLM is a frozen policy. The filesystem is the parameter store. Cold-clone scores are the reward signal. File writes are the gradient updates.
- **VSM (Beer):** kb-verify is the comparator. /ingest is the effector. The dream daemon (v0.2+) is the proprioceptive loop. Algedonic signal = calibration inversion.

These aren't competing frames — they're the same closed loop in different vocabularies. Pick whichever lands for your audience.

## Naming

`mvm` = minimum viable memory. The name is the thesis.
