"""
Tests for qmemory.core.linker

Tests the automatic memory linking service that runs in the background.
It queries unlinked memories, asks a cheap LLM to find relationships,
and creates edges between related memories.

Test categories:
  1. test_linker_no_unlinked — returns found_work=False when nothing to process
  2. test_linker_marks_linked — memories get linked=true after processing
  3. test_linker_validates_ids — pure function test for hallucination defense
  4. test_linker_returns_stats — result dict has the expected shape
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qmemory.core.linker import _validate_edges, run_linker_cycle
from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# 1. No unlinked memories → found_work=False
# ---------------------------------------------------------------------------


async def test_linker_no_unlinked(db):
    """
    When there are no memories with linked=false in the database,
    run_linker_cycle should return found_work=False immediately.

    This tells the worker to back off (sleep 30 min) instead of
    checking again right away.
    """
    # Don't insert any memories — database is empty
    result = await run_linker_cycle(db=db)

    assert result["found_work"] is False
    assert "elapsed_ms" in result


# ---------------------------------------------------------------------------
# 2. After processing, memories have linked=true
# ---------------------------------------------------------------------------


async def test_linker_marks_linked(db):
    """
    After run_linker_cycle processes memories, they should have
    linked=true — even if the LLM found zero relationships.

    This prevents the same memories from being re-checked endlessly.
    We mock the LLM call to return an empty array (no relationships found).
    """
    # Create 2 memories — both start with linked=false (the default)
    await save_memory(content="The project deadline is March 2026", category="context", db=db)
    await save_memory(content="Budget approved at 500K SAR", category="decision", db=db)

    # Verify they start as unlinked
    unlinked_before = await query(
        db,
        "SELECT id FROM memory WHERE linked = false AND is_active = true",
    )
    assert len(unlinked_before) == 2

    # Mock the LLM call to return empty array (no relationships found)
    # Also mock token budget to always allow spending
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="[]")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=10)

    with (
        patch("qmemory.core.linker._call_llm", new_callable=AsyncMock, return_value=mock_response),
        patch("qmemory.core.linker.can_spend", return_value=True),
        patch("qmemory.core.linker.record_spend"),
    ):
        result = await run_linker_cycle(db=db)

    # Should report work was found (memories existed to process)
    assert result["found_work"] is True
    assert result["processed"] == 2  # Both memories were processed
    assert result["edges_created"] == 0  # LLM found no relationships

    # Now both memories should be linked=true in the database
    unlinked_after = await query(
        db,
        "SELECT id FROM memory WHERE linked = false AND is_active = true",
    )
    assert len(unlinked_after) == 0


# ---------------------------------------------------------------------------
# 3. _validate_edges filters out IDs not in the working set
# ---------------------------------------------------------------------------


def test_linker_validates_ids():
    """
    _validate_edges() is a pure function (no DB needed) that filters
    out any edge suggestions where the IDs don't match the actual
    working set of memories.

    This is hallucination defense — the LLM might invent IDs that
    look plausible but don't exist. We catch those here.
    """
    # The actual IDs in our working set (unlinked + candidates)
    valid_ids = {
        "memory:mem001",
        "memory:mem002",
        "memory:mem003",
        "memory:mem004",
    }

    # LLM returned these edge suggestions — some have invented IDs
    raw_edges = [
        # Good: both IDs exist in the working set
        {"from_id": "memory:mem001", "to_id": "memory:mem003", "type": "supports", "reason": "related context"},
        # Bad: from_id is hallucinated (not in working set)
        {"from_id": "memory:FAKE999", "to_id": "memory:mem002", "type": "contradicts", "reason": "made up"},
        # Bad: to_id is hallucinated
        {"from_id": "memory:mem002", "to_id": "memory:INVENTED", "type": "elaborates", "reason": "also made up"},
        # Good: both IDs exist
        {"from_id": "memory:mem004", "to_id": "memory:mem001", "type": "depends_on", "reason": "dependency"},
    ]

    # Validate — should keep only the 2 good edges
    validated = _validate_edges(raw_edges, valid_ids)

    assert len(validated) == 2
    assert validated[0]["from_id"] == "memory:mem001"
    assert validated[0]["to_id"] == "memory:mem003"
    assert validated[1]["from_id"] == "memory:mem004"
    assert validated[1]["to_id"] == "memory:mem001"


def test_linker_validates_ids_empty_input():
    """
    _validate_edges with an empty list should return an empty list.
    """
    valid_ids = {"memory:mem001"}
    validated = _validate_edges([], valid_ids)
    assert validated == []


def test_linker_validates_ids_all_invalid():
    """
    If every edge has at least one fake ID, the result should be empty.
    """
    valid_ids = {"memory:mem001", "memory:mem002"}
    raw_edges = [
        {"from_id": "memory:FAKE1", "to_id": "memory:FAKE2", "type": "supports", "reason": "all fake"},
    ]
    validated = _validate_edges(raw_edges, valid_ids)
    assert validated == []


# ---------------------------------------------------------------------------
# 4. Return dict has expected shape
# ---------------------------------------------------------------------------


async def test_linker_returns_stats(db):
    """
    run_linker_cycle should return a dict with at minimum:
    - found_work (bool)
    - processed (int)
    - edges_created (int)
    - elapsed_ms (number)
    """
    # Create a memory so there's work to do
    await save_memory(content="Test memory for stats", category="context", db=db)

    # Mock the LLM to return one valid edge (we need two memories for that)
    await save_memory(content="Another memory for linking", category="context", db=db)

    # Mock LLM to return an empty response
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="[]")]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=10)

    with (
        patch("qmemory.core.linker._call_llm", new_callable=AsyncMock, return_value=mock_response),
        patch("qmemory.core.linker.can_spend", return_value=True),
        patch("qmemory.core.linker.record_spend"),
    ):
        result = await run_linker_cycle(db=db)

    # Check the shape of the result
    assert "found_work" in result
    assert "processed" in result
    assert "edges_created" in result
    assert "elapsed_ms" in result

    # Types should be correct
    assert isinstance(result["found_work"], bool)
    assert isinstance(result["processed"], int)
    assert isinstance(result["edges_created"], int)
    assert isinstance(result["elapsed_ms"], (int, float))

    # elapsed_ms should be non-negative
    assert result["elapsed_ms"] >= 0
