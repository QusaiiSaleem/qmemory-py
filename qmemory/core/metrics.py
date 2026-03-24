"""
Qmemory Metrics — Lightweight Event Tracking

Fire-and-forget event logging to the `metrics` table.
Never blocks hot paths — errors are caught and silently dropped.

Event types used across the codebase:
  recall_hit    — memory recall returned results (data: count as string)
  recall_miss   — memory recall returned 0 results
  dedup_add     — new memory added after dedup check
  dedup_update  — existing memory updated during dedup
  dedup_noop    — duplicate detected, nothing written
  tool_call     — agent tool was invoked (data: tool name)
  compaction    — compaction triggered (data: stage number as string)
  extraction    — background facts extracted (data: count as string)

Design: track_event() is intentionally fire-and-forget. Callers should
NOT await it on hot paths — just schedule it. get_session_metrics()
is used for dashboards and debugging, not agent context injection.

Ported from src/core/metrics.ts — behaviour must stay in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import generate_id, get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_id_part(full_id: Any) -> str:
    """
    Extract the bare ID from a SurrealDB record reference.

    E.g. "session:s1234abc" → "s1234abc".
    If there's no colon, the full string is returned unchanged.
    """
    s = str(full_id)
    idx = s.find(":")
    return s[idx + 1:] if idx >= 0 else s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def track_event(
    session_id: str,
    event_type: str,
    data: str | None = None,
    db: Any = None,
) -> None:
    """
    Log an event to the metrics table.

    Fire-and-forget — never raises. Errors are silently dropped so a
    metrics failure never interrupts normal agent operation.

    Args:
        session_id: The session's SurrealDB ID (with or without "session:" prefix).
        event_type: One of the event type strings defined in this module's docstring.
        data:       Optional extra data (e.g. a count, a tool name). Omit if not needed.
        db:         Optional DB connection for test injection.
    """
    try:
        id_part = generate_id("mt")
        sid = _session_id_part(session_id)

        if data is not None:
            # Include the optional event_data field when data was provided.
            surql = """CREATE type::record("metrics", $id_part) CONTENT {
                session:    type::record("session", $session_id),
                event_type: $event_type,
                event_data: $event_data,
                created_at: time::now()
            }"""
            params: dict[str, Any] = {
                "id_part": id_part,
                "session_id": sid,
                "event_type": event_type,
                "event_data": data,
            }
        else:
            # Omit event_data entirely — SurrealDB 3.0 rejects NULL for
            # option<string> fields; omitting the key is the correct approach.
            surql = """CREATE type::record("metrics", $id_part) CONTENT {
                session:    type::record("session", $session_id),
                event_type: $event_type,
                created_at: time::now()
            }"""
            params = {
                "id_part": id_part,
                "session_id": sid,
                "event_type": event_type,
            }

        if db is not None:
            await query(db, surql, params)
        else:
            async with get_db() as conn:
                await query(conn, surql, params)

    except Exception:
        # Intentional silent drop — metrics must never crash callers.
        pass


async def get_session_metrics(
    session_id: str,
    db: Any = None,
) -> dict[str, int]:
    """
    Return aggregated event counts for a session.

    Useful for debugging, dashboards, and session-end summaries.
    Returns a dict with zero values for any event type that has no rows yet.

    Args:
        session_id: The session's SurrealDB ID.
        db:         Optional DB connection for test injection.

    Returns:
        Dict with keys: recall_hits, recall_misses, dedup_adds, dedup_updates,
        dedup_noops, tool_calls, compactions, extractions.
    """
    # Default to all zeros so callers never need to handle missing keys.
    totals: dict[str, int] = {
        "recall_hits": 0,
        "recall_misses": 0,
        "dedup_adds": 0,
        "dedup_updates": 0,
        "dedup_noops": 0,
        "tool_calls": 0,
        "compactions": 0,
        "extractions": 0,
    }

    # Map from event_type string → summary dict key.
    _key_map = {
        "recall_hit": "recall_hits",
        "recall_miss": "recall_misses",
        "dedup_add": "dedup_adds",
        "dedup_update": "dedup_updates",
        "dedup_noop": "dedup_noops",
        "tool_call": "tool_calls",
        "compaction": "compactions",
        "extraction": "extractions",
    }

    surql = """SELECT event_type, count() AS total
               FROM metrics
               WHERE session = type::record("session", $session_id)
               GROUP BY event_type"""
    params = {"session_id": _session_id_part(session_id)}

    try:
        if db is not None:
            rows = await query(db, surql, params)
        else:
            async with get_db() as conn:
                rows = await query(conn, surql, params)

        if not rows:
            return totals

        for row in rows:
            key = _key_map.get(row.get("event_type", ""))
            if key:
                totals[key] = int(row.get("total", 0))

        return totals
    except Exception as exc:
        logger.debug("get_session_metrics failed: %s", exc)
        return totals
