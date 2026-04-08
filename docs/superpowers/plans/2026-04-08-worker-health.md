# Worker Health System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a linter job + dedup worker + health report system to the existing background worker, plus a `qmemory_health` MCP tool for agents to read graph health.

**Architecture:** The worker loop (`qmemory/worker/__init__.py`) already runs linker, decay, and reflector cycles. We add two new jobs (linter + dedup_worker) and a report-saving step. A new `health_report` table in SurrealDB stores findings. The `qmemory_health` MCP tool reads the latest report.

**Tech Stack:** Python 3.11+, SurrealDB 3.0, Claude Haiku (via `qmemory/llm/`), FastMCP, Click CLI

**Existing code that does NOT need changes:**
- `qmemory/core/linker.py` — fully implemented, runs every worker cycle
- `qmemory/core/decay.py` — fully implemented, runs every worker cycle
- `qmemory/core/reflector.py` — fully implemented, handles contradictions + compressions + patterns

**Note on file locations:** The spec suggested `worker/jobs/` but the existing codebase puts all worker job logic in `qmemory/core/` (linker.py, decay.py, reflector.py). We follow that convention — new jobs go in `core/` too.

---

### Task 1: Add health_report table to schema

**Files:**
- Modify: `qmemory/db/schema.surql` (append after line 203, after metrics table)

- [ ] **Step 1: Write the schema addition**

Add to the end of `qmemory/db/schema.surql`:

```sql
-- ============================================================
-- NODE: health_report (worker health check results)
-- ============================================================

DEFINE TABLE IF NOT EXISTS health_report SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS created_at           ON health_report TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS orphans_found        ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS contradictions_found ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS stale_found          ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS links_created        ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS dupes_merged         ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS gaps                 ON health_report TYPE array    DEFAULT [];
DEFINE FIELD IF NOT EXISTS quality_issues       ON health_report TYPE int      DEFAULT 0;
DEFINE FIELD IF NOT EXISTS findings             ON health_report TYPE array    DEFAULT [];
DEFINE FIELD IF NOT EXISTS duration_ms          ON health_report TYPE int      DEFAULT 0;

DEFINE INDEX IF NOT EXISTS idx_health_report_created ON health_report FIELDS created_at;
```

- [ ] **Step 2: Verify schema applies cleanly**

Run: `uv run qmemory schema`
Expected: "Schema applied successfully."

- [ ] **Step 3: Verify table exists**

Run: `uv run qmemory status`
Expected: `health_report` appears in the table list with count 0.

- [ ] **Step 4: Add health_report to CLI status table list**

In `qmemory/cli.py`, add `"health_report"` to the `tables` list (after `"metrics"`):

```python
    tables = [
        "memory",
        "entity",
        "session",
        "message",
        "tool_call",
        "relates",
        "scratchpad",
        "metrics",
        "health_report",
    ]
```

- [ ] **Step 5: Commit**

```bash
git add qmemory/db/schema.surql qmemory/cli.py
git commit -m "feat: add health_report table to schema"
```

---

### Task 2: Create core/linter.py — 6 health checks

**Files:**
- Create: `qmemory/core/linter.py`
- Test: `tests/test_core/test_linter.py`

Each check function returns a list of finding dicts. A finding looks like:

```python
{
    "check": "orphan",           # orphan|stale|gap|quality|contradiction|linker|dedup
    "severity": "warning",       # info|warning|error
    "node_id": "memory:mem123",  # the affected node
    "detail": "Memory has no connections",
    "action": {"tool": "qmemory_link", "params": {...}} | None,
    "fixed": False,              # True if auto-fixed
}
```

- [ ] **Step 1: Write the failing tests**

Create `tests/test_core/test_linter.py`:

```python
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
    """An edge pointing to a non-existent node should be found and auto-fixed."""
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
    # Create a fake target memory that we'll delete
    await query(db, """
        CREATE memory:fake1 SET
            content = 'I will be deleted',
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_linter.py -v`
Expected: All tests FAIL with `ImportError: cannot import name 'check_orphans' from 'qmemory.core.linter'`

- [ ] **Step 3: Write the linter implementation**

Create `qmemory/core/linter.py`:

