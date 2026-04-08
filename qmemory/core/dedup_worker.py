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
        result["found_work"],
        result["dupes_merged"],
        result["elapsed_ms"],
    )
    return result


async def _run_cycle(db: Any) -> dict:
    """Internal cycle — runs with active DB connection."""

    # Step 1: Get distinct category+scope groups with 2+ memories
    groups = await query(db, """
        SELECT category, scope, count() AS cnt FROM memory
        WHERE is_active = true
        GROUP BY category, scope
    """)

    # Filter groups with at least 2 memories
    groups = [g for g in (groups or []) if g.get("cnt", 0) >= 2]

    if not groups:
        return {"found_work": False, "dupes_merged": 0, "pairs_checked": 0}

    # Step 2: For each group, find candidate pairs via word overlap
    all_candidates: list[tuple[dict, dict]] = []

    for group in groups:
        cat = group["category"]
        scope = group["scope"]

        memories = await query(db, """
            SELECT id, content, salience, created_at FROM memory
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
        return {
            "found_work": True,
            "dupes_merged": 0,
            "pairs_checked": len(candidates),
        }

    if not isinstance(result, dict):
        return {
            "found_work": True,
            "dupes_merged": 0,
            "pairs_checked": len(candidates),
        }

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
        await query(
            db,
            f"UPDATE {table}:`{suffix}` SET is_active = false, updated_at = time::now()",
        )
        dupes_merged += 1
        logger.info(
            "dedup_worker: soft-deleted duplicate %s — %s",
            remove_id,
            dupe.get("reason", ""),
        )

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

    return (
        "You are a memory deduplication system. For each pair below, decide "
        "if they are duplicates (same core fact, different wording). If yes, "
        "keep the one with higher salience and remove the other.\n\n"
        f"{chr(10).join(lines)}\n\n"
        "Return duplicates found. For each, specify keep_id, remove_id, "
        "and reason. If a pair is NOT a duplicate, don't include it."
    )
