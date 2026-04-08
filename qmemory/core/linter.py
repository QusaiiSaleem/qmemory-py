"""
Linter — 6 health checks for the memory graph.

Each check function returns a list of finding dicts. Some checks auto-fix
issues (stale facts, broken edges). Others just report (orphans, gaps).

Checks:
  A) Orphans      — nodes with zero relates edges (warning, no auto-fix)
  B) Contradictions — handled by reflector, reported via reflector stats
  C) Stale facts  — expired or decayed memories (info, auto-fix: soft-delete)
  D) Missing links — handled by linker, reported via linker stats
  E) Gaps         — categories with < 3 memories (info, no auto-fix)
  F) Data quality — broken edges, empty content (error, auto-fix)
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.constants import MEMORY_CATEGORIES
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Finding helper
# ---------------------------------------------------------------------------


def _finding(
    check: str,
    severity: str,
    node_id: str,
    detail: str,
    action: dict | None = None,
    fixed: bool = False,
) -> dict:
    """Build a standardized finding dict."""
    return {
        "check": check,
        "severity": severity,
        "node_id": node_id,
        "detail": detail,
        "action": action,
        "fixed": fixed,
    }


# ---------------------------------------------------------------------------
# Check A: Orphans
# ---------------------------------------------------------------------------


async def check_orphans(db: Any = None) -> list[dict]:
    """
    Find active memories and entities with no relates edges.

    These are isolated nodes — not connected to anything in the graph.
    Flagged but not auto-fixed (they might be valuable).

    Returns list of findings with severity=warning.
    """
    async def _run(conn: Any) -> list[dict]:
        # Two-step approach: SurrealDB v3 subqueries with edge fields
        # (in/out) are unreliable, so we do the set difference in Python.

        # Step 1: Get all node IDs that appear in any relates edge
        edge_ins = await query(conn, "SELECT in FROM relates")
        edge_outs = await query(conn, "SELECT out FROM relates")
        linked_ids: set[str] = set()
        for row in (edge_ins or []):
            linked_ids.add(str(row.get("in", "")))
        for row in (edge_outs or []):
            linked_ids.add(str(row.get("out", "")))

        # Step 2: Get all active memories and entities
        all_memories = await query(conn, """
            SELECT id, content, category FROM memory
            WHERE is_active = true
        """)
        all_entities = await query(conn, """
            SELECT id, name, type FROM entity
        """)

        # Step 3: Find orphans (not in linked_ids)
        orphan_memories = [
            m for m in (all_memories or [])
            if str(m["id"]) not in linked_ids
        ]
        orphan_entities = [
            e for e in (all_entities or [])
            if str(e["id"]) not in linked_ids
        ]

        findings = []

        for mem in (orphan_memories or []):
            content_preview = (mem.get("content") or "")[:80]
            findings.append(_finding(
                check="orphan",
                severity="warning",
                node_id=str(mem["id"]),
                detail=f"Memory has no connections: {content_preview}",
                action={
                    "tool": "qmemory_link",
                    "params": {
                        "from_id": str(mem["id"]),
                        "to_id": "?",
                        "type": "related_to",
                    },
                },
            ))

        for ent in (orphan_entities or []):
            findings.append(_finding(
                check="orphan",
                severity="warning",
                node_id=str(ent["id"]),
                detail=(
                    f"Entity '{ent.get('name', '?')}' "
                    f"({ent.get('type', '?')}) has no connections"
                ),
                action={
                    "tool": "qmemory_link",
                    "params": {
                        "from_id": str(ent["id"]),
                        "to_id": "?",
                        "type": "related_to",
                    },
                },
            ))

        logger.info("check_orphans: found %d orphans", len(findings))
        return findings

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


# ---------------------------------------------------------------------------
# Check C: Stale facts
# ---------------------------------------------------------------------------


async def check_stale(db: Any = None) -> list[dict]:
    """
    Find memories past valid_until or with salience < 0.1.

    Auto-fix: soft-delete them (is_active = false).

    Returns list of findings with severity=info and fixed=True.
    """
    async def _run(conn: Any) -> list[dict]:
        stale = await query(conn, """
            SELECT id, content, salience, valid_until FROM memory
            WHERE is_active = true
            AND (valid_until < time::now() OR salience < 0.1)
        """)

        if not stale:
            logger.info("check_stale: no stale memories found")
            return []

        findings = []
        for mem in stale:
            content_preview = (mem.get("content") or "")[:80]
            mem_id = str(mem["id"])
            table, suffix = mem_id.split(":", 1)

            # Auto-fix: soft-delete
            await query(
                conn,
                f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()",
            )

            reason = (
                "expired"
                if mem.get("valid_until")
                else f"salience={mem.get('salience', 0):.2f}"
            )
            findings.append(_finding(
                check="stale",
                severity="info",
                node_id=mem_id,
                detail=f"Stale memory ({reason}): {content_preview}",
                fixed=True,
            ))

        logger.info(
            "check_stale: found and soft-deleted %d stale memories",
            len(findings),
        )
        return findings

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


# ---------------------------------------------------------------------------
# Check E: Gaps in coverage
# ---------------------------------------------------------------------------


async def check_gaps(db: Any = None) -> list[dict]:
    """
    Find categories with surprisingly few memories.

    Flags categories with < 3 active memories and zero persons.

    Returns list of findings with severity=info.
    """
    async def _run(conn: Any) -> list[dict]:
        counts = await query(conn, """
            SELECT category, count() AS cnt FROM memory
            WHERE is_active = true
            GROUP BY category
        """)

        cat_counts: dict[str, int] = {}
        for row in (counts or []):
            cat_counts[row["category"]] = row["cnt"]

        findings = []

        for cat in MEMORY_CATEGORIES:
            count = cat_counts.get(cat, 0)
            if count < 3:
                findings.append(_finding(
                    check="gap",
                    severity="info",
                    node_id=f"category:{cat}",
                    detail=f"Category '{cat}' has only {count} active memories (< 3)",
                ))

        # Check for zero persons
        person_count = await query(conn, """
            SELECT count() AS cnt FROM entity
            WHERE type = 'person'
            GROUP ALL
        """)
        p_count = 0
        if person_count and isinstance(person_count, list) and len(person_count) > 0:
            p_count = person_count[0].get("cnt", 0)

        if p_count == 0:
            findings.append(_finding(
                check="gap",
                severity="info",
                node_id="entity:person",
                detail="No person entities exist in the graph",
            ))

        logger.info("check_gaps: found %d gaps", len(findings))
        return findings

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


# ---------------------------------------------------------------------------
# Check F: Data quality
# ---------------------------------------------------------------------------


async def check_quality(db: Any = None) -> list[dict]:
    """
    Find data quality issues: broken edges, empty content, invalid categories.

    Auto-fix: delete broken edges, soft-delete empty memories.

    Returns list of findings with severity=error.
    """
    async def _run(conn: Any) -> list[dict]:
        findings = []

        # 1. Edges pointing to inactive/deleted nodes
        # Two-step approach: SurrealDB v3 subqueries with edge fields unreliable
        inactive_result = await query(conn, """
            SELECT id FROM memory WHERE is_active = false
        """)
        inactive_ids: set[str] = {
            str(m["id"]) for m in (inactive_result or [])
        }

        all_edges = await query(conn, """
            SELECT id, in, out, relationship_type FROM relates
        """)
        broken_edges = [
            e for e in (all_edges or [])
            if str(e.get("in", "")) in inactive_ids
            or str(e.get("out", "")) in inactive_ids
        ]

        for edge in (broken_edges or []):
            edge_id = str(edge["id"])
            # Auto-fix: hard-delete the broken edge
            await query(conn, f"DELETE {edge_id}")
            findings.append(_finding(
                check="quality",
                severity="error",
                node_id=edge_id,
                detail=(
                    f"Edge points to inactive node: "
                    f"{edge.get('relationship_type', '?')}"
                ),
                fixed=True,
            ))

        # 2. Memories with empty content
        empty_memories = await query(conn, """
            SELECT id FROM memory
            WHERE is_active = true
            AND (content = '' OR content IS NONE)
        """)

        for mem in (empty_memories or []):
            mem_id = str(mem["id"])
            table, suffix = mem_id.split(":", 1)
            # Auto-fix: soft-delete
            await query(
                conn,
                f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()",
            )
            findings.append(_finding(
                check="quality",
                severity="error",
                node_id=mem_id,
                detail="Memory has empty content",
                fixed=True,
            ))

        # 3. Memories with invalid categories
        invalid_cats = await query(conn, """
            SELECT id, category FROM memory
            WHERE is_active = true
            AND category NOT IN $valid_cats
        """, {"valid_cats": MEMORY_CATEGORIES})

        for mem in (invalid_cats or []):
            findings.append(_finding(
                check="quality",
                severity="error",
                node_id=str(mem["id"]),
                detail=f"Invalid category: '{mem.get('category', '?')}'",
            ))

        logger.info("check_quality: found %d quality issues", len(findings))
        return findings

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------


async def run_linter_checks(db: Any = None) -> list[dict]:
    """
    Run all linter checks and return combined findings.

    Checks B (contradictions) and D (missing links) are handled by the
    reflector and linker jobs respectively — not run here.
    """
    async def _run(conn: Any) -> list[dict]:
        all_findings = []
        all_findings.extend(await check_orphans(db=conn))
        all_findings.extend(await check_stale(db=conn))
        all_findings.extend(await check_gaps(db=conn))
        all_findings.extend(await check_quality(db=conn))
        return all_findings

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)
