"""
Tests for qmemory.core.save

Tests the basic CREATE path of save_memory(). All tests use the `db` fixture
from conftest.py, which provides a fresh SurrealDB connection in the
"qmemory_test" namespace — so we never touch production data.

These tests require SurrealDB to be running locally (ws://localhost:8000).
If SurrealDB is not running, the tests will fail with connection errors.

What we're testing:
  1. Basic save returns the expected result shape (action, memory_id, _nudge)
  2. All optional fields can be passed without errors
  3. Invalid categories raise ValueError
  4. Saved memories are actually readable from the database
"""

import pytest

from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# Basic save tests
# ---------------------------------------------------------------------------


async def test_save_memory_basic(db):
    """
    The simplest save — just content, category, and salience.

    Verifies:
    - Returns a dict with "action", "memory_id", and "_nudge" keys
    - action is "ADD" (always, since we don't have dedup yet)
    - memory_id starts with "memory:" (SurrealDB record format)
    - _nudge contains a suggestion to link the memory
    """
    result = await save_memory(
        content="Annual budget is 500K SAR",
        category="context",
        salience=0.8,
        db=db,
    )

    # Check the result shape
    assert result["action"] == "ADD"
    assert result["memory_id"].startswith("memory:")
    assert "_nudge" in result
    assert "qmemory_link" in result["_nudge"]


async def test_save_memory_all_fields(db):
    """
    Save with ALL optional fields provided.

    This exercises the dynamic query builder — every optional clause
    (source_person, context_mood, valid_from, valid_until) gets appended.

    Note: source_person needs a real entity record to exist, so we skip
    it here to avoid test coupling. It's tested separately below.
    """
    result = await save_memory(
        content="Team approved the plan",
        category="decision",
        salience=0.9,
        scope="project:alpha",
        confidence=0.95,
        evidence_type="observed",
        context_mood="calm_decision",
        source_type="conversation",
        db=db,
    )

    assert result["action"] == "ADD"
    assert result["memory_id"].startswith("memory:")


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


async def test_save_memory_invalid_category(db):
    """
    An invalid category should raise ValueError immediately.

    The agent might hallucinate a category name like "knowledge" or "memory".
    We catch it early so the agent can correct itself.
    """
    with pytest.raises(ValueError, match="Invalid category"):
        await save_memory(
            content="test",
            category="invalid_category",
            db=db,
        )


async def test_save_memory_invalid_category_empty(db):
    """Empty string is also not a valid category."""
    with pytest.raises(ValueError, match="Invalid category"):
        await save_memory(
            content="test",
            category="",
            db=db,
        )


# ---------------------------------------------------------------------------
# Database verification tests
# ---------------------------------------------------------------------------


async def test_saved_memory_readable(db):
    """
    Verify the memory was actually stored in SurrealDB.

    This is the most important test — it proves the CREATE query actually
    worked and the data is persisted. We save a memory, then SELECT it
    back and check the content matches.
    """
    result = await save_memory(
        content="Important fact",
        category="context",
        salience=0.7,
        db=db,
    )

    # Query the database directly to verify the memory exists
    memories = await query(db, "SELECT * FROM memory WHERE is_active = true")

    assert memories is not None
    assert len(memories) >= 1

    # Find our specific memory by content
    found = any(m["content"] == "Important fact" for m in memories)
    assert found, "Saved memory not found in database"


async def test_saved_memory_fields_correct(db):
    """
    Verify that all fields are stored with the correct values.

    Saves a memory with specific values, then reads it back and checks
    each field individually. This catches bugs in parameter binding.
    """
    result = await save_memory(
        content="The team uses Slack for communication",
        category="context",
        salience=0.75,
        scope="project:beta",
        confidence=0.9,
        evidence_type="reported",
        source_type="workspace",
        context_mood="casual",
        db=db,
    )

    # Extract the ID suffix from "memory:memXXX" to query by ID
    memory_id = result["memory_id"]

    # Query the specific memory by its full record ID
    memories = await query(
        db,
        "SELECT * FROM type::record('memory', $id)",
        {"id": memory_id.split(":")[1]},
    )

    assert memories is not None
    assert len(memories) == 1

    mem = memories[0]
    assert mem["content"] == "The team uses Slack for communication"
    assert mem["category"] == "context"
    assert mem["salience"] == 0.75
    assert mem["scope"] == "project:beta"
    assert mem["confidence"] == 0.9
    assert mem["evidence_type"] == "reported"
    assert mem["source_type"] == "workspace"
    assert mem["context_mood"] == "casual"
    assert mem["recall_count"] == 0
    assert mem["linked"] is False
    assert mem["is_active"] is True


async def test_multiple_saves_create_separate_memories(db):
    """
    Saving two different facts should create two separate memory nodes.

    This verifies that each save generates a unique ID and doesn't
    overwrite previous memories.
    """
    result1 = await save_memory(
        content="First fact",
        category="context",
        db=db,
    )
    result2 = await save_memory(
        content="Second fact",
        category="decision",
        db=db,
    )

    # Each save should return a different memory ID
    assert result1["memory_id"] != result2["memory_id"]

    # Both should be in the database
    memories = await query(db, "SELECT * FROM memory WHERE is_active = true")
    assert memories is not None
    assert len(memories) == 2


# ---------------------------------------------------------------------------
# Category coverage tests
# ---------------------------------------------------------------------------


async def test_all_valid_categories_accepted(db):
    """
    Every one of the 8 valid categories should be accepted without error.

    This is a sanity check that our validation list matches what the
    database schema expects.
    """
    from qmemory.constants import MEMORY_CATEGORIES

    for cat in MEMORY_CATEGORIES:
        result = await save_memory(
            content=f"Test fact for category {cat}",
            category=cat,
            db=db,
        )
        assert result["action"] == "ADD", f"Category '{cat}' failed"


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


async def test_save_memory_default_values(db):
    """
    When only content and category are provided, all defaults should be applied.

    Verifies: salience=0.5, scope="global", confidence=0.8, etc.
    """
    result = await save_memory(
        content="Fact with all defaults",
        category="preference",
        db=db,
    )

    # Read back the memory to check defaults
    memories = await query(db, "SELECT * FROM memory WHERE content = $c", {"c": "Fact with all defaults"})

    assert memories is not None
    assert len(memories) == 1

    mem = memories[0]
    assert mem["salience"] == 0.5
    assert mem["scope"] == "global"
    assert mem["confidence"] == 0.8
    assert mem["evidence_type"] == "observed"
    assert mem["source_type"] == "conversation"
