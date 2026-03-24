"""
Background Reflector -- finds patterns, contradictions, and insights in recent memories.

Runs as part of the background worker. Performs 5 cognitive jobs in ONE LLM call:

  1. patterns        — Recurring behaviors/rules across memories
                       → New memory (category: "context", source_type: "reflect")
  2. contradictions  — Flag conflicting memories
                       → `contradicts` edge between them
  3. compressions    — 3+ similar old memories merged into one principle
                       → New memory + soft-delete originals
  4. ghost_entities  — Names mentioned 3+ times but no entity node
                       → Create entity via person module
  5. self_learnings  — Meta-observations about agent performance
                       → New memory (category: "self", source_type: "reflect")

Algorithm:
  1. Query 30 most recent memories WHERE source_type != 'reflect' AND is_active = true
     (Feedback loop prevention: never reflect on reflections)
  2. If fewer than 3 → return {"found_work": false} (not enough to reflect on)
  3. Send to LLM with structured prompt → ask for JSON with 5 arrays
  4. Process each result:
     - patterns → save_memory(content=..., category="context", source_type="reflect")
     - contradictions → link_nodes(from_id, to_id, "contradicts")
     - compressions → save new, soft-delete originals via correct_memory(id, "delete")
     - ghost_entities → create_person(name=...)
     - self_learnings → save_memory(content=..., category="self", source_type="reflect")
  5. Return stats

Key safety features:
  - Feedback loop prevention: never includes source_type='reflect' memories
  - Validates all LLM-suggested memory IDs against the actual working set
  - Uses token budget — skips if budget exhausted
  - Uses cheap LLM (Z.AI) — not the user's Anthropic key
  - _parse_reflection() is a pure function (easy to test)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from qmemory.config import get_settings
from qmemory.core.correct import correct_memory
from qmemory.core.link import link_nodes
from qmemory.core.person import create_person
from qmemory.core.save import save_memory
from qmemory.core.token_budget import can_spend, record_spend
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many recent memories to fetch for reflection
MEMORY_FETCH_LIMIT = 30

# Minimum number of non-reflect memories needed to run a reflection cycle
MIN_MEMORIES_FOR_REFLECTION = 3

# Max characters of memory content to include in the prompt
# (keeps the prompt small and cheap)
MAX_CONTENT_LENGTH = 200

# The LLM model to use for reflection (cheap model via Z.AI)
REFLECTOR_MODEL = "claude-3-5-haiku-latest"

# The prompt template sent to the LLM
REFLECTOR_PROMPT = """You are a memory reflection agent. Analyze these memories and find patterns, contradictions, and insights.

MEMORIES:
{memory_list}

Analyze and return a JSON object with these 5 arrays (empty arrays if nothing found):

{{
  "patterns": [
    {{"content": "Recurring pattern description", "memory_ids": ["id1", "id2"]}}
  ],
  "contradictions": [
    {{"memory_a": "id1", "memory_b": "id2", "reason": "Why they contradict"}}
  ],
  "compressions": [
    {{"merged_content": "Single principle from multiple memories", "source_ids": ["id1", "id2", "id3"]}}
  ],
  "ghost_entities": [
    {{"name": "Person name mentioned 3+ times but no entity exists"}}
  ],
  "self_learnings": [
    {{"content": "Meta-observation about how the agent performs"}}
  ]
}}

Rules:
- Only use memory IDs from the list above
- Patterns need at least 2 supporting memories
- Compressions need at least 3 similar memories
- Ghost entities need 3+ mentions across different memories
- Be conservative — only flag clear patterns, not speculation
- Return ONLY the JSON object"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_reflector_cycle(db: Any = None) -> dict:
    """
    One reflector cycle. Analyzes recent memories for patterns, contradictions,
    compressions, ghost entities, and self-learnings.

    This is the main entry point called by the background worker.

    Args:
        db: Optional SurrealDB connection. If None, creates a fresh one.
            Pass the test fixture here for testing.

    Returns:
        dict with:
          - found_work (bool): True if there were enough memories to reflect on
          - patterns (int): How many pattern memories were created
          - contradictions (int): How many contradiction edges were created
          - compressions (int): How many compressions were applied
          - ghost_entities (int): How many ghost entities were created
          - self_learnings (int): How many self-learning memories were saved
          - elapsed_ms (float): How long the cycle took in milliseconds
    """
    start_time = time.time()

    # Dispatch: use provided DB connection or create a fresh one
    if db is not None:
        result = await _run_cycle(db)
    else:
        async with get_db() as conn:
            result = await _run_cycle(conn)

    # Add timing to the result
    elapsed_ms = (time.time() - start_time) * 1000
    result["elapsed_ms"] = round(elapsed_ms, 1)

    logger.info(
        "reflector_cycle: found_work=%s patterns=%d contradictions=%d "
        "compressions=%d ghost_entities=%d self_learnings=%d elapsed=%.1fms",
        result["found_work"],
        result.get("patterns", 0),
        result.get("contradictions", 0),
        result.get("compressions", 0),
        result.get("ghost_entities", 0),
        result.get("self_learnings", 0),
        result["elapsed_ms"],
    )

    return result


