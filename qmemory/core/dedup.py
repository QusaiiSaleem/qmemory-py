"""
Deduplication Pipeline

Checks if a new memory's content is already known — preventing the graph from
filling up with duplicate facts.

Three possible decisions:
  ADD    — genuinely new information, create a new memory node
  UPDATE — the new fact updates/corrects an existing one (return which one)
  NOOP   — the exact same fact is already in the graph (skip saving)

Two-tier approach:
  1. Fast path  — if no existing memories in this scope+category, return ADD
                  immediately. No LLM call needed (saves cost).
  2. LLM path   — ask Claude Haiku to compare the new fact against existing
                  ones and decide. Haiku is cheap, fast, and good enough for
                  dedup decisions.
  3. Rule-based fallback — if the LLM call fails (no API key, network error,
                  rate limit), fall back to simple string-overlap logic.
                  This is important for offline tests and development.

Rule-based fallback logic:
  - Exact content match (after stripping whitespace) → NOOP
  - Word overlap > 80% → UPDATE (return the most-overlapping memory ID)
  - Otherwise → ADD

Design decisions:
  - We limit existing memory lookup to 20 records in the same scope+category.
    Comparing against 1000 memories in one LLM call would produce poor results
    and waste tokens. If the scope+category has >20 memories, we compare against
    the 20 most recent ones (highest salience, latest creation time).
  - The LLM receives IDs in the prompt so it can reference them in the response.
  - We accept both "UPDATE" without an update_id (falls back to ADD) because
    Claude sometimes returns UPDATE without specifying which memory to replace.
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import get_db, query
from qmemory.llm import get_llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


async def dedup(
    content: str,
    category: str,
    scope: str = "global",
    db: Any = None,
) -> dict:
    """
    Check if a similar memory already exists in the same scope+category.

    Args:
        content:  The new fact content to check for duplicates.
        category: Memory category (e.g. "context", "preference").
                  Used to narrow the comparison — we only compare within
                  the same category (a "preference" fact can't duplicate
                  a "context" fact even if the text is similar).
        scope:    Visibility scope (e.g. "global", "project:alpha").
                  We only compare against memories in the same scope.
        db:       Optional SurrealDB connection. If None, creates a fresh one.
                  Pass the test fixture here during tests.

    Returns:
        dict with:
          - "decision":    "ADD", "UPDATE", or "NOOP"
          - "update_id":   The memory ID to soft-delete+replace (only for UPDATE)
                           None for ADD and NOOP.
          - "reason":      Short explanation of the decision (from LLM or fallback)
          - "related_ids": List of IDs that were considered (for debugging)
    """

    # --- Step 1: Fetch existing memories in the same scope+category ---
    # We limit to 20 to keep the LLM prompt manageable.
    # Sorted by salience DESC so the most important memories come first.
    existing = await _fetch_existing(content, category, scope, db)

    # --- Fast path: no existing memories → ADD immediately ---
    # No LLM call needed. This is the common case when first populating the graph.
    if not existing:
        logger.debug("dedup: no existing memories in %s/%s → ADD", scope, category)
        return {
            "decision": "ADD",
            "update_id": None,
            "reason": "No existing memories in this scope+category.",
            "related_ids": [],
        }

    # --- Step 2: Try the LLM path ---
    # Ask Claude Haiku to compare the new fact against existing ones.
    # If the LLM fails (returns empty dict), fall back to rule-based logic.
    logger.debug("dedup: comparing against %d existing memories via LLM", len(existing))

    llm_result = await _llm_dedup(content, existing)

    if llm_result:
        # LLM returned a valid decision
        decision = llm_result.get("decision", "ADD")
        update_id = llm_result.get("update_id")  # May be None or empty string
        reason = llm_result.get("reason", "LLM decision.")

        # Validate: if UPDATE but no update_id provided, treat as ADD
        # (LLM sometimes says UPDATE without specifying which memory)
        if decision == "UPDATE" and not update_id:
            logger.debug("dedup: LLM said UPDATE but gave no update_id → ADD")
            decision = "ADD"
            reason = f"LLM suggested update but didn't specify which memory. Original: {reason}"

        return {
            "decision": decision,
            "update_id": update_id or None,
            "reason": reason,
            "related_ids": [m["id"] for m in existing],
        }

    # --- Step 3: Rule-based fallback ---
    # LLM returned empty dict (API failure or no key).
    # Use simple string overlap to decide.
    logger.debug("dedup: LLM unavailable, using rule-based fallback")
    return _rule_based_dedup(content, existing)


# ---------------------------------------------------------------------------
# Internal: fetch existing memories
# ---------------------------------------------------------------------------


async def _fetch_existing(
    content: str,
    category: str,
    scope: str,
    db: Any,
) -> list[dict]:
    """
    Query SurrealDB for active memories in the same scope+category.

    Returns up to 20 records, sorted by salience DESC (most important first).
    If the DB is unavailable, returns an empty list (graceful degradation).
    """
    surql = """
        SELECT id, content, salience
        FROM memory
        WHERE is_active = true
          AND category = $category
          AND scope = $scope
        ORDER BY salience DESC
        LIMIT 20
    """
    params = {"category": category, "scope": scope}

    # Use the provided db connection or open a fresh one
    if db is not None:
        result = await query(db, surql, params)
    else:
        async with get_db() as conn:
            result = await query(conn, surql, params)

    # Graceful degradation: if query returned None (DB down), treat as empty
    return result or []


# ---------------------------------------------------------------------------
# Internal: LLM-based dedup
# ---------------------------------------------------------------------------


async def _llm_dedup(content: str, existing: list[dict]) -> dict:
    """
    Ask Claude Haiku to decide if the new fact is ADD, UPDATE, or NOOP.

    Returns the parsed structured response, or {} if the LLM call fails.
    """

    # Format existing memories as a numbered list with IDs.
    # IDs are included so the LLM can reference them in the update_id field.
    formatted_existing = "\n".join(
        f"[{i + 1}] ID={m['id']} | {m['content']}"
        for i, m in enumerate(existing)
    )

    prompt = f"""You are a memory deduplication system. Given a NEW fact and EXISTING facts, decide:
