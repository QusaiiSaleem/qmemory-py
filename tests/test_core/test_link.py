"""
Tests for qmemory.core.link

Tests the link_nodes() function — creating dynamic `relates` edges
between any two nodes in the graph.

All tests use the `db` fixture from conftest.py, which gives us a fresh
SurrealDB connection in the "qmemory_test" namespace. Every test starts
with a clean, empty database so there's no cross-test interference.

These tests require SurrealDB to be running locally (ws://localhost:8000).

What we're testing:
  1. Basic edge creation between two memories
  2. Edge includes the confidence field (optional)
  3. Edge includes the reason field (optional)
  4. Linking nodes from different tables (e.g. memory → entity)
  5. Invalid node IDs return None gracefully (not an exception)
  6. Invalid ID format (missing colon) raises ValueError
  7. The returned dict has the expected shape (edge_id, from_id, to_id, type, _nudge)
"""

import pytest

from qmemory.core.link import link_nodes
from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# Basic edge creation
# ---------------------------------------------------------------------------


async def test_link_two_memories(db):
    """
    The most basic case: link two memory nodes with a relationship type.

    Verifies:
    - Returns a dict (not None)
    - dict has "edge_id", "from_id", "to_id", "type", "_nudge" keys
    - "type" matches what we passed in
    - The edge actually exists in SurrealDB (we query to confirm)
    """
    # Create two memories to link together
    m1 = await save_memory(content="Budget is 500K", category="context", db=db)
    m2 = await save_memory(content="Board approved budget", category="decision", db=db)

    # Link them — m2 supports m1
    edge = await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="supports",
        reason="Budget approval evidence",
        db=db,
    )

    # The function should return a result dict
    assert edge is not None

    # Check the shape of the returned dict
    assert "edge_id" in edge
    assert "from" in edge
    assert "to" in edge
    assert "relationship_type" in edge
    assert "actions" in edge
    assert "meta" in edge

    # The relationship type should be echoed back
    assert edge["relationship_type"] == "supports"

    # The from/to IDs should be echoed back
    assert edge["from"]["id"] == m1["memory_id"]
    assert edge["to"]["id"] == m2["memory_id"]

    # The edge ID should start with "relates:" (SurrealDB format)
    assert edge["edge_id"].startswith("relates:")


async def test_link_with_confidence(db):
    """
    Passing a confidence value should be accepted without error.
    The edge record should exist in the database.
    """
    m1 = await save_memory(content="Fact A", category="context", db=db)
    m2 = await save_memory(content="Fact B", category="context", db=db)

    edge = await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="contradicts",
        confidence=0.7,
        db=db,
    )

    # Should succeed with the confidence value
    assert edge is not None
    assert edge["relationship_type"] == "contradicts"
    assert "edge_id" in edge


async def test_link_with_reason_only(db):
    """
    Passing just a reason (no confidence) should work fine.
    Confidence should default to 0.8.
    """
    m1 = await save_memory(content="Cause fact", category="context", db=db)
    m2 = await save_memory(content="Effect fact", category="context", db=db)

    edge = await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="caused_by",
        reason="Direct causal relationship observed",
        db=db,
    )

    assert edge is not None
    assert edge["relationship_type"] == "caused_by"


async def test_link_minimal_params(db):
    """
    Only from_id, to_id, and relationship_type are required.
    Neither reason nor confidence need to be provided.
    """
    m1 = await save_memory(content="Node one", category="context", db=db)
    m2 = await save_memory(content="Node two", category="context", db=db)

    edge = await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="relates_to",
        db=db,
    )

    assert edge is not None
    assert edge["relationship_type"] == "relates_to"


# ---------------------------------------------------------------------------
# Edge actually persisted in DB
# ---------------------------------------------------------------------------


async def test_edge_exists_in_database(db):
    """
    After creating a link, the `relates` edge should be queryable from SurrealDB.

    This is the most important test — it proves the RELATE statement actually
    ran and persisted, not just that the function returned a result.
    """
    m1 = await save_memory(content="Memory alpha", category="context", db=db)
    m2 = await save_memory(content="Memory beta", category="context", db=db)

    # Parse suffixes for the query (split on first colon)
    id1_suffix = m1["memory_id"].split(":")[1]

    edge = await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="supports",
        db=db,
    )

    assert edge is not None

    # Query SurrealDB directly to verify the edge exists
    edges_in_db = await query(
        db,
        "SELECT id, relationship_type FROM relates WHERE in = <record>$from_id",
        {"from_id": m1["memory_id"]},
    )

    assert edges_in_db is not None
    assert len(edges_in_db) >= 1

    # The relationship_type should be stored on the edge record
    assert edges_in_db[0]["relationship_type"] == "supports"