# ---------------------------------------------------------------------------
# Internal: the actual cycle logic
# ---------------------------------------------------------------------------


def _empty_stats() -> dict:
    """Return a result dict with all counters at zero and found_work=False."""
    return {
        "found_work": False,
        "patterns": 0,
        "contradictions": 0,
        "compressions": 0,
        "ghost_entities": 0,
        "self_learnings": 0,
    }


async def _run_cycle(db: Any) -> dict:
    """
    Internal cycle implementation. Runs with an active DB connection.

    Steps:
      1. Query recent non-reflect memories
      2. If fewer than 3 -> return early
      3. Check token budget
      4. Check for API key
      5. Build prompt and call LLM
      6. Parse JSON response
      7. Process each of the 5 result types
      8. Return stats
    """

    # --- Step 1: Query 30 most recent non-reflect, active memories ---
    # CRITICAL: We exclude source_type='reflect' to prevent feedback loops.
    # Without this filter, the reflector would reflect on its own reflections,
    # creating an infinite chain of increasingly abstract meta-observations.
    memories = await query(
        db,
        "SELECT id, content, category, salience, created_at FROM memory "
        "WHERE source_type != 'reflect' AND is_active = true "
        "ORDER BY created_at DESC "
        "LIMIT $limit",
        {"limit": MEMORY_FETCH_LIMIT},
    )

    # --- Step 2: If fewer than 3 memories, nothing meaningful to reflect on ---
    if not memories or len(memories) < MIN_MEMORIES_FOR_REFLECTION:
        logger.info(
            "reflector: only %d non-reflect memories found (need %d), skipping",
            len(memories) if memories else 0,
            MIN_MEMORIES_FOR_REFLECTION,
        )
        return _empty_stats()

    logger.info("reflector: found %d memories to reflect on", len(memories))

    # Build a set of valid memory IDs for validation later
    valid_ids = {str(m["id"]) for m in memories}

    # --- Step 3: Check token budget before calling LLM ---
    # Reflector is "low" priority — gets cut off first when budget is tight
    estimated_tokens = 3000  # rough estimate for a reflection prompt
    if not can_spend(estimated_tokens, priority="low"):
        logger.info("reflector: token budget exhausted, skipping LLM call")
        stats = _empty_stats()
        stats["found_work"] = True  # work exists, just can't afford it
        return stats

    # --- Step 4: Check for API key ---
    settings = get_settings()
    if not settings.zai_api_key:
        logger.warning("reflector: no ZAI_API_KEY configured, cannot call LLM")
        stats = _empty_stats()
        stats["found_work"] = True  # work exists, just no key
        return stats

    # --- Step 5: Build the prompt and call LLM ---
    prompt = _build_prompt(memories)

    try:
        response = await _call_llm(prompt, settings.zai_api_key)
    except Exception as e:
        logger.warning("reflector: LLM call failed: %s", e)
        stats = _empty_stats()
        stats["found_work"] = True
        return stats

    # Record the token spend (from the LLM response usage stats)
    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    record_spend(actual_tokens, source="reflector", priority="low")

    # --- Step 6: Parse JSON response ---
    raw_text = response.content[0].text.strip()
    reflection = _parse_reflection(raw_text)

    # --- Step 7: Process each of the 5 result types ---
    stats = _empty_stats()
    stats["found_work"] = True

    # 7a: Patterns → save as new memories with source_type="reflect"
    stats["patterns"] = await _process_patterns(reflection["patterns"], db)

    # 7b: Contradictions → create "contradicts" edges between memory pairs
    stats["contradictions"] = await _process_contradictions(
        reflection["contradictions"], valid_ids, db
    )

    # 7c: Compressions → save merged memory, soft-delete originals
    stats["compressions"] = await _process_compressions(
        reflection["compressions"], valid_ids, db
    )

    # 7d: Ghost entities → create person entities
    stats["ghost_entities"] = await _process_ghost_entities(
        reflection["ghost_entities"], db
    )

    # 7e: Self-learnings → save as new memories with category="self"
    stats["self_learnings"] = await _process_self_learnings(
        reflection["self_learnings"], db
    )

    return stats


# ---------------------------------------------------------------------------
# Pure helper: parse the LLM JSON response
# ---------------------------------------------------------------------------


