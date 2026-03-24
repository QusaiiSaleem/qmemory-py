"""
Background Linker -- finds relationships between unlinked memories via LLM.

Runs as part of the background worker. Queries memories where linked=false,
asks a cheap LLM (Z.AI) to find relationships, creates edges.

Algorithm:
  1. Query up to 10 memories where linked=false and is_active=true
  2. If none found -> return {"found_work": false} (worker backs off to 30 min)
  3. Fetch 20 most recent OTHER memories as comparison candidates
  4. Build a prompt with both lists -> send to LLM (Z.AI, cheap)
  5. Parse JSON response -> list of {from_id, to_id, type, reason}
  6. Validate every ID against the working set (hallucination defense!)
  7. Create edges via existing link_nodes() function with created_by: "linker"
  8. Mark ALL processed memories as linked=true (even if no edges created)
  9. Return {"found_work": true, "processed": N, "edges_created": N}

Key safety features:
  - Validates all LLM-suggested IDs against the actual working set
  - Uses token budget -- skips if budget exhausted
  - Marks memories as linked=true even if no edges found (prevents re-checking)
  - Uses cheap LLM (Z.AI) -- not the user's Anthropic key
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from qmemory.config import get_settings
from qmemory.core.link import link_nodes
from qmemory.core.token_budget import can_spend, record_spend
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many unlinked memories to process per cycle
BATCH_SIZE = 10

# How many recent memories to use as comparison candidates
CANDIDATE_COUNT = 20

# Max characters of memory content to include in the prompt
# (keeps the prompt small and cheap)
MAX_CONTENT_LENGTH = 200

# The LLM model to use for linking (cheap model via Z.AI)
LINKER_MODEL = "claude-3-5-haiku-latest"

# The prompt template sent to the LLM
LINKER_PROMPT = """You are a knowledge graph linker. Given two sets of memories, find meaningful relationships between them.

UNLINKED MEMORIES (find relationships FOR these):
{unlinked_list}

CANDIDATE MEMORIES (link TO these):
{candidate_list}

For each relationship you find, return a JSON array:
[
  {{"from_id": "memory:xxx", "to_id": "memory:yyy", "type": "supports|contradicts|elaborates|depends_on|caused_by|related_to", "reason": "brief explanation"}}
]

