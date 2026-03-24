"""
Tests for qmemory.core.reflector

Tests the background reflection service that analyzes recent memories
to find patterns, contradictions, compressions, ghost entities, and
self-learnings — all in a single LLM call.

Test categories:
  1. test_reflector_no_memories — returns found_work=False with too few memories
  2. test_reflector_excludes_reflections — source_type='reflect' memories are excluded
  3. test_reflector_returns_stats — result dict has the expected shape
  4. test_parse_reflection_valid — pure function: handles valid JSON
  5. test_parse_reflection_invalid — pure function: handles malformed JSON
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qmemory.core.reflector import _parse_reflection, run_reflector_cycle
from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# 1. Too few memories → found_work=False
# ---------------------------------------------------------------------------


async def test_reflector_no_memories(db):
    """
    When there are fewer than 3 non-reflect memories in the database,
    run_reflector_cycle should return found_work=False immediately.

    The reflector needs at least 3 memories to find meaningful patterns.
    With 0 or 1-2 memories, there is not enough signal to reflect on.
    """
    # Don't insert any memories — database is empty
    result = await run_reflector_cycle(db=db)

    assert result["found_work"] is False
    assert "elapsed_ms" in result


async def test_reflector_no_memories_with_two(db):
    """
    Even with 2 memories, we still need at least 3 to reflect.
    """
    await save_memory(content="User prefers dark mode", category="preference", db=db)
    await save_memory(content="Project uses Python 3.11", category="context", db=db)

    result = await run_reflector_cycle(db=db)

    assert result["found_work"] is False


# ---------------------------------------------------------------------------
# 2. source_type='reflect' memories are excluded from input
# ---------------------------------------------------------------------------


async def test_reflector_excludes_reflections(db):
    """
    Memories with source_type='reflect' (i.e. outputs from previous
    reflections) should NOT be included in the input to the reflector.

    This prevents a feedback loop where the reflector reflects on its
    own reflections, creating an infinite chain of meta-observations.

    We create 3 reflect-sourced memories and 2 normal ones — the reflector
    should only see the 2 normal ones (fewer than 3), so found_work=False.
    """
    # Create 3 memories with source_type='reflect' — these should be excluded
    await save_memory(
        content="Pattern: user tends to work late at night",
        category="context",
        source_type="reflect",
        db=db,
    )
    await save_memory(
        content="Pattern: user prefers concise answers",
        category="self",
        source_type="reflect",
        db=db,
    )
    await save_memory(
        content="Self-learning: I should ask clarifying questions",
        category="self",
        source_type="reflect",
        db=db,
    )

    # Create only 2 normal memories — not enough after excluding reflections
    await save_memory(content="User likes tea", category="preference", db=db)
    await save_memory(content="Meeting at 3pm", category="context", db=db)

    result = await run_reflector_cycle(db=db)

    # Should return found_work=False because only 2 non-reflect memories exist
    assert result["found_work"] is False


# ---------------------------------------------------------------------------
# 3. Return dict has expected shape
# ---------------------------------------------------------------------------


async def test_reflector_returns_stats(db):
    """
    When enough memories exist and the LLM returns results,
    run_reflector_cycle should return a dict with:
    - found_work (bool)
    - patterns (int) — count of patterns created
    - contradictions (int) — count of contradiction edges created
    - compressions (int) — count of compressions applied
    - ghost_entities (int) — count of ghost entities created
    - self_learnings (int) — count of self-learnings saved
    - elapsed_ms (number)
    """
    # Create 4 memories so the reflector has enough to work with
    await save_memory(content="User prefers dark mode", category="preference", db=db)
    await save_memory(content="Project deadline is March 2026", category="context", db=db)
    await save_memory(content="Budget approved at 500K", category="decision", db=db)
    await save_memory(content="Ahmed mentioned a new tool", category="context", db=db)

    # Mock LLM to return a valid reflection with one pattern found
    llm_response_json = '{"patterns": [{"content": "User has clear project constraints", "memory_ids": ["memory:fake1", "memory:fake2"]}], "contradictions": [], "compressions": [], "ghost_entities": [], "self_learnings": []}'

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=llm_response_json)]
    mock_response.usage = MagicMock(input_tokens=500, output_tokens=200)

    # Mock get_settings to provide a fake ZAI API key (the reflector
    # checks for this before calling the LLM)
    mock_settings = MagicMock()
    mock_settings.zai_api_key = "fake-key-for-testing"

    with (
        patch("qmemory.core.reflector._call_llm", new_callable=AsyncMock, return_value=mock_response),
        patch("qmemory.core.reflector.can_spend", return_value=True),
        patch("qmemory.core.reflector.record_spend"),
        patch("qmemory.core.reflector.get_settings", return_value=mock_settings),
        patch("qmemory.core.reflector.save_memory", new_callable=AsyncMock, return_value={"action": "ADD", "memory_id": "memory:memtest123"}),
    ):
        result = await run_reflector_cycle(db=db)

    # Check the shape of the result
    assert "found_work" in result
    assert "patterns" in result
    assert "contradictions" in result
    assert "compressions" in result
    assert "ghost_entities" in result
    assert "self_learnings" in result
    assert "elapsed_ms" in result

    # Types should be correct
    assert isinstance(result["found_work"], bool)
    assert isinstance(result["patterns"], int)
    assert isinstance(result["contradictions"], int)
    assert isinstance(result["compressions"], int)
    assert isinstance(result["ghost_entities"], int)
    assert isinstance(result["self_learnings"], int)
    assert isinstance(result["elapsed_ms"], (int, float))

    # found_work should be True since we had enough memories
    assert result["found_work"] is True

    # 1 pattern was returned by the mock LLM
    assert result["patterns"] == 1

    # elapsed_ms should be non-negative
    assert result["elapsed_ms"] >= 0


# ---------------------------------------------------------------------------
# 4. _parse_reflection() — valid JSON
# ---------------------------------------------------------------------------


def test_parse_reflection_valid():
    """
    _parse_reflection() is a pure function (no DB, no LLM) that parses
    the raw JSON string from the LLM into a structured dict with the 5 arrays.

    Given valid JSON with all 5 keys, it should return them as-is.
    """
    raw_json = """{
        "patterns": [
            {"content": "User prefers dark mode consistently", "memory_ids": ["memory:mem001", "memory:mem002"]}
        ],
        "contradictions": [
            {"memory_a": "memory:mem003", "memory_b": "memory:mem004", "reason": "Conflicting deadlines"}
        ],
        "compressions": [
            {"merged_content": "User prefers minimalist design", "source_ids": ["memory:mem005", "memory:mem006", "memory:mem007"]}
        ],
        "ghost_entities": [
            {"name": "Ahmed"}
        ],
        "self_learnings": [
            {"content": "I should ask for clarification before assuming context"}
        ]
    }"""

    result = _parse_reflection(raw_json)

    # All 5 arrays should be present
    assert len(result["patterns"]) == 1
    assert len(result["contradictions"]) == 1
    assert len(result["compressions"]) == 1
    assert len(result["ghost_entities"]) == 1
    assert len(result["self_learnings"]) == 1

    # Spot-check the content
    assert result["patterns"][0]["content"] == "User prefers dark mode consistently"
    assert result["contradictions"][0]["memory_a"] == "memory:mem003"
    assert result["ghost_entities"][0]["name"] == "Ahmed"


def test_parse_reflection_valid_empty_arrays():
    """
    When the LLM finds nothing, it returns all empty arrays.
    _parse_reflection() should handle this gracefully.
    """
    raw_json = '{"patterns": [], "contradictions": [], "compressions": [], "ghost_entities": [], "self_learnings": []}'

    result = _parse_reflection(raw_json)

    assert result["patterns"] == []
    assert result["contradictions"] == []
    assert result["compressions"] == []
    assert result["ghost_entities"] == []
    assert result["self_learnings"] == []


# ---------------------------------------------------------------------------
# 5. _parse_reflection() — invalid / malformed JSON
# ---------------------------------------------------------------------------


def test_parse_reflection_invalid():
    """
    _parse_reflection() should return the empty structure (all 5 arrays empty)
    when the LLM returns invalid JSON or garbage text.

    This is a safety net — the LLM sometimes returns markdown or explanatory
    text instead of pure JSON.
    """
    result = _parse_reflection("This is not JSON at all!")

    assert result["patterns"] == []
    assert result["contradictions"] == []
    assert result["compressions"] == []
    assert result["ghost_entities"] == []
    assert result["self_learnings"] == []


def test_parse_reflection_partial_keys():
    """
    If the LLM returns valid JSON but missing some keys, the missing
    keys should default to empty arrays.
    """
    # Only has patterns and contradictions — missing the other 3
    raw_json = '{"patterns": [{"content": "test", "memory_ids": ["m1"]}], "contradictions": []}'

    result = _parse_reflection(raw_json)

    assert len(result["patterns"]) == 1
    assert result["contradictions"] == []
    # Missing keys default to empty arrays
    assert result["compressions"] == []
    assert result["ghost_entities"] == []
    assert result["self_learnings"] == []


def test_parse_reflection_not_a_dict():
    """
    If the LLM returns valid JSON that is an array instead of an object,
    _parse_reflection should return the empty structure.
    """
    raw_json = '[{"something": "unexpected"}]'

    result = _parse_reflection(raw_json)

    assert result["patterns"] == []
    assert result["contradictions"] == []
    assert result["compressions"] == []
    assert result["ghost_entities"] == []
    assert result["self_learnings"] == []