def _parse_reflection(raw_text: str) -> dict:
    """
    Parse the raw JSON string from the LLM into a structured dict
    with the 5 reflection arrays.

    This is a pure function (no DB, no side effects) so it's easy to test.

    If the JSON is invalid or missing keys, returns a safe default structure
    with all 5 arrays empty. This prevents crashes from malformed LLM output.

    Args:
        raw_text: The raw text string from the LLM response.

    Returns:
        dict with 5 keys, each an array:
          - patterns, contradictions, compressions, ghost_entities, self_learnings
    """
    # The safe default — all 5 arrays empty
    empty = {
        "patterns": [],
        "contradictions": [],
        "compressions": [],
        "ghost_entities": [],
        "self_learnings": [],
    }

    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("reflector: LLM returned invalid JSON: %s", raw_text[:200])
        return empty

    # Ensure the parsed result is a dict (not a list or other type)
    if not isinstance(parsed, dict):
        logger.warning("reflector: LLM returned non-dict JSON: %s", type(parsed))
        return empty

    # Extract each key, defaulting to empty list if missing.
    # Also ensure each value is actually a list (not a string or dict).
    result = {}
    for key in empty:
        value = parsed.get(key, [])
        result[key] = value if isinstance(value, list) else []

    return result


# ---------------------------------------------------------------------------
# Helper: build the LLM prompt
# ---------------------------------------------------------------------------


def _build_prompt(memories: list[dict]) -> str:
    """
    Build the prompt string for the LLM, listing all memories.

    Each memory's content is truncated to MAX_CONTENT_LENGTH characters
    to keep the prompt small and cheap.

    Args:
        memories: List of memory dicts (id, content, category, salience).

    Returns:
        The formatted prompt string ready to send to the LLM.
    """
    memory_lines = []
    for m in memories:
        # Truncate content to save tokens
        content = (m.get("content") or "")[:MAX_CONTENT_LENGTH]
        category = m.get("category", "unknown")
        salience = m.get("salience", 0.5)
        memory_lines.append(
            f"- ID: {m['id']} | {content} [{category}, salience={salience}]"
        )

    return REFLECTOR_PROMPT.format(
        memory_list="\n".join(memory_lines),
    )


# ---------------------------------------------------------------------------
# Helper: call the LLM via Z.AI (Anthropic-compatible API)
# ---------------------------------------------------------------------------


async def _call_llm(prompt: str, api_key: str) -> Any:
    """
    Call the Z.AI LLM with the reflection prompt.

    Uses the Anthropic SDK pointed at Z.AI's API endpoint.
    This is a synchronous SDK call wrapped for async (the Anthropic
    Python SDK's messages.create is synchronous).

    Args:
        prompt:  The full prompt string to send.
        api_key: The Z.AI API key.

    Returns:
        The Anthropic Message response object.
    """
    import asyncio

    from anthropic import Anthropic

    # Create a client pointed at Z.AI's Anthropic-compatible endpoint
    client = Anthropic(
        base_url="https://api.z.ai/api/anthropic",
        api_key=api_key,
    )

    # The Anthropic SDK's messages.create is synchronous, so we
    # run it in a thread to avoid blocking the event loop
    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(
        None,
        lambda: client.messages.create(
            model=REFLECTOR_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        ),
    )

    return response


# ---------------------------------------------------------------------------
# Processors: handle each of the 5 reflection result types
# ---------------------------------------------------------------------------


async def _process_patterns(patterns: list[dict], db: Any) -> int:
    """
    Save each pattern as a new memory with category="context" and
    source_type="reflect".

    Args:
        patterns: List of pattern dicts from the LLM, each with
                  "content" and "memory_ids" keys.
        db:       Active SurrealDB connection.

    Returns:
        How many pattern memories were successfully created.
    """
    count = 0
    for pattern in patterns:
        content = pattern.get("content")
        if not content:
            continue

        try:
            await save_memory(
                content=content,
                category="context",
                source_type="reflect",
                salience=0.6,  # moderate salience — patterns are useful but not urgent
                db=db,
            )
            count += 1
            logger.info("reflector: saved pattern: %s", content[:100])
        except Exception as e:
            logger.warning("reflector: failed to save pattern: %s", e)

    return count


