"""Test fixtures."""
import sys
from pathlib import Path

import pytest

# Make the mvm package importable
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


@pytest.fixture
def tmp_kb(tmp_path):
    """A small temp KB with two canonicals + tests yaml. Used by index/search tests."""
    knowledge = tmp_path / "knowledge"
    state = tmp_path / "state"
    knowledge.mkdir()
    state.mkdir()

    # Doc 1
    (knowledge / "topic-a.md").write_text("""---
source: file://topic-a
kind: canonical
ingested_at: 2026-05-08
---

# Topic A

Topic A connects to [Topic B](topic-b.md) and [[wiki-link]].

External: [Source](https://example.com/a).
""")
    (knowledge / "topic-a.tests.yaml").write_text("""\
- id: 1
  q: "What does topic A connect to?"
  a: "Topic B and wiki-link"
""")

    # Doc 2
    (knowledge / "topic-b.md").write_text("""---
source: https://example.com/b
kind: canonical
see_also: [topic-a.md]
ingested_at: 2026-05-08
---

# Topic B

This is topic B.
""")
    (knowledge / "topic-b.tests.yaml").write_text("""\
- id: 1
  q: "What's topic B?"
  a: "topic B"
""")

    return tmp_path  # caller can access knowledge/ and state/