```python
"""
Linter — 6 health checks for the memory graph.

Each check function returns a list of finding dicts. Some checks auto-fix
issues (stale facts, broken edges). Others just report (orphans, gaps).

Checks:
  A) Orphans      — nodes with zero relates edges (warning, no auto-fix)
  B) Contradictions — handled by reflector, reported via linker stats
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
# Finding type
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
    We flag them but don't auto-fix (they might be valuable).

    Returns list of findings with severity=warning.
    """
    async def _run(conn: Any) -> list[dict]:
        # Find active memories not appearing in any relates edge
        orphan_memories = await query(conn, """
            SELECT id, content, category FROM memory
            WHERE is_active = true
            AND id NOT IN (SELECT in FROM relates)
            AND id NOT IN (SELECT out FROM relates)
        """)

        # Find entities not appearing in any relates edge
        orphan_entities = await query(conn, """
            SELECT id, name, type FROM entity
            WHERE id NOT IN (SELECT in FROM relates)
            AND id NOT IN (SELECT out FROM relates)
        """)

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
                    "params": {"from_id": str(mem["id"]), "to_id": "?", "type": "related_to"},
                },
            ))

        for ent in (orphan_entities or []):
            findings.append(_finding(
                check="orphan",
                severity="warning",
                node_id=str(ent["id"]),
                detail=f"Entity '{ent.get('name', '?')}' ({ent.get('type', '?')}) has no connections",
                action={
                    "tool": "qmemory_link",
                    "params": {"from_id": str(ent["id"]), "to_id": "?", "type": "related_to"},
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
        # Find expired or ultra-low-salience memories
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
            # Parse ID for UPDATE
            mem_id = str(mem["id"])
            table, suffix = mem_id.split(":", 1)

            # Auto-fix: soft-delete
            await query(conn, f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()")

            reason = "expired" if mem.get("valid_until") else f"salience={mem.get('salience', 0):.2f}"
            findings.append(_finding(
                check="stale",
                severity="info",
                node_id=mem_id,
                detail=f"Stale memory ({reason}): {content_preview}",
                fixed=True,
            ))

        logger.info("check_stale: found and soft-deleted %d stale memories", len(findings))
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
        # Count memories per category
        counts = await query(conn, """
            SELECT category, count() AS cnt FROM memory
            WHERE is_active = true
            GROUP BY category
        """)

        # Build a map: category -> count
        cat_counts: dict[str, int] = {}
        for row in (counts or []):
            cat_counts[row["category"]] = row["cnt"]

        findings = []

        # Check all 8 categories
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
        broken_edges = await query(conn, """
            SELECT id, in, out, relationship_type FROM relates
            WHERE in IN (SELECT id FROM memory WHERE is_active = false)
            OR out IN (SELECT id FROM memory WHERE is_active = false)
        """)

        for edge in (broken_edges or []):
            edge_id = str(edge["id"])
            # Auto-fix: hard-delete the broken edge
            await query(conn, f"DELETE {edge_id}")
            findings.append(_finding(
                check="quality",
                severity="error",
                node_id=edge_id,
                detail=f"Edge points to inactive node: {edge.get('relationship_type', '?')}",
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
            await query(conn, f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()")
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
    Run all 6 linter checks and return combined findings.

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core/test_linter.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add qmemory/core/linter.py tests/test_core/test_linter.py
git commit -m "feat: add linter with 6 health checks (orphans, stale, gaps, quality)"
```

---

### Task 3: Create core/dedup_worker.py — batch dedup

**Files:**
- Create: `qmemory/core/dedup_worker.py`
- Test: `tests/test_core/test_dedup_worker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_core/test_dedup_worker.py`:

