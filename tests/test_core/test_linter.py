"""Tests for the linter health checks."""
from __future__ import annotations

import pytest

from qmemory.core.linter import (
    check_orphans,
    check_stale,
    check_gaps,
    check_quality,
)
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_check_orphans_finds_unlinked_memory(db):
    """A memory with no relates edges should be flagged as orphan."""
    # Create a memory with no edges
    await query(db, """
        CREATE memory:orphan1 SET
            content = 'I have no friends',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)

    findings = await check_orphans(db=db)
    orphan_ids = [f["node_id"] for f in findings]
    assert "memory:orphan1" in orphan_ids
    assert findings[0]["check"] == "orphan"
    assert findings[0]["severity"] == "warning"
    assert findings[0]["fixed"] is False


@pytest.mark.asyncio
async def test_check_orphans_skips_linked_memory(db):
    """A memory with a relates edge should NOT be flagged."""
    # Create two memories with an edge between them
    await query(db, """
        CREATE memory:linked1 SET
            content = 'I have a friend',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = true
    """)
    await query(db, """
        CREATE memory:linked2 SET
            content = 'I am the friend',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = true
    """)
    await query(db, """
        RELATE memory:linked1->relates->memory:linked2
        SET relationship_type = 'supports', confidence = 0.8
    """)

    findings = await check_orphans(db=db)
    orphan_ids = [f["node_id"] for f in findings]
    assert "memory:linked1" not in orphan_ids
    assert "memory:linked2" not in orphan_ids


@pytest.mark.asyncio
async def test_check_stale_finds_expired(db):
    """A memory past valid_until should be found and auto-fixed."""
    await query(db, """
        CREATE memory:stale1 SET
            content = 'This fact expired',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false,
            valid_until = <datetime>'2020-01-01T00:00:00Z'
    """)

    findings = await check_stale(db=db)
    assert len(findings) >= 1
    stale = [f for f in findings if f["node_id"] == "memory:stale1"]
    assert len(stale) == 1
    assert stale[0]["fixed"] is True

    # Verify the memory was actually soft-deleted
    result = await query(db, "SELECT is_active FROM memory:stale1")
    assert result[0]["is_active"] is False


@pytest.mark.asyncio
async def test_check_stale_finds_decayed(db):
    """A memory with salience < 0.1 should be found and auto-fixed."""
    await query(db, """
        CREATE memory:decayed1 SET
            content = 'Very low salience',
            category = 'context',
            is_active = true,
            salience = 0.05,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)

    findings = await check_stale(db=db)
    decayed = [f for f in findings if f["node_id"] == "memory:decayed1"]
    assert len(decayed) == 1
    assert decayed[0]["fixed"] is True


@pytest.mark.asyncio
async def test_check_gaps(db):
    """Categories with < 3 memories should be flagged."""
    # Create 1 memory in 'decision' category — should be flagged as gap
    await query(db, """
        CREATE memory:gap1 SET
            content = 'Lone decision',
            category = 'decision',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)

    findings = await check_gaps(db=db)
    assert len(findings) >= 1
    # 'decision' should appear as a gap (only 1 memory)
    gap_details = [f["detail"] for f in findings]
    assert any("decision" in d for d in gap_details)


@pytest.mark.asyncio
async def test_check_quality_broken_edge(db):
    """An edge pointing to an inactive node should be found and auto-fixed."""
    # Create one real memory
    await query(db, """
        CREATE memory:real1 SET
            content = 'I exist',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = true
    """)
    # Create a fake target memory that is inactive
    await query(db, """
        CREATE memory:fake1 SET
            content = 'I am inactive',
            category = 'context',
            is_active = false,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)
    # Create edge to the inactive memory
    await query(db, """
        RELATE memory:real1->relates->memory:fake1
        SET relationship_type = 'supports', confidence = 0.8
    """)

    findings = await check_quality(db=db)
    # Should find the edge pointing to an inactive node
    assert len(findings) >= 1
    assert findings[0]["check"] == "quality"
    assert findings[0]["severity"] == "error"
