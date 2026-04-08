"""
Health Report — save and read worker health check results.

The worker saves a report after each cycle. The qmemory_health MCP tool
reads the latest report. This module handles both directions.
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import generate_id, get_db, query
from qmemory.formatters.response import attach_meta

logger = logging.getLogger(__name__)

# Map check parameter values to finding check types
CHECK_TYPE_MAP = {
    "orphans": "orphan",
    "contradictions": "contradiction",
    "stale": "stale",
    "missing_links": "linker",
    "gaps": "gap",
    "quality": "quality",
}


async def save_health_report(
    *,
    orphans_found: int = 0,
    contradictions_found: int = 0,
    stale_found: int = 0,
    links_created: int = 0,
    dupes_merged: int = 0,
    gaps: list[str] | None = None,
    quality_issues: int = 0,
    findings: list[dict] | None = None,
    duration_ms: int = 0,
    db: Any = None,
) -> str:
    """
    Save a health report to the health_report table.

    Returns the report ID.
    """
    report_id = generate_id("hr")

    # Use backtick syntax for record ID (type::record() unreliable in v3)
    surql = (
        f"CREATE health_report:`{report_id}` SET "
        "orphans_found = $orphans, "
        "contradictions_found = $contradictions, "
        "stale_found = $stale, "
        "links_created = $links, "
        "dupes_merged = $dupes, "
        "gaps = $gaps, "
        "quality_issues = $quality, "
        "findings = $findings, "
        "duration_ms = $duration"
    )
    params = {
        "orphans": orphans_found,
        "contradictions": contradictions_found,
        "stale": stale_found,
        "links": links_created,
        "dupes": dupes_merged,
        "gaps": gaps or [],
        "quality": quality_issues,
        "findings": findings or [],
        "duration": duration_ms,
    }

    if db is not None:
        await query(db, surql, params)
    else:
        async with get_db() as conn:
            await query(conn, surql, params)

    logger.info("save_health_report: saved report health_report:%s", report_id)
    return f"health_report:{report_id}"


async def get_latest_report(
    check: str = "all",
    db: Any = None,
) -> dict | None:
    """
    Read the most recent health report from the database.

    Args:
        check: Filter findings to this check type. "all" returns everything.
               Valid: "all", "orphans", "contradictions", "stale",
                      "missing_links", "gaps", "quality"
        db:    Optional DB connection for testing.

    Returns:
        The report dict with summary + findings, or None if no report exists.
    """
    surql = """
        SELECT *, created_at FROM health_report
        ORDER BY created_at DESC
        LIMIT 1
    """

    if db is not None:
        result = await query(db, surql)
    else:
        async with get_db() as conn:
            result = await query(conn, surql)

    if not result or not isinstance(result, list) or len(result) == 0:
        return None

    report = result[0]

    # Filter findings by check type if requested
    if check != "all" and check in CHECK_TYPE_MAP:
        check_type = CHECK_TYPE_MAP[check]
        report["findings"] = [
            f for f in report.get("findings", [])
            if f.get("check") == check_type
        ]

    return attach_meta(
        report,
        actions_context={"type": "health", "check": check},
    )