```python
"""Tests for the batch dedup worker."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from qmemory.core.dedup_worker import run_dedup_cycle
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_dedup_finds_and_merges_duplicates(db):
    """Two very similar memories in same category should be merged."""
    # Create two near-identical memories
    await query(db, """
        CREATE memory:dup1 SET
            content = 'Qusai prefers dark mode in all applications',
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
            content = 'Qusai likes using dark mode everywhere',
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
            {"keep_id": "memory:dup1", "remove_id": "memory:dup2", "reason": "Same fact about dark mode"}
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_dedup_worker.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write the dedup worker implementation**

Create `qmemory/core/dedup_worker.py`:

```python
"""
Batch Dedup Worker — finds duplicate memories missed by save-time dedup.

Different from core/dedup.py which runs per-save. This scans the entire
graph for near-duplicate pairs within the same category+scope, asks Haiku
to confirm, and soft-deletes the weaker duplicate.

Algorithm:
  1. For each category+scope group with 2+ memories, fetch all active memories
  2. Use Jaccard word similarity to find candidate pairs (> 0.5 overlap)
  3. Send candidate pairs to Haiku: "Are these duplicates?"
  4. For confirmed dupes, soft-delete the one with lower salience
  5. Return stats
"""
from __future__ import annotations

import logging
import time
from typing import Any

from qmemory.core.token_budget import can_spend, record_spend
from qmemory.db.client import get_db, query
from qmemory.llm import get_llm

logger = logging.getLogger(__name__)

# Maximum pairs to send to LLM per cycle (cost control)
MAX_PAIRS_PER_CYCLE = 10


async def run_dedup_cycle(db: Any = None) -> dict:
    """
    One dedup cycle. Scans for duplicate memories and merges them.

    Returns:
        dict with found_work, dupes_merged, pairs_checked, elapsed_ms
    """
    start = time.monotonic()

    if db is not None:
        result = await _run_cycle(db)
    else:
        async with get_db() as conn:
            result = await _run_cycle(conn)

    result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 1)
    logger.info(
        "dedup_cycle: found_work=%s merged=%d elapsed=%.1fms",
        result["found_work"], result["dupes_merged"], result["elapsed_ms"],
    )
    return result


