"""Tests for health report reading/saving."""
from __future__ import annotations

import pytest

from qmemory.core.health import get_latest_report, save_health_report


@pytest.mark.asyncio
async def test_save_and_read_report(db):
    """Save a report, then read it back."""
    findings = [
        {
            "check": "orphan",
            "severity": "warning",
            "node_id": "memory:mem1",
            "detail": "No connections",
            "action": None,
            "fixed": False,
        },
    ]
    await save_health_report(
        orphans_found=1,
        contradictions_found=0,
        stale_found=0,
        links_created=5,
        dupes_merged=2,
        gaps=["decision", "self"],
        quality_issues=0,
        findings=findings,
        duration_ms=1234,
        db=db,
    )

    report = await get_latest_report(db=db)
    assert report is not None
    assert report["orphans_found"] == 1
    assert report["links_created"] == 5
    assert report["dupes_merged"] == 2
    assert len(report["findings"]) == 1
    assert report["findings"][0]["check"] == "orphan"


@pytest.mark.asyncio
async def test_read_empty_report(db):
    """If no report exists, return None."""
    report = await get_latest_report(db=db)
    assert report is None


@pytest.mark.asyncio
async def test_filter_by_check(db):
    """Filter findings by check type."""
    findings = [
        {
            "check": "orphan",
            "severity": "warning",
            "node_id": "memory:mem1",
            "detail": "No connections",
            "action": None,
            "fixed": False,
        },
        {
            "check": "stale",
            "severity": "info",
            "node_id": "memory:mem2",
            "detail": "Expired",
            "action": None,
            "fixed": True,
        },
        {
            "check": "quality",
            "severity": "error",
            "node_id": "relates:rel1",
            "detail": "Broken edge",
            "action": None,
            "fixed": True,
        },
    ]
    await save_health_report(
        orphans_found=1,
        contradictions_found=0,
        stale_found=1,
        links_created=0,
        dupes_merged=0,
        gaps=[],
        quality_issues=1,
        findings=findings,
        duration_ms=500,
        db=db,
    )

    report = await get_latest_report(check="orphans", db=db)
    assert report is not None
    assert len(report["findings"]) == 1
    assert report["findings"][0]["check"] == "orphan"

    report = await get_latest_report(check="quality", db=db)
    assert len(report["findings"]) == 1
    assert report["findings"][0]["check"] == "quality"