- ADD: The new fact is genuinely new information not covered by any existing fact
- UPDATE: The new fact updates, corrects, or supersedes an existing fact (specify which one)
- NOOP: The new fact is already captured by an existing fact (same meaning, no new info)

NEW FACT: {content}

EXISTING FACTS:
{formatted_existing}

Consider facts as duplicates if they convey the same core information, even if worded differently.
Consider an UPDATE if the new fact changes or adds precision to an existing fact."""

    # JSON Schema for structured output.
    # Claude MUST return a dict matching this schema (via tool_use).
    schema = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["ADD", "UPDATE", "NOOP"],
                "description": "Whether to add, update, or skip this fact",
            },
            "update_id": {
                "type": "string",
                "description": "Full ID of the memory to replace (only required if decision is UPDATE)",
            },
            "reason": {
                "type": "string",
                "description": "Brief explanation of the decision (1-2 sentences)",
            },
        },
        "required": ["decision", "reason"],
    }

    try:
        llm = get_llm("haiku")
        result = await llm.complete(prompt, schema=schema)

        # complete() returns {} on failure — check for valid dict with decision key
        if isinstance(result, dict) and "decision" in result:
            return result

        logger.debug("dedup: LLM returned invalid structure: %s", result)
        return {}

    except Exception as e:
        logger.warning("dedup: LLM call raised exception: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Internal: rule-based fallback
# ---------------------------------------------------------------------------


def _rule_based_dedup(content: str, existing: list[dict]) -> dict:
    """
    Simple string-overlap fallback when the LLM is unavailable.

    Rules (in priority order):
      1. Exact match (normalized) → NOOP
      2. Word overlap > 80% → UPDATE (most-overlapping memory)
      3. Otherwise → ADD

    Word overlap = |intersection| / |union| (Jaccard similarity on word sets).
    This is intentionally simple — the LLM path handles nuanced cases.
    """
    # Normalize the new content for comparison
    new_normalized = content.strip().lower()
    new_words = set(new_normalized.split())

    best_match_id: str | None = None
    best_overlap: float = 0.0

    for mem in existing:
        existing_content = mem.get("content", "")
        existing_normalized = existing_content.strip().lower()
        existing_words = set(existing_normalized.split())

        # Rule 1: Exact match → NOOP immediately
        if new_normalized == existing_normalized:
            logger.debug("dedup fallback: exact match with %s → NOOP", mem["id"])
            return {
                "decision": "NOOP",
                "update_id": None,
                "reason": f"Exact match with existing memory {mem['id']}.",
                "related_ids": [m["id"] for m in existing],
            }

        # Calculate Jaccard similarity (word overlap)
        # Jaccard = |A ∩ B| / |A ∪ B|
        if new_words or existing_words:
            intersection = len(new_words & existing_words)
            union = len(new_words | existing_words)
            overlap = intersection / union if union > 0 else 0.0

            # Track the best (most similar) match for a potential UPDATE
            if overlap > best_overlap:
                best_overlap = overlap
                best_match_id = mem["id"]

    # Rule 2: High overlap → UPDATE
    # 80% Jaccard means the texts are very similar — likely an update
    if best_overlap > 0.8 and best_match_id:
        logger.debug(
            "dedup fallback: %.0f%% word overlap with %s → UPDATE",
            best_overlap * 100,
            best_match_id,
        )
        return {
            "decision": "UPDATE",
            "update_id": best_match_id,
            "reason": f"High word overlap ({best_overlap:.0%}) with existing memory {best_match_id}.",
            "related_ids": [m["id"] for m in existing],
        }

    # Rule 3: Low overlap → ADD
    logger.debug("dedup fallback: best overlap %.0f%% → ADD", best_overlap * 100)
    return {
        "decision": "ADD",
        "update_id": None,
        "reason": "No sufficiently similar existing memory found (rule-based).",
        "related_ids": [m["id"] for m in existing],
    }
