"""
Core Salience Decay

3-tier biological memory decay model — memories fade differently based on
how frequently they've been recalled.

Tier 1: Never recalled (recall_count = 0) and older than 7 days
  → salience *= 0.90 (10% decay per cycle, floor: 0.1)
  These are facts the agent was told but never used. They fade fastest.

Tier 2: Recalled but stale (recall_count 1-4, last_recalled > 14 days ago)
  → salience *= 0.98 (2% decay per cycle, floor: 0.1)
  These were useful once but haven't been accessed recently. Gentle decay.

Tier 3: Cemented (recall_count >= 5)
  → No decay, but enforce floor of 0.5
  These are core knowledge — recalled often enough to be "cemented" in memory.
  They never drop below 0.5 salience, even if somehow set lower.

This is a pure DB operation — zero LLM cost. Safe to run on a schedule
(e.g. every 6 hours via the background worker).

Design decisions:
  - Uses SurrealDB UPDATE queries that return the modified records.
    We count them to report how many memories were affected per tier.
  - The decay constants come from qmemory/constants.py so they can be
    tuned without touching this code.
  - Accepts an optional `db` connection so tests can inject the fixture.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from qmemory.constants import (
    SALIENCE_CEMENTED_THRESHOLD,
    SALIENCE_DECAY_CEMENTED_FLOOR,
    SALIENCE_DECAY_NEVER_RECALLED,
    SALIENCE_DECAY_STALE,
)
from qmemory.db.client import get_db, query

# Logger for this module — structured logging with timing
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main export: run_salience_decay
# ---------------------------------------------------------------------------


async def run_salience_decay(db: Any = None) -> dict:
    """
    Run the 3-tier salience decay cycle across all active memories.

    This processes all memories in the database in 3 UPDATE queries:
      - Tier 1: Never-recalled memories older than 7 days (fastest decay)
      - Tier 2: Recalled-but-stale memories (gentle decay)
      - Tier 3: Cemented memories (no decay, just enforce floor)

    Args:
        db: Optional SurrealDB connection. If None, creates a fresh one
            via get_db(). Pass the test fixture here.

    Returns:
        dict with:
          - "tier1_decayed": count of never-recalled memories that decayed
          - "tier2_decayed": count of stale memories that decayed
          - "tier3_enforced": count of cemented memories with floor enforced
          - "elapsed_ms": how long the entire decay cycle took
    """
    # Start timing — we report elapsed_ms for observability
    start = time.monotonic()

    logger.info("Starting salience decay cycle")

    # Run all 3 tiers using the provided connection or a fresh one
    if db is not None:
        result = await _run_all_tiers(db)
    else:
        async with get_db() as conn:
            result = await _run_all_tiers(conn)

    # Calculate elapsed time in milliseconds
    elapsed_ms = round((time.monotonic() - start) * 1000, 1)
    result["elapsed_ms"] = elapsed_ms

    logger.info(
        "Salience decay complete: tier1=%d, tier2=%d, tier3=%d, elapsed=%.1fms",
        result["tier1_decayed"],
        result["tier2_decayed"],
        result["tier3_enforced"],
        elapsed_ms,
    )

    return result


async def _run_all_tiers(db: Any) -> dict:
    """
    Execute all 3 decay tier queries against the database.

    Each UPDATE returns the list of records it modified.
    We count them to build the result dict.
    """

    # --- Tier 1: Never recalled + older than 7 days ---
    # These are facts the agent was told but never used.
    # They fade the fastest: 10% loss per cycle (multiply by 0.90).
    # Floor of 0.1 prevents them from hitting zero.
    tier1_result = await query(
        db,
        "UPDATE memory SET salience = math::max([salience * $decay_rate, 0.1]), "
        "updated_at = time::now() "
        "WHERE is_active = true AND recall_count = 0 "
        "AND created_at < time::now() - 7d",
        {"decay_rate": SALIENCE_DECAY_NEVER_RECALLED},
    )
    tier1_count = len(tier1_result) if isinstance(tier1_result, list) else 0

    logger.debug("Tier 1 (never recalled): %d memories decayed", tier1_count)

    # --- Tier 2: Recalled but stale (last_recalled > 14 days ago) ---
    # These were useful but haven't been accessed recently.
    # Gentle 2% decay per cycle (multiply by 0.98).
    # Only applies to memories with recall_count between 1 and 4
    # (cemented memories with 5+ recalls are handled by Tier 3).
    tier2_result = await query(
        db,
        "UPDATE memory SET salience = math::max([salience * $decay_rate, 0.1]), "
        "updated_at = time::now() "
        "WHERE is_active = true AND recall_count > 0 "
        "AND recall_count < $cemented_threshold "
        "AND last_recalled < time::now() - 14d",
        {
            "decay_rate": SALIENCE_DECAY_STALE,
            "cemented_threshold": SALIENCE_CEMENTED_THRESHOLD,
        },
    )
    tier2_count = len(tier2_result) if isinstance(tier2_result, list) else 0

    logger.debug("Tier 2 (recalled but stale): %d memories decayed", tier2_count)

    # --- Tier 3: Cemented (recall_count >= 5) ---
    # These are core knowledge — the agent relies on them frequently.
    # No decay at all, but we enforce a floor of 0.5 salience.
    # This only triggers if salience was somehow set below the floor
    # (e.g. by a manual update or a bug).
    tier3_result = await query(
        db,
        "UPDATE memory SET salience = math::max([salience, $floor]), "
        "updated_at = time::now() "
        "WHERE is_active = true AND recall_count >= $cemented_threshold "
        "AND salience < $floor",
        {
            "floor": SALIENCE_DECAY_CEMENTED_FLOOR,
            "cemented_threshold": SALIENCE_CEMENTED_THRESHOLD,
        },
    )
    tier3_count = len(tier3_result) if isinstance(tier3_result, list) else 0

    logger.debug("Tier 3 (cemented floor enforced): %d memories affected", tier3_count)

    return {
        "tier1_decayed": tier1_count,
        "tier2_decayed": tier2_count,
        "tier3_enforced": tier3_count,
    }


# ---------------------------------------------------------------------------
# Main export: apply_recall_boost
# ---------------------------------------------------------------------------


async def apply_recall_boost(memory_id: str, db: Any = None) -> None:
    """
    Boost a memory's salience when it is recalled.

    Called every time a memory appears in a recall/search result that the
    agent actually uses. This reinforces important memories over time:

      - recall_count += 1  (tracks how many times this memory was used)
      - last_recalled = now (tracks when it was last accessed)
      - salience += 0.05   (small boost, capped at 1.0)

    Once recall_count reaches the cemented threshold (5), the memory becomes
    "cemented" and is protected from decay (Tier 3 in run_salience_decay).

    Args:
        memory_id: The ID suffix of the memory (e.g. "mem1710864000000abc").
                   Do NOT include the "memory:" table prefix.
        db:        Optional SurrealDB connection. If None, creates a fresh one
                   via get_db(). Pass the test fixture here.
    """
    start = time.monotonic()

    # The UPDATE query does 3 things atomically:
    #   1. Increments recall_count by 1
    #   2. Sets last_recalled to the current time
    #   3. Adds 0.05 to salience, but caps it at 1.0 using math::min()
    surql = (
        "UPDATE type::record('memory', $id) SET "
        "recall_count += 1, "
        "last_recalled = time::now(), "
        "salience = math::min([salience + 0.05, 1.0])"
    )
    params = {"id": memory_id}

    if db is not None:
        await query(db, surql, params)
    else:
        async with get_db() as conn:
            await query(conn, surql, params)

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    logger.info(
        "Recall boost applied to memory:%s (elapsed=%.1fms)",
        memory_id,
        elapsed_ms,
    )