Rules:
- Only use IDs from the lists above (NEVER invent IDs)
- Only create relationships that are clearly meaningful
- Prefer specific types (supports, contradicts) over generic (related_to)
- Return [] if no clear relationships exist
- Return ONLY the JSON array, nothing else"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_linker_cycle(db: Any = None) -> dict:
    """
    One linker cycle. Finds unlinked memories, asks LLM for relationships,
    creates edges.

    This is the main entry point called by the background worker.

    Args:
        db: Optional SurrealDB connection. If None, creates a fresh one.
            Pass the test fixture here for testing.

    Returns:
        dict with:
          - found_work (bool): True if there were unlinked memories to process
          - processed (int): How many memories were processed
          - edges_created (int): How many new edges were created
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
        "linker_cycle: found_work=%s processed=%d edges=%d elapsed=%.1fms",
        result["found_work"],
        result.get("processed", 0),
        result.get("edges_created", 0),
        result["elapsed_ms"],
    )

    return result


# ---------------------------------------------------------------------------
# Internal: the actual cycle logic
# ---------------------------------------------------------------------------


async def _run_cycle(db: Any) -> dict:
    """
    Internal cycle implementation. Runs with an active DB connection.

    Steps:
      1. Query unlinked memories
      2. If none -> return early
      3. Fetch candidate memories for comparison
      4. Check token budget
      5. Call LLM
      6. Parse + validate response
      7. Create edges
      8. Mark all processed memories as linked=true
    """

    # --- Step 1: Query up to 10 unlinked, active memories ---
    unlinked = await query(
        db,
        "SELECT id, content, category FROM memory "
        "WHERE linked = false AND is_active = true "
        "LIMIT $limit",
        {"limit": BATCH_SIZE},
    )

    # --- Step 2: If no unlinked memories, nothing to do ---
    if not unlinked:
        logger.info("linker: no unlinked memories found, backing off")
        return {"found_work": False, "processed": 0, "edges_created": 0}

    logger.info("linker: found %d unlinked memories to process", len(unlinked))

    # Collect the IDs of the unlinked memories (for exclusion + validation)
    unlinked_ids = {m["id"] for m in unlinked}

    # --- Step 3: Fetch 20 most recent OTHER memories as comparison candidates ---
    candidates = await query(
        db,
        "SELECT id, content, category FROM memory "
        "WHERE is_active = true "
        "ORDER BY created_at DESC "
        "LIMIT $limit",
        {"limit": CANDIDATE_COUNT + BATCH_SIZE},  # fetch extra, then filter
    )

    # Filter out the unlinked memories from candidates (no self-linking)
    if candidates:
        candidates = [c for c in candidates if c["id"] not in unlinked_ids]
        candidates = candidates[:CANDIDATE_COUNT]  # trim to 20
    else:
        candidates = []

    # If no candidates to compare against, still mark as linked and return
    if not candidates:
        logger.info("linker: no candidate memories to compare against")
        await _mark_linked(db, unlinked_ids)
        return {"found_work": True, "processed": len(unlinked), "edges_created": 0}

    # --- Step 4: Check token budget before calling LLM ---
    # Linker is "low" priority -- gets cut off first when budget is tight
    estimated_tokens = 2000  # rough estimate for a linking prompt
    if not can_spend(estimated_tokens, priority="low"):
        logger.info("linker: token budget exhausted, skipping LLM call")
        return {"found_work": True, "processed": 0, "edges_created": 0}

    # --- Step 5: Check for API key ---
    settings = get_settings()
    if not settings.zai_api_key:
        logger.warning("linker: no ZAI_API_KEY configured, cannot call LLM")
        return {"found_work": True, "processed": 0, "edges_created": 0}

    # --- Step 6: Build the prompt and call LLM ---
    prompt = _build_prompt(unlinked, candidates)

    try:
        response = await _call_llm(prompt, settings.zai_api_key)
    except Exception as e:
        logger.warning("linker: LLM call failed: %s", e)
        # Mark as linked anyway so we don't retry the same batch forever
        await _mark_linked(db, unlinked_ids)
        return {"found_work": True, "processed": len(unlinked), "edges_created": 0}

    # Record the token spend (from the LLM response usage stats)
    actual_tokens = response.usage.input_tokens + response.usage.output_tokens
    record_spend(actual_tokens, source="linker", priority="low")

    # --- Step 7: Parse JSON response ---
    raw_text = response.content[0].text.strip()
    try:
        raw_edges = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("linker: LLM returned invalid JSON: %s", raw_text[:200])
        raw_edges = []

    # Ensure we got a list
    if not isinstance(raw_edges, list):
        logger.warning("linker: LLM returned non-list: %s", type(raw_edges))
        raw_edges = []

    # --- Step 8: Validate IDs against the working set (hallucination defense!) ---
    # Build the set of ALL valid IDs (unlinked + candidates)
    all_valid_ids = unlinked_ids | {c["id"] for c in candidates}
    validated_edges = _validate_edges(raw_edges, all_valid_ids)

    logger.info(
        "linker: LLM suggested %d edges, %d passed validation",
        len(raw_edges),
        len(validated_edges),
    )

    # --- Step 9: Create edges via link_nodes() ---
    edges_created = 0
    for edge in validated_edges:
        try:
            result = await link_nodes(
                from_id=edge["from_id"],
                to_id=edge["to_id"],
                relationship_type=edge["type"],
                reason=edge.get("reason"),
                confidence=0.6,  # lower confidence since it's auto-detected
                db=db,
            )
            if result is not None:
                edges_created += 1
                logger.info(
                    "linker: created edge %s -[%s]-> %s",
                    edge["from_id"],
                    edge["type"],
                    edge["to_id"],
                )
        except Exception as e:
            logger.warning("linker: failed to create edge: %s", e)

    # --- Step 10: Mark ALL processed memories as linked=true ---
    # This happens even if no edges were created -- prevents re-checking
    await _mark_linked(db, unlinked_ids)

    return {
        "found_work": True,
        "processed": len(unlinked),
        "edges_created": edges_created,
    }


# ---------------------------------------------------------------------------
# Pure helper: validate edges (hallucination defense)
# ---------------------------------------------------------------------------


def _validate_edges(
    raw_edges: list[dict],
    valid_ids: set[str],
) -> list[dict]:
    """
    Filter out edge suggestions where either ID is not in the valid set.

    This is the hallucination defense -- the LLM might invent IDs that
    look plausible but don't exist. We only keep edges where BOTH the
    from_id and to_id are in our actual working set.

    This is a pure function (no DB, no side effects) so it's easy to test.

    Args:
        raw_edges:  List of dicts from the LLM, each with from_id, to_id, type, reason.
        valid_ids:  Set of actual memory IDs that exist in our working set.

    Returns:
        Filtered list containing only edges with valid IDs.
    """
    validated = []

    for edge in raw_edges:
        from_id = edge.get("from_id", "")
        to_id = edge.get("to_id", "")

        # Both IDs must be in the valid set
        if from_id in valid_ids and to_id in valid_ids:
            validated.append(edge)
        else:
            logger.debug(
                "linker: rejected edge %s -> %s (ID not in working set)",
                from_id,
                to_id,
            )

    return validated


# ---------------------------------------------------------------------------
# Helper: build the LLM prompt
# ---------------------------------------------------------------------------


def _build_prompt(unlinked: list[dict], candidates: list[dict]) -> str:
    """
    Build the prompt string for the LLM, with both memory lists.

    Each memory's content is truncated to MAX_CONTENT_LENGTH characters
    to keep the prompt small and cheap.

    Args:
        unlinked:   List of unlinked memory dicts (id, content, category).
        candidates: List of candidate memory dicts (id, content, category).

    Returns:
        The formatted prompt string ready to send to the LLM.
    """
    # Format the unlinked memories list
    unlinked_lines = []
    for m in unlinked:
        content = (m.get("content") or "")[:MAX_CONTENT_LENGTH]
        category = m.get("category", "unknown")
        unlinked_lines.append(f"- ID: {m['id']} | {content} [{category}]")

    # Format the candidate memories list
    candidate_lines = []
    for m in candidates:
        content = (m.get("content") or "")[:MAX_CONTENT_LENGTH]
        category = m.get("category", "unknown")
        candidate_lines.append(f"- ID: {m['id']} | {content} [{category}]")

    return LINKER_PROMPT.format(
        unlinked_list="\n".join(unlinked_lines),
        candidate_list="\n".join(candidate_lines),
    )


# ---------------------------------------------------------------------------
# Helper: call the LLM via Z.AI (Anthropic-compatible API)
# ---------------------------------------------------------------------------


async def _call_llm(prompt: str, api_key: str) -> Any:
    """
    Call the Z.AI LLM with the linking prompt.

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
            model=LINKER_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        ),
    )

    return response


# ---------------------------------------------------------------------------
# Helper: mark memories as linked in the database
# ---------------------------------------------------------------------------


async def _mark_linked(db: Any, memory_ids: set[str]) -> None:
    """
    Set linked=true for all the given memory IDs.

    This is called after processing, even if no edges were created.
    It prevents the same memories from being re-checked in the next cycle.

    Args:
        db:         Active SurrealDB connection.
        memory_ids: Set of memory ID strings (e.g. {"memory:mem001", "memory:mem002"}).
    """
    for mem_id in memory_ids:
        # Parse the suffix from "memory:xxx" format
        table, suffix = mem_id.split(":", 1)
        await query(
            db,
            f"UPDATE {table}:`{suffix}` SET linked = true",
        )

    logger.info("linker: marked %d memories as linked=true", len(memory_ids))
