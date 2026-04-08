"""Integration test: worker cycle saves report, health tool reads it."""
from __future__ import annotations

import pytest

from qmemory.core.health import get_latest_report, save_health_report
from qmemory.core.linter import run_linter_checks
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_linter_then_report_then_read(db):
    """Full flow: create data, run linter, save report, read via health module."""
    # Create an orphan memory
    await query(db, """
        CREATE memory:orphan_int SET
            content = 'Integration test orphan',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)

    # Create a stale memory
    await query(db, """
        CREATE memory:stale_int SET
            content = 'Integration test stale',
            category = 'context',
            is_active = true,
            salience = 0.05,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            source_type = 'conversation',
            linked = false
    """)

    # Run linter checks
    findings = await run_linter_checks(db=db)
    assert len(findings) >= 2  # at least orphan + stale

    # Save report
    orphans = len([f for f in findings if f["check"] == "orphan"])
    stale = len([f for f in findings if f["check"] == "stale"])

    await save_health_report(
        orphans_found=orphans,
        stale_found=stale,
        findings=findings,
        duration_ms=100,
        db=db,
    )

    # Read report via health module
    report = await get_latest_report(db=db)
    assert report is not None
    assert report["orphans_found"] >= 1
    assert report["stale_found"] >= 1
    assert len(report["findings"]) >= 2

    # Filter by check type
    orphan_report = await get_latest_report(check="orphans", db=db)
    assert all(f["check"] == "orphan" for f in orphan_report["findings"])