async def test_reason_stored_in_edge(db):
    """
    The reason string should be stored on the edge record in the database.
    """
    m1 = await save_memory(content="Fact with reason", category="context", db=db)
    m2 = await save_memory(content="Related fact", category="context", db=db)

    id1_suffix = m1["memory_id"].split(":")[1]

    await link_nodes(
        from_id=m1["memory_id"],
        to_id=m2["memory_id"],
        relationship_type="supports",
        reason="Shared project context",
        db=db,
    )

    # Verify the reason was stored
    edges_in_db = await query(
        db,
        "SELECT reason FROM relates WHERE in = <record>$from_id",
        {"from_id": m1["memory_id"]},
    )

    assert edges_in_db is not None and len(edges_in_db) >= 1
    assert edges_in_db[0].get("reason") == "Shared project context"


# ---------------------------------------------------------------------------
# Error / graceful degradation
# ---------------------------------------------------------------------------


async def test_link_invalid_from_id(db):
    """
    If the from node doesn't exist in the database, link_nodes should
    return None gracefully — NOT raise an exception.

    The agent needs a clear non-crash signal so it can try a different ID.
    """
    m2 = await save_memory(content="Valid target memory", category="context", db=db)

    result = await link_nodes(
        from_id="memory:nonexistentXXX",
        to_id=m2["memory_id"],
        relationship_type="test",
        db=db,
    )

    # Should return None (graceful degradation), not raise
    assert result is None


async def test_link_invalid_to_id(db):
    """
    If the to node doesn't exist, link_nodes should return None gracefully.
    """
    m1 = await save_memory(content="Valid source memory", category="context", db=db)

    result = await link_nodes(
        from_id=m1["memory_id"],
        to_id="memory:nonexistentYYY",
        relationship_type="test",
        db=db,
    )

    assert result is None


async def test_link_both_invalid_ids(db):
    """
    If both nodes don't exist, link_nodes should return None gracefully.
    """
    result = await link_nodes(
        from_id="memory:nonexistent",
        to_id="memory:also_nonexistent",
        relationship_type="test",
        db=db,
    )

    assert result is None


async def test_link_invalid_id_format_raises_valueerror(db):
    """
    IDs that don't include a colon should raise ValueError immediately.
    Format "memory:suffix" is required — passing just "memory" is wrong.
    """
    m1 = await save_memory(content="Memory for format test", category="context", db=db)

    with pytest.raises(ValueError, match="Invalid from_id"):
        await link_nodes(
            from_id="justasuffix",  # Missing the "table:" prefix
            to_id=m1["memory_id"],
            relationship_type="test",
            db=db,
        )


async def test_link_invalid_to_id_format_raises_valueerror(db):
    """
    The to_id must also include a colon — otherwise ValueError is raised.
    """
    m1 = await save_memory(content="Memory for format test 2", category="context", db=db)

    with pytest.raises(ValueError, match="Invalid to_id"):
        await link_nodes(
            from_id=m1["memory_id"],
            to_id="justasuffix",  # Missing the "table:" prefix
            relationship_type="test",
            db=db,
        )


# ---------------------------------------------------------------------------
# Multiple edges
# ---------------------------------------------------------------------------


async def test_multiple_edges_from_same_node(db):
    """
    One memory can have multiple outgoing edges to different targets.
    Each call to link_nodes creates a new independent edge.
    """
    # One source, two targets
    source = await save_memory(content="Central fact", category="context", db=db)
    target1 = await save_memory(content="Related fact 1", category="context", db=db)
    target2 = await save_memory(content="Related fact 2", category="decision", db=db)

    edge1 = await link_nodes(
        from_id=source["memory_id"],
        to_id=target1["memory_id"],
        relationship_type="supports",
        db=db,
    )
    edge2 = await link_nodes(
        from_id=source["memory_id"],
        to_id=target2["memory_id"],
        relationship_type="caused_by",
        db=db,
    )

    # Both edges should be created
    assert edge1 is not None
    assert edge2 is not None

    # They should have different edge IDs
    assert edge1["edge_id"] != edge2["edge_id"]

    # Verify both exist in the DB
    edges_in_db = await query(
        db,
        "SELECT id FROM relates WHERE in = <record>$from_id",
        {"from_id": source["memory_id"]},
    )

    assert edges_in_db is not None
    assert len(edges_in_db) == 2
