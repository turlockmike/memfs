"""Shared pytest fixtures for memfs Neo4j tests.

Each test gets a fresh graph (all data wiped). Neo4j must be reachable at
the URI specified in $MEMFS_NEO4J_URI.

PRODUCTION-GRAPH GUARD
----------------------
The `graph` fixture is destructive: it runs ``MATCH (n) WHERE n:Node OR
n:Query OR n:Claim DETACH DELETE n`` before every test. Running tests
against a production Neo4j wipes memory corpora — which happened
2026-04-17 03:01 CDT when a dev pytest invocation quietly nuked the
karpathy graph (187 nodes → 9).

To prevent this, tests now REFUSE to run unless ``MEMFS_TEST_ALLOW_WIPE=1``
is set in the environment. Set it explicitly AND either (a) set
``MEMFS_NEO4J_URI`` to a test-only instance, or (b) accept that you're
wiping whatever bolt://localhost:7687 currently holds.
"""

import os
import pytest

from memfs.graph import create_db, connect, clear_data


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """Create schema once per session; tests wipe data each run."""
    if os.environ.get("MEMFS_TEST_ALLOW_WIPE") != "1":
        pytest.skip(
            "Production-graph guard: set MEMFS_TEST_ALLOW_WIPE=1 to run "
            "memfs tests. The `graph` fixture wipes all Node/Query/Claim "
            "data from $MEMFS_NEO4J_URI (default bolt://localhost:7687); "
            "without this flag, a casual `pytest` invocation from a dev "
            "shell would silently destroy production memory corpora. "
            "For test runs against a dedicated test DB, prefer setting "
            "MEMFS_NEO4J_URI to a different instance/port as well.",
            allow_module_level=True,
        )
    try:
        create_db()
    except Exception as e:
        pytest.skip(f"Neo4j unavailable: {e}", allow_module_level=True)


@pytest.fixture
def graph():
    """Fresh graph for each test. Wipes all Node/Query/Claim/edges before yield.

    NOTE: this is destructive against whatever Neo4j instance is pointed at by
    MEMFS_NEO4J_URI. Gated by MEMFS_TEST_ALLOW_WIPE=1 (see module docstring).
    """
    g = connect()
    # Start clean
    g.run("MATCH (n) WHERE n:Node OR n:Query OR n:Claim DETACH DELETE n")
    try:
        yield g
    finally:
        g.close()
