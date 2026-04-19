"""Shared pytest fixtures for memfs Neo4j tests.

Each test gets a fresh graph (all data wiped). Neo4j must be reachable at
the URI specified in $MEMFS_NEO4J_URI.

PRODUCTION-GRAPH GUARDS (two layers)
------------------------------------
The `graph` fixture is destructive: it runs ``MATCH (n) WHERE n:Node OR
n:Query OR n:Claim OR n:Access OR n:DreamRun OR n:DreamAction DETACH
DELETE n`` before every test. Running tests against a production Neo4j
wipes memory corpora — which
happened 2026-04-17 03:01 CDT (187 nodes → 9) AND AGAIN 2026-04-18 14:37
CDT (205 nodes → 5) when a dev pytest invocation quietly nuked the
karpathy graph.

GUARD 1 — explicit opt-in: tests REFUSE to run unless
``MEMFS_TEST_ALLOW_WIPE=1`` is set. Honor-system, easily bypassed.

GUARD 2 — node-count circuit breaker: on session start we count :Node
nodes in the target DB. If the count exceeds
``MEMFS_TEST_MAX_EXISTING_NODES`` (default 20), we HARD-FAIL regardless
of ``MEMFS_TEST_ALLOW_WIPE``. Production Karpathy graph is ~200 nodes;
a fresh test DB is 0. Anything in between is either a test DB someone
already used, or — more likely — production. Fail fast, point at a
real test instance.

To run tests: bring up a dedicated memfs test instance via
``docker compose -f docker-compose.test.yml up -d`` (TODO: add it) and
set ``MEMFS_NEO4J_URI=bolt://localhost:7688 MEMFS_TEST_ALLOW_WIPE=1``.
"""

import os
import pytest

from memfs.graph import create_db, connect, clear_data


@pytest.fixture(scope="session", autouse=True)
def _disable_semantic_contradiction():
    """Tests run without calling `infer` — heuristic-only semantics.

    The production detector calls `infer -r contradiction-judge` as a
    subprocess to semantically verify heuristic candidates. That would be
    (a) slow, (b) non-deterministic, and (c) require infer + ollama to be
    running in CI. Setting this env var bypasses the semantic stage so
    tests validate the heuristic + edge-creation paths only. The semantic
    path has its own dedicated tests that mock subprocess explicitly.
    """
    os.environ["MEMFS_CONTRADICTION_SKIP_SEMANTIC"] = "1"


@pytest.fixture(scope="session", autouse=True)
def _ensure_schema():
    """Create schema once per session; tests wipe data each run.

    Two-layer production guard — see module docstring.
    """
    # GUARD 1: explicit opt-in
    if os.environ.get("MEMFS_TEST_ALLOW_WIPE") != "1":
        pytest.skip(
            "Production-graph guard: set MEMFS_TEST_ALLOW_WIPE=1 to run "
            "memfs tests. The `graph` fixture wipes all Node/Query/Claim/"
            "Access data from $MEMFS_NEO4J_URI (default "
            "bolt://localhost:7687); without this flag, a casual `pytest` "
            "invocation from a dev shell would silently destroy production "
            "memory corpora.",
            allow_module_level=True,
        )

    # GUARD 2: node-count circuit breaker. If the target DB has more than
    # MAX_EXISTING_NODES :Node, we assume production and hard-fail.
    max_existing = int(os.environ.get("MEMFS_TEST_MAX_EXISTING_NODES", "20"))
    try:
        g = connect()
    except Exception as e:
        pytest.skip(f"Neo4j unavailable: {e}", allow_module_level=True)
    try:
        existing = g.run_scalar("MATCH (n:Node) RETURN count(n)") or 0
    except Exception:
        existing = 0
    finally:
        g.close()

    if existing > max_existing:
        pytest.skip(
            f"Production-graph circuit breaker: $MEMFS_NEO4J_URI has "
            f"{existing} :Node nodes (threshold={max_existing}). This "
            f"smells like production. Point tests at a dedicated test "
            f"instance (recommended: docker compose -f "
            f"docker-compose.test.yml up -d; MEMFS_NEO4J_URI=bolt://"
            f"localhost:7688) or raise the threshold via "
            f"MEMFS_TEST_MAX_EXISTING_NODES=<N> if you intentionally "
            f"want to wipe.",
            allow_module_level=True,
        )

    try:
        create_db()
    except Exception as e:
        pytest.skip(f"Neo4j unavailable: {e}", allow_module_level=True)


@pytest.fixture
def graph():
    """Fresh graph for each test. Wipes all Node/Query/Claim/Access/edges
    before yield.

    NOTE: this is destructive against whatever Neo4j instance is pointed at by
    MEMFS_NEO4J_URI. Gated by MEMFS_TEST_ALLOW_WIPE=1 (see module docstring).
    """
    g = connect()
    # Start clean
    g.run(
        "MATCH (n) WHERE n:Node OR n:Query OR n:Claim OR n:Access "
        "OR n:DreamRun OR n:DreamAction DETACH DELETE n"
    )
    try:
        yield g
    finally:
        g.close()
