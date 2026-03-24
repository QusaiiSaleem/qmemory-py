"""
Tests for qmemory.core.decay

Tests the 3-tier biological salience decay system and the recall boost function.
All tests use the `db` fixture from conftest.py, which provides a fresh SurrealDB
connection in the "qmemory_test" namespace — so we never touch production data.

These tests require SurrealDB to be running locally (ws://localhost:8000).

What we're testing:
  1. run_salience_decay returns the expected result dict shape
  2. Empty database returns all-zero counts
  3. apply_recall_boost increments recall_count and bumps salience by 0.05
  4. apply_recall_boost caps salience at 1.0 (never exceeds)
"""

from __future__ import annotations

import pytest

from qmemory.core.decay import apply_recall_boost, run_salience_decay
from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# run_salience_decay — result shape
# ---------------------------------------------------------------------------


async def test_decay_returns_dict(db):
    """run_salience_decay returns a dict with tier counts and elapsed_ms."""
    result = await run_salience_decay(db=db)

    # All 4 expected keys must be present
    assert "tier1_decayed" in result
    assert "tier2_decayed" in result
    assert "tier3_enforced" in result
    assert "elapsed_ms" in result


async def test_decay_no_memories(db):
    """No memories in the database → all counts should be 0."""
    result = await run_salience_decay(db=db)

    assert result["tier1_decayed"] == 0
    assert result["tier2_decayed"] == 0
    assert result["tier3_enforced"] == 0


# ---------------------------------------------------------------------------
# apply_recall_boost — basic behavior
# ---------------------------------------------------------------------------


async def test_recall_boost_increments(db):
    """apply_recall_boost bumps salience by 0.05 and increments recall_count."""
    # Save a memory with known salience
    saved = await save_memory(
        content="Test fact for recall boost",
        category="context",
        salience=0.5,
        db=db,
    )
    # Extract just the ID suffix (e.g. "mem1710864000000abc" from "memory:mem...")
    mem_id = saved["memory_id"].split(":")[1]

    # Apply the recall boost
    await apply_recall_boost(mem_id, db=db)

    # Read back the memory and check salience + recall_count
    result = await query(
        db,
        "SELECT salience, recall_count FROM type::record('memory', $id)",
        {"id": mem_id},
    )

    assert result is not None
    assert len(result) == 1
    assert result[0]["salience"] == pytest.approx(0.55, abs=0.01)
    assert result[0]["recall_count"] == 1


async def test_recall_boost_caps_at_1(db):
    """Salience should never exceed 1.0, even when boosted from 0.98."""
    # Save a memory with salience very close to the maximum
    saved = await save_memory(
        content="Important fact near max salience",
        category="context",
        salience=0.98,
        db=db,
    )
    mem_id = saved["memory_id"].split(":")[1]

    # Apply the recall boost — would be 0.98 + 0.05 = 1.03, but should cap at 1.0
    await apply_recall_boost(mem_id, db=db)

    # Verify salience did not exceed 1.0
    result = await query(
        db,
        "SELECT salience FROM type::record('memory', $id)",
        {"id": mem_id},
    )

    assert result is not None
    assert len(result) == 1
    assert result[0]["salience"] <= 1.0
