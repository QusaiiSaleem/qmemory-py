"""
Qmemory Session Scratchpad — Working Memory

A per-session record that tracks current task state:
progress, findings, open questions, and tool summary.

Lives in the `scratchpad` table with a UNIQUE index on session.
The UPSERT pattern is used so there's never a race condition between
a SELECT check and a CREATE — SurrealDB handles it atomically.

Ported from src/core/scratchpad.ts — behaviour must stay in sync.
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session_id_part(full_id: Any) -> str:
    """
    Extract the bare ID from a SurrealDB record reference.

    SurrealDB returns IDs like "session:s1234abc".
    We only want the part AFTER the colon: "s1234abc".
    If there's no colon, the whole string is returned unchanged.
    """
    s = str(full_id)
    idx = s.find(":")
    return s[idx + 1:] if idx >= 0 else s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_scratchpad(session_id: str, db: Any = None) -> dict | None:
    """
    Get the scratchpad for a session.

    Returns a dict with fields like task_progress, key_findings,
    open_questions, and tool_summary — or None if no scratchpad exists yet.

    Args:
        session_id: The session's SurrealDB ID (with or without "session:" prefix).
        db:         Optional DB connection for test injection. If None, opens a
                    fresh connection via get_db().
    """
    surql = 'SELECT * FROM scratchpad WHERE session = type::record("session", $session_id) LIMIT 1'
    params = {"session_id": _session_id_part(session_id)}

    try:
        if db is not None:
            results = await query(db, surql, params)
        else:
            async with get_db() as conn:
                results = await query(conn, surql, params)

        if results:
            return results[0]
        return None
    except Exception as exc:
        logger.debug("get_scratchpad failed: %s", exc)
        return None


async def update_scratchpad(
    session_id: str,
    *,
    task_progress: str | None = None,
    key_findings: str | None = None,
    open_questions: str | None = None,
    tool_summary: str | None = None,
    db: Any = None,
) -> bool:
    """
    Update (upsert) the scratchpad for a session.

    Only non-empty string fields are written — empty strings and None are
    skipped so a partial update never blanks existing content.

    Uses SurrealDB's UPSERT with a UNIQUE index on session, making this a
    single indexed operation rather than a SELECT-then-CREATE/UPDATE pair.

    Args:
        session_id:     The session's SurrealDB ID.
        task_progress:  What the agent is currently working on.
        key_findings:   Important discoveries so far.
        open_questions: Questions that still need answers.
        tool_summary:   Summary of tools used this session.
        db:             Optional DB connection for test injection.

    Returns:
        True if the upsert succeeded, False on error.
    """
    try:
        sid = _session_id_part(session_id)

        # Build SET clauses and params dynamically.
        # session + updated_at are always written.
        set_clauses: list[str] = [
            'session = type::record("session", $session_id)',
            "updated_at = time::now()",
        ]
        params: dict[str, Any] = {"session_id": sid}

        if task_progress:
            set_clauses.append("task_progress = $task_progress")
            params["task_progress"] = task_progress

        if key_findings:
            set_clauses.append("key_findings = $key_findings")
            params["key_findings"] = key_findings

        if open_questions:
            set_clauses.append("open_questions = $open_questions")
            params["open_questions"] = open_questions

        if tool_summary:
            set_clauses.append("tool_summary = $tool_summary")
            params["tool_summary"] = tool_summary

        set_sql = ", ".join(set_clauses)
        surql = (
            f'UPSERT scratchpad SET {set_sql} '
            f'WHERE session = type::record("session", $session_id)'
        )

        if db is not None:
            await query(db, surql, params)
        else:
            async with get_db() as conn:
                await query(conn, surql, params)

        return True
    except Exception as exc:
        logger.debug("update_scratchpad failed: %s", exc)
        return False


async def clear_scratchpad(session_id: str, db: Any = None) -> bool:
    """
    Delete the scratchpad row for a session.

    Used at session end to free the row. The memory graph still preserves
    any facts that were extracted — the scratchpad is only working memory.

    Args:
        session_id: The session's SurrealDB ID.
        db:         Optional DB connection for test injection.

    Returns:
        True if the delete succeeded (or there was nothing to delete), False on error.
    """
    surql = 'DELETE scratchpad WHERE session = type::record("session", $session_id)'
    params = {"session_id": _session_id_part(session_id)}

    try:
        if db is not None:
            await query(db, surql, params)
        else:
            async with get_db() as conn:
                await query(conn, surql, params)
        return True
    except Exception as exc:
        logger.debug("clear_scratchpad failed: %s", exc)
        return False
