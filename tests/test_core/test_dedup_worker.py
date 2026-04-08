"""Tests for the batch dedup worker."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from qmemory.core.dedup_worker import run_dedup_cycle
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_dedup_finds_and_merges_duplicates(db):
    """Two very similar memories in same category should be merged."""
    # Create two near-identical memories (high word overlap for Jaccard > 0.5)
    await query(db, """
        CREATE memory:dup1 SET
            content = 'Qusai prefers dark mode in all applications and editors',
            category = 'preference',
            is_active = true,
            salience = 0.7,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = true
    """)
    await query(db, """
        CREATE memory:dup2 SET
            content = 'Qusai prefers using dark mode in all applications',
            category = 'preference',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = true
    """)

    # Mock the LLM to say these are duplicates
    mock_llm_response = {
        "duplicates": [
            {
                "keep_id": "memory:dup1",
                "remove_id": "memory:dup2",
                "reason": "Same fact about dark mode",
            }
        ]
    }

    with patch("qmemory.core.dedup_worker.get_llm") as mock_get_llm:
        mock_provider = AsyncMock()
        mock_provider.complete.return_value = mock_llm_response
        mock_get_llm.return_value = mock_provider

        result = await run_dedup_cycle(db=db)

    assert result["found_work"] is True
    assert result["dupes_merged"] >= 1

    # The weaker duplicate should be soft-deleted
    dup2 = await query(db, "SELECT is_active FROM memory:dup2")
    assert dup2[0]["is_active"] is False


@pytest.mark.asyncio
async def test_dedup_no_work_when_empty(db):
    """If no memories exist, return found_work=False."""
    result = await run_dedup_cycle(db=db)
    assert result["found_work"] is False
    assert result["dupes_merged"] == 0