async def _process_contradictions(
    contradictions: list[dict],
    valid_ids: set[str],
    db: Any,
) -> int:
    """
    Create "contradicts" edges between pairs of contradicting memories.

    Validates both memory IDs against the working set before creating
    the edge (hallucination defense).

    Args:
        contradictions: List of contradiction dicts from the LLM, each with
                        "memory_a", "memory_b", and "reason" keys.
        valid_ids:      Set of valid memory ID strings from the working set.
        db:             Active SurrealDB connection.

    Returns:
        How many contradiction edges were successfully created.
    """
    count = 0
    for contradiction in contradictions:
        memory_a = contradiction.get("memory_a", "")
        memory_b = contradiction.get("memory_b", "")
        reason = contradiction.get("reason", "Contradicting memories")

        # Validate both IDs exist in our working set (hallucination defense)
        if memory_a not in valid_ids or memory_b not in valid_ids:
            logger.debug(
                "reflector: rejected contradiction %s <-> %s (ID not in working set)",
                memory_a,
                memory_b,
            )
            continue

        try:
            result = await link_nodes(
                from_id=memory_a,
                to_id=memory_b,
                relationship_type="contradicts",
                reason=reason,
                confidence=0.7,
                db=db,
            )
            if result is not None:
                count += 1
                logger.info(
                    "reflector: created contradiction edge %s <-> %s",
                    memory_a,
                    memory_b,
                )
        except Exception as e:
            logger.warning("reflector: failed to create contradiction edge: %s", e)

    return count


async def _process_compressions(
    compressions: list[dict],
    valid_ids: set[str],
    db: Any,
) -> int:
    """
    For each compression: save the merged memory FIRST, then soft-delete
    the originals.

    Order matters: if the save fails, we don't want to delete the originals.
    If a delete fails, the merged memory still exists — no data loss.

    Validates all source IDs against the working set before processing
    (hallucination defense).

    Args:
        compressions: List of compression dicts from the LLM, each with
                      "merged_content" and "source_ids" keys.
        valid_ids:    Set of valid memory ID strings from the working set.
        db:           Active SurrealDB connection.

    Returns:
        How many compressions were successfully applied.
    """
    count = 0
    for compression in compressions:
        merged_content = compression.get("merged_content")
        source_ids = compression.get("source_ids", [])

        if not merged_content or len(source_ids) < 3:
            continue

        # Validate ALL source IDs exist in our working set
        if not all(sid in valid_ids for sid in source_ids):
            logger.debug(
                "reflector: rejected compression — some source IDs not in working set: %s",
                source_ids,
            )
            continue

        try:
            # Step 1: Save the merged memory FIRST
            save_result = await save_memory(
                content=merged_content,
                category="context",
                source_type="reflect",
                salience=0.7,  # slightly higher — compressed = more refined
                db=db,
            )

            if save_result.get("action") == "NOOP":
                # Dedup caught it — the merged content already exists
                logger.info("reflector: compression skipped (already exists)")
                continue

            # Step 2: Soft-delete the originals
            deleted_count = 0
            for source_id in source_ids:
                try:
                    await correct_memory(
                        memory_id=source_id,
                        action="delete",
                        reason="Compressed by reflector into merged memory",
                        db=db,
                    )
                    deleted_count += 1
                except Exception as e:
                    logger.warning(
                        "reflector: failed to delete original %s: %s", source_id, e
                    )

            count += 1
            logger.info(
                "reflector: compressed %d memories into one (deleted %d originals)",
                len(source_ids),
                deleted_count,
            )
        except Exception as e:
            logger.warning("reflector: failed to apply compression: %s", e)

    return count


async def _process_ghost_entities(ghost_entities: list[dict], db: Any) -> int:
    """
    Create person entities for names mentioned 3+ times across memories
    but without an existing entity node.

    Args:
        ghost_entities: List of ghost entity dicts from the LLM, each with
                        a "name" key.
        db:             Active SurrealDB connection.

    Returns:
        How many ghost entities were successfully created.
    """
    count = 0
    for ghost in ghost_entities:
        name = ghost.get("name")
        if not name:
            continue

        try:
            result = await create_person(name=name, db=db)
            # Only count as new if the person was actually created (not found)
            if result.get("action") == "created":
                count += 1
                logger.info("reflector: created ghost entity: %s", name)
            else:
                logger.info(
                    "reflector: ghost entity '%s' already exists, skipped", name
                )
        except Exception as e:
            logger.warning("reflector: failed to create ghost entity '%s': %s", name, e)

    return count


async def _process_self_learnings(self_learnings: list[dict], db: Any) -> int:
    """
    Save each self-learning as a new memory with category="self" and
    source_type="reflect".

    Args:
        self_learnings: List of self-learning dicts from the LLM, each with
                        a "content" key.
        db:             Active SurrealDB connection.

    Returns:
        How many self-learning memories were successfully created.
    """
    count = 0
    for learning in self_learnings:
        content = learning.get("content")
        if not content:
            continue

        try:
            await save_memory(
                content=content,
                category="self",
                source_type="reflect",
                salience=0.5,  # moderate salience — self-observations are useful context
                db=db,
            )
            count += 1
            logger.info("reflector: saved self-learning: %s", content[:100])
        except Exception as e:
            logger.warning("reflector: failed to save self-learning: %s", e)

    return count