async def _run_cycle(db: Any) -> dict:
    """Internal cycle — runs with active DB connection."""

    # Step 1: Get distinct category+scope groups
    groups = await query(db, """
        SELECT category, scope, count() AS cnt FROM memory
        WHERE is_active = true
        GROUP BY category, scope
        HAVING count() >= 2
    """)

    if not groups:
        return {"found_work": False, "dupes_merged": 0, "pairs_checked": 0}

    # Step 2: For each group, find candidate pairs via word overlap
    all_candidates: list[tuple[dict, dict]] = []

    for group in groups:
        cat = group["category"]
        scope = group["scope"]

        memories = await query(db, """
            SELECT id, content, salience FROM memory
            WHERE is_active = true AND category = $cat AND scope = $scope
            ORDER BY created_at DESC
            LIMIT 30
        """, {"cat": cat, "scope": scope})

        if not memories or len(memories) < 2:
            continue

        # Find pairs with high word overlap (Jaccard > 0.5)
        for i, m1 in enumerate(memories):
            words1 = set((m1.get("content") or "").lower().split())
            for m2 in memories[i + 1:]:
                words2 = set((m2.get("content") or "").lower().split())
                if not words1 or not words2:
                    continue
                jaccard = len(words1 & words2) / len(words1 | words2)
                if jaccard > 0.5:
                    all_candidates.append((m1, m2))

    if not all_candidates:
        return {"found_work": False, "dupes_merged": 0, "pairs_checked": 0}

    # Limit pairs per cycle
    candidates = all_candidates[:MAX_PAIRS_PER_CYCLE]

    # Step 3: Check token budget
    if not can_spend(2000, priority="low"):
        logger.info("dedup_worker: token budget exhausted")
        return {"found_work": True, "dupes_merged": 0, "pairs_checked": 0}

    # Step 4: Ask Haiku which pairs are true duplicates
    prompt = _build_prompt(candidates)

    try:
        llm = get_llm("haiku")
        schema = {
            "type": "object",
            "properties": {
                "duplicates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "keep_id": {"type": "string"},
                            "remove_id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["keep_id", "remove_id", "reason"],
                    },
                },
            },
            "required": ["duplicates"],
        }
        result = await llm.complete(prompt, schema=schema)
        record_spend(2000, source="dedup_worker", priority="low")
    except Exception as e:
        logger.warning("dedup_worker: LLM call failed: %s", e)
        return {"found_work": True, "dupes_merged": 0, "pairs_checked": len(candidates)}

    if not isinstance(result, dict):
        return {"found_work": True, "dupes_merged": 0, "pairs_checked": len(candidates)}

    # Step 5: Validate and apply merges
    valid_ids = set()
    for m1, m2 in candidates:
        valid_ids.add(str(m1["id"]))
        valid_ids.add(str(m2["id"]))

    dupes_merged = 0
    for dupe in result.get("duplicates", []):
        remove_id = dupe.get("remove_id", "")
        if remove_id not in valid_ids:
            continue
        if ":" not in remove_id:
            continue

        table, suffix = remove_id.split(":", 1)
        await query(db, f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()")
        dupes_merged += 1
        logger.info("dedup_worker: soft-deleted duplicate %s — %s", remove_id, dupe.get("reason", ""))

    return {
        "found_work": True,
        "dupes_merged": dupes_merged,
        "pairs_checked": len(candidates),
    }


def _build_prompt(candidates: list[tuple[dict, dict]]) -> str:
    """Build the LLM prompt listing candidate duplicate pairs."""
    lines = []
    for i, (m1, m2) in enumerate(candidates, 1):
        c1 = (m1.get("content") or "")[:200]
        c2 = (m2.get("content") or "")[:200]
        s1 = m1.get("salience", 0.5)
        s2 = m2.get("salience", 0.5)
        lines.append(
            f"Pair {i}:\n"
            f"  A: ID={m1['id']} (salience={s1}) | {c1}\n"
            f"  B: ID={m2['id']} (salience={s2}) | {c2}"
        )

    return f"""You are a memory deduplication system. For each pair below, decide if they
are duplicates (same core fact, different wording). If yes, keep the one with
higher salience and remove the other.

{chr(10).join(lines)}

Return duplicates found. For each, specify keep_id, remove_id, and reason.
If a pair is NOT a duplicate, don't include it."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core/test_dedup_worker.py -v`
Expected: Both tests PASS

- [ ] **Step 5: Commit**

```bash
git add qmemory/core/dedup_worker.py tests/test_core/test_dedup_worker.py
git commit -m "feat: add batch dedup worker for missed duplicates"
```

---

### Task 4: Create core/health.py — report reading and saving

**Files:**
- Create: `qmemory/core/health.py`
- Test: `tests/test_core/test_health.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_core/test_health.py`:

```python
"""Tests for health report reading/saving."""
from __future__ import annotations

import pytest

from qmemory.core.health import get_latest_report, save_health_report
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_save_and_read_report(db):
    """Save a report, then read it back."""
    findings = [
        {"check": "orphan", "severity": "warning", "node_id": "memory:mem1",
         "detail": "No connections", "action": None, "fixed": False},
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
        {"check": "orphan", "severity": "warning", "node_id": "memory:mem1",
         "detail": "No connections", "action": None, "fixed": False},
        {"check": "stale", "severity": "info", "node_id": "memory:mem2",
         "detail": "Expired", "action": None, "fixed": True},
        {"check": "quality", "severity": "error", "node_id": "relates:rel1",
         "detail": "Broken edge", "action": None, "fixed": True},
    ]
    await save_health_report(
        orphans_found=1, contradictions_found=0, stale_found=1,
        links_created=0, dupes_merged=0, gaps=[], quality_issues=1,
        findings=findings, duration_ms=500, db=db,
    )

    report = await get_latest_report(check="orphans", db=db)
    assert report is not None
    assert len(report["findings"]) == 1
    assert report["findings"][0]["check"] == "orphan"

    report = await get_latest_report(check="quality", db=db)
    assert len(report["findings"]) == 1
    assert report["findings"][0]["check"] == "quality"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_health.py -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Write the health module**

Create `qmemory/core/health.py`:

```python
"""
Health Report — save and read worker health check results.

The worker saves a report after each cycle. The qmemory_health MCP tool
reads the latest report. This module handles both directions.
"""
from __future__ import annotations

import json
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

    # SurrealDB stores arrays natively — pass them as params
    surql = """
        CREATE health_report SET
            id = type::record('health_report', $id),
            orphans_found = $orphans,
            contradictions_found = $contradictions,
            stale_found = $stale,
            links_created = $links,
            dupes_merged = $dupes,
            gaps = $gaps,
            quality_issues = $quality,
            findings = $findings,
            duration_ms = $duration
    """
    params = {
        "id": report_id,
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
        SELECT * FROM health_report
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core/test_health.py -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add qmemory/core/health.py tests/test_core/test_health.py
git commit -m "feat: add health report save/read module"
```

---

### Task 5: Update worker loop + CLI

**Files:**
- Modify: `qmemory/worker/__init__.py`
- Modify: `qmemory/cli.py`

- [ ] **Step 1: Rewrite the worker loop**

Replace the contents of `qmemory/worker/__init__.py` with the updated loop that:
- Adds linter and dedup_worker jobs
- Saves a health report after each cycle
- Supports `--interval` and `--once` flags via parameters

```python
"""
Qmemory Background Worker — maintains graph health automatically.

Runs 5 jobs per cycle:
  1. Linker     — finds and creates edges between unlinked memories
  2. Dedup      — finds and merges duplicate memories
  3. Decay      — fades old memories' salience scores
  4. Reflector  — finds patterns, contradictions, ghost entities
  5. Linter     — 6 health checks (orphans, stale, gaps, quality)

After all jobs, saves a health report to the database.

Default: runs once per day (86400s). Use --once for single run.
Pausable: touch ~/.qmemory/worker-paused to pause.
Token-budgeted: respects hourly LLM token limits.

Usage:
    qmemory worker                  # once per day
    qmemory worker --interval 3600  # every hour
    qmemory worker --once           # run once and exit
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PAUSE_FILE = Path.home() / ".qmemory" / "worker-paused"
DEFAULT_INTERVAL = 86400  # once per day
REFLECTOR_EVERY_N = 2


async def run_worker(interval: int = DEFAULT_INTERVAL, once: bool = False):
    """
    Main worker loop — runs all maintenance jobs, saves health report.

    Args:
        interval: Seconds between cycles. Default: 86400 (once per day).
        once:     If True, run one cycle and exit (for testing/cron).
    """
    from qmemory.core.token_budget import init_token_budget

    init_token_budget("balanced")
    cycle = 0

    logger.info(
        "worker.started interval=%ds once=%s",
        interval, once,
    )

    while True:
        # Pause check
        if PAUSE_FILE.exists():
            logger.debug("worker.paused file=%s", PAUSE_FILE)
            if once:
                logger.info("worker.paused and --once set, exiting")
                return
            await asyncio.sleep(60)
            continue

        cycle += 1
        cycle_start = time.monotonic()

        # Accumulators for the health report
        all_findings: list[dict] = []
        links_created = 0
        dupes_merged = 0
        contradictions_found = 0

        try:
            # --- Job 1: Linker ---
            from qmemory.core.linker import run_linker_cycle

            linker_result = await run_linker_cycle()
            links_created = linker_result.get("edges_created", 0)
            logger.info("worker.linker cycle=%d result=%s", cycle, linker_result)

            # --- Job 2: Dedup ---
            from qmemory.core.dedup_worker import run_dedup_cycle

            dedup_result = await run_dedup_cycle()
            dupes_merged = dedup_result.get("dupes_merged", 0)
            logger.info("worker.dedup cycle=%d result=%s", cycle, dedup_result)

            # --- Job 3: Decay ---
            from qmemory.core.decay import run_salience_decay

            decay_result = await run_salience_decay()
            logger.info("worker.decay cycle=%d result=%s", cycle, decay_result)

            # --- Job 4: Reflector (every Nth cycle) ---
            if cycle % REFLECTOR_EVERY_N == 0:
                from qmemory.core.reflector import run_reflector_cycle

                reflect_result = await run_reflector_cycle()
                contradictions_found = reflect_result.get("contradictions", 0)
                logger.info("worker.reflector cycle=%d result=%s", cycle, reflect_result)

            # --- Job 5: Linter ---
            from qmemory.core.linter import run_linter_checks

            linter_findings = await run_linter_checks()
            all_findings.extend(linter_findings)
            logger.info("worker.linter cycle=%d findings=%d", cycle, len(linter_findings))

        except Exception:
            logger.exception("worker.cycle_error cycle=%d", cycle)

        # --- Save health report ---
        elapsed_ms = int((time.monotonic() - cycle_start) * 1000)

        orphans = len([f for f in all_findings if f["check"] == "orphan"])
        stale = len([f for f in all_findings if f["check"] == "stale"])
        gaps = [f["node_id"].split(":")[-1] for f in all_findings if f["check"] == "gap"]
        quality = len([f for f in all_findings if f["check"] == "quality"])

        # Add linker/dedup findings to the report
        if links_created > 0:
            all_findings.append({
                "check": "linker",
                "severity": "info",
                "node_id": "worker:linker",
                "detail": f"Linker created {links_created} new edges",
                "action": None,
                "fixed": True,
            })
        if dupes_merged > 0:
            all_findings.append({
                "check": "dedup",
                "severity": "info",
                "node_id": "worker:dedup",
                "detail": f"Dedup merged {dupes_merged} duplicate memories",
                "action": None,
                "fixed": True,
            })

        try:
            from qmemory.core.health import save_health_report

            await save_health_report(
                orphans_found=orphans,
                contradictions_found=contradictions_found,
                stale_found=stale,
                links_created=links_created,
                dupes_merged=dupes_merged,
                gaps=gaps,
                quality_issues=quality,
                findings=all_findings,
                duration_ms=elapsed_ms,
            )
        except Exception:
            logger.exception("worker.report_save_error cycle=%d", cycle)

        logger.info(
            "worker.cycle_done cycle=%d elapsed_ms=%d findings=%d",
            cycle, elapsed_ms, len(all_findings),
        )

        # Exit if --once
        if once:
            logger.info("worker.once_done exiting")
            return

        await asyncio.sleep(interval)


def main():
    """Entry point for `python -m qmemory.worker`."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        logger.info("worker.stopped reason=keyboard_interrupt")
```

- [ ] **Step 2: Update CLI to pass --interval and --once flags**

Replace the `worker` command in `qmemory/cli.py`:

```python
@main.command()
@click.option("--interval", default=86400, show_default=True,
              help="Seconds between cycles (default: once per day).")
@click.option("--once", is_flag=True, default=False,
              help="Run one cycle and exit (for testing/cron).")
def worker(interval, once):
    """Run the background worker (linker, dedup, linter, reflector, decay)."""
    import logging

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from qmemory.worker import run_worker

    try:
        asyncio.run(run_worker(interval=interval, once=once))
    except KeyboardInterrupt:
        click.echo("Worker stopped.")
```

- [ ] **Step 3: Test the worker runs with --once**

Run: `uv run qmemory worker --once`
Expected: Worker runs one full cycle, prints logs for each job, saves health report, and exits cleanly. No crash.

- [ ] **Step 4: Verify health report was saved**

Run: `uv run qmemory status`
Expected: `health_report` shows count >= 1.

- [ ] **Step 5: Commit**

```bash
git add qmemory/worker/__init__.py qmemory/cli.py
git commit -m "feat: integrate linter + dedup into worker, add --interval and --once flags"
```

---

### Task 6: Add qmemory_health MCP tool to both transports

**Files:**
- Modify: `qmemory/mcp/server.py` (stdio transport)
- Modify: `qmemory/app/main.py` (HTTP transport)

- [ ] **Step 1: Add qmemory_health to stdio transport**

Add after the `qmemory_books` tool in `qmemory/mcp/server.py`:

```python
# ---------------------------------------------------------------------------
# Tool 9: qmemory_health
# Read-only — reads the latest worker health report.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_health(
    check: str = "all",
) -> str:
    """Check the health of your memory graph.

    Returns the latest health report from the background worker.
    Shows orphan nodes, stale facts, missing links, data quality issues,
    and coverage gaps — with suggested actions for each finding.

    The worker runs these checks daily. This tool reads the latest report.

    Args:
        check: Which check to show. Options:
               all            — everything (default)
               orphans        — nodes with zero connections
               contradictions — conflicting memories
               stale          — expired or decayed memories
               missing_links  — links created by the linker
               gaps           — categories with few memories
               quality        — broken edges, empty content

    Returns JSON with summary counts, detailed findings, and suggested
    actions for each issue. Run 'qmemory worker --once' first if no
    report exists yet.
    """
    from qmemory.core.health import get_latest_report

    result = await get_latest_report(check=check)
    if result is None:
        return json.dumps({
            "status": "no_report",
            "message": "No health report found. Run 'qmemory worker --once' to generate one.",
            "actions": [
                {"tool": "shell", "command": "qmemory worker --once",
                 "description": "Run the worker once to generate a health report"}
            ],
        }, ensure_ascii=False)
    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 2: Add qmemory_health to HTTP transport**

Add after the `qmemory_books` tool in `qmemory/app/main.py`:

```python
# ---------------------------------------------------------------------------
# Tool 9: qmemory_health (read-only)
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_health(
    check: str = "all",
) -> str:
    """Check the health of your memory graph.

    Returns the latest health report from the background worker.
    Shows orphan nodes, stale facts, missing links, data quality issues,
    and coverage gaps — with suggested actions for each finding.

    Args:
        check: Which check to show. Options:
               all            — everything (default)
               orphans        — nodes with zero connections
               contradictions — conflicting memories
               stale          — expired or decayed memories
               missing_links  — links created by the linker
               gaps           — categories with few memories
               quality        — broken edges, empty content

    Returns JSON with summary counts, detailed findings, and actions.
    """
    start = time.monotonic()
    logger.info("Tool call: qmemory_health(check=%s)", check)

    from qmemory.core.health import get_latest_report

    result = await get_latest_report(check=check)

    elapsed = time.monotonic() - start
    logger.info("qmemory_health completed in %.2fs", elapsed)

    if result is None:
        return json.dumps({
            "status": "no_report",
            "message": "No health report found. Run 'qmemory worker --once' to generate one.",
        }, ensure_ascii=False)
    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 3: Update tool count in module docstrings**

Update the docstring at the top of `qmemory/mcp/server.py` from "7 tools" to "9 tools" and add `qmemory_health` to the list.

Update the docstring at the top of `qmemory/app/main.py` from "7 tools" to "9 tools".

- [ ] **Step 4: Commit**

```bash
git add qmemory/mcp/server.py qmemory/app/main.py
git commit -m "feat: add qmemory_health MCP tool to both transports"
```

---

### Task 7: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the MCP Tools table**

In the MCP Tools section, change "9 total" to "10 total" and add:

```markdown
| `qmemory_health` | Yes | Check graph health — orphans, stale, gaps, quality |
```

- [ ] **Step 2: Update the worker CLI command**

In the Commands section, add:

```markdown
qmemory worker --once             # run one maintenance cycle and exit
qmemory worker --interval 3600    # run every hour (default: daily)
```

- [ ] **Step 3: Update Architecture section**

Add to the `core/` section:

```
    health.py        #   Read/save worker health reports
    linter.py        #   6 graph health checks (orphans, stale, gaps, quality)
    dedup_worker.py  #   Batch dedup via word similarity + Haiku
```

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md with health tool and worker docs"
```

---

### Task 8: Integration test — full worker + health tool cycle

**Files:**
- Create: `tests/test_worker/test_integration.py`

- [ ] **Step 1: Create test directory and write the integration test**

Create `tests/test_worker/__init__.py` (empty file) and `tests/test_worker/test_integration.py`:

```python
"""Integration test: worker cycle saves report, health tool reads it."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from qmemory.core.health import get_latest_report, save_health_report
from qmemory.core.linter import run_linter_checks
from qmemory.db.client import query


@pytest.mark.asyncio
async def test_linter_then_report_then_read(db):
    """Full flow: create data, run linter, save report, read via health tool."""
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
```

- [ ] **Step 2: Run the integration test**

Run: `uv run pytest tests/test_worker/test_integration.py -v`
Expected: PASS

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All new tests pass. Existing tests still pass (the 9 known failures from SurrealDB v3 edge syntax are expected).

- [ ] **Step 4: Commit**

```bash
git add tests/test_worker/test_integration.py
git commit -m "test: add integration test for worker health cycle"
```
