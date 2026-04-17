"""Shared pytest fixtures for memfs Neo4j tests.

Each test gets a fresh graph (all data wiped). Neo4j must be reachable at
the URI specified in $MEMFS_NEO4J_URI (default bolt://localhost:7687).
"""

import os
import pytest

from memfs.graph import create_db, connect, clear_data


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """Create schema once per session; tests wipe data each run."""
    try:
        create_db()
    except Exception as e:
        pytest.skip(f"Neo4j unavailable: {e}", allow_module_level=True)


@pytest.fixture
def graph():
    """Fresh graph for each test. Wipes all Node/Query/Claim/edges before yield.

    NOTE: this is destructive against whatever Neo4j instance is pointed at by
    MEMFS_NEO4J_URI. Don't run tests against a production graph.
    """
    g = connect()
    # Start clean
    g.run("MATCH (n) WHERE n:Node OR n:Query OR n:Claim DETACH DELETE n")
    try:
        yield g
    finally:
        g.close()
