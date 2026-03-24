"""
Core Save Memory

The fundamental "write" operation for Qmemory — creates a new memory node
in SurrealDB with an optional vector embedding.

Flow:
  1. Validate the category is one of the 8 allowed types
  2. Run deduplication — check if a similar memory already exists
       ADD  → proceed to create a new memory (steps 3-7)
       NOOP → return early, fact already known
       UPDATE → soft-delete the old memory, then create the new one (version chain)
  3. Generate a vector embedding for semantic search (async, non-fatal)
  4. Generate a unique timestamp-based ID
  5. Build a SurrealQL CREATE query with parameterized values
  6. Handle optional fields carefully (SurrealDB 3.0 rejects NULL for option<> fields)
  7. Execute the query
  8. Return result with a _nudge suggesting the agent link the new memory

Design decisions:
  - Accepts an optional `db` connection parameter. If provided (e.g. in tests),
    uses it directly. If not, creates a fresh connection via get_db().
  - Optional fields (source_person, context_mood, valid_from, valid_until,
    embedding) are OMITTED from the query when None — SurrealDB 3.0's
    option<> fields reject NULL values passed from the SDK.
  - source_person expects an entity ID string (e.g. "ent1710864000abc"),
    which gets wrapped in type::record('entity', ...) in the query.
  - Embedding generation is non-fatal: if Voyage API is down or no key is
    configured, the memory saves without a vector.
  - Dedup is non-fatal: if the LLM is unavailable, the rule-based fallback
    runs instead. If both fail, we default to ADD (never drop a memory silently).
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.constants import MEMORY_CATEGORIES
from qmemory.core.dedup import dedup
from qmemory.core.embeddings import generate_embedding
from qmemory.db.client import generate_id, get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


async def save_memory(
    content: str,
    category: str,
    salience: float = 0.5,
    scope: str = "global",
    confidence: float = 0.8,
    source_person: str | None = None,
    evidence_type: str = "observed",
    context_mood: str | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
    source_type: str = "conversation",
    owner_id: str | None = None,
    db: Any = None,
) -> dict:
    """
    Save a fact to cross-session memory.

    Creates a new memory node in SurrealDB with all required fields and
    any optional fields that have values. Generates a vector embedding
    for semantic search (non-fatal if it fails).

    Args:
        content:        The fact itself — one clear statement.
        category:       One of the 8 allowed categories (see MEMORY_CATEGORIES).
        salience:       Importance weight 0.0-1.0 (higher = recalled first).
        scope:          Visibility: "global", "project:xxx", "topic:xxx".
        confidence:     LLM confidence in this fact 0.0-1.0.
        source_person:  Entity ID of who said this (e.g. "ent1710864000abc").
        evidence_type:  How learned: "observed", "reported", "inferred", "self".
        context_mood:   Situational context: "calm_decision", "urgent", etc.
        valid_from:     ISO datetime string — when the fact became true.
        valid_until:    ISO datetime string — when the fact expired.
        source_type:    How sourced: "conversation", "workspace", "agent", etc.
        owner_id:       Optional user ID for multi-tenant cloud mode. When set,
                        tags the memory with owner = type::record('user', owner_id).
                        When None (local mode), no owner is set.
        db:             Optional SurrealDB connection. If None, creates a
                        fresh one via get_db(). Pass the test fixture here.

    Returns:
        dict with:
          - "action": "ADD" | "UPDATE" | "NOOP"
          - "memory_id": "memory:xxx" or None (None when action is NOOP)
          - "_nudge": suggestion for the agent to link the new memory

    Raises:
        ValueError: If category is not one of the 8 allowed categories.
    """

    # --- Step 1: Validate category ---
    # The agent might hallucinate a category name. Catch it early with
    # a clear error message so the agent can correct itself.
    if category not in MEMORY_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. "
            f"Must be one of: {', '.join(MEMORY_CATEGORIES)}"
        )

    logger.debug("Saving memory with owner=%s", owner_id)

    # --- Step 2: Run deduplication ---
    # Before creating a new memory, check if a similar one already exists.
    # This prevents the graph from filling up with duplicate facts.
    #
    # Three outcomes:
    #   ADD    → proceed normally (new fact, create it)
    #   NOOP   → return early (fact already known, don't save)
    #   UPDATE → soft-delete the old memory first, then create the new one
    #
    # Dedup is non-fatal: if the LLM is unavailable, the rule-based fallback
    # runs. If dedup itself crashes, we log and default to ADD.
    try:
        dedup_result = await dedup(content, category, scope, db=db)
        decision = dedup_result.get("decision", "ADD")
    except Exception as e:
        # If dedup crashes entirely (e.g. DB connection issue), default to ADD.
        # We never want to silently drop a memory due to a dedup bug.
        logger.warning("dedup failed, defaulting to ADD: %s", e)
        decision = "ADD"
        dedup_result = {"decision": "ADD", "update_id": None, "reason": "dedup error"}

    # Handle NOOP — the fact is already known, skip saving
    if decision == "NOOP":
        logger.info(
            "save_memory: NOOP (duplicate detected) — %s",
            dedup_result.get("reason", "")
        )
        return {
            "action": "NOOP",
            "memory_id": None,
            "_nudge": "Already known. No action taken.",
        }

    # Handle UPDATE — soft-delete the old memory before creating the new one.
    # This maintains a version chain (same pattern as correct_memory "correct" action).
    if decision == "UPDATE":
        old_id = dedup_result.get("update_id")
        if old_id:
            # Extract the ID suffix from "memory:mem..." format
            id_suffix = old_id.split(":", 1)[1] if ":" in old_id else old_id

            # Soft-delete the old memory (set is_active = false)
            # The record stays in SurrealDB for audit purposes
            soft_delete_surql = (
                "UPDATE type::record('memory', $id) "
                "SET is_active = false, updated_at = time::now()"
            )
            if db is not None:
                await query(db, soft_delete_surql, {"id": id_suffix})
            else:
                async with get_db() as conn:
                    await query(conn, soft_delete_surql, {"id": id_suffix})

            logger.info(
                "save_memory: UPDATE — soft-deleted old memory %s before saving new version",
                old_id,
            )

    # --- Step 3: Generate embedding (non-fatal) ---
    # If Voyage API is down or no key is set, embedding will be None
    # and the memory saves without a vector. Semantic search won't find
    # it, but BM25 full-text search still will.
    embedding = await generate_embedding(content)

    if embedding:
        logger.debug("Embedding generated (%d dimensions)", len(embedding))
    else:
        logger.debug("No embedding generated (Voyage unavailable or no API key)")

    # --- Step 4: Generate a unique ID ---
    # Format: mem{timestamp_ms}{random_chars} — e.g. "mem1710864000000abc"
    # No dashes, SurrealDB-safe, roughly time-ordered.
    memory_id_suffix = generate_id("mem")

    # --- Step 5: Build the SurrealQL CREATE query ---
    # We build the query dynamically because SurrealDB 3.0 rejects NULL
    # for option<> fields. If a value is None, we simply don't include
    # that field in the SET clause — SurrealDB will leave it as NONE.

    # Required fields — always included in the query
    base_query = """CREATE type::record('memory', $id) SET
    content = $content,
    category = $category,
    salience = $salience,
    scope = $scope,
    confidence = $confidence,
    evidence_type = $evidence_type,
    source_type = $source_type,
    recall_count = 0,
    linked = false,
    is_active = true,
    created_at = time::now(),
    updated_at = time::now()"""

    # Required parameters — always passed to the query
    params: dict[str, Any] = {
        "id": memory_id_suffix,
        "content": content,
        "category": category,
        "salience": salience,
        "scope": scope,
        "confidence": confidence,
        "evidence_type": evidence_type,
        "source_type": source_type,
    }

    # --- Step 6: Add optional fields (only if they have values) ---
    # Each optional field gets its own clause appended to the query.
    # This avoids sending NULL to SurrealDB's option<> fields.

    optional_parts: list[str] = []

    if owner_id:
        # owner is a record<user> FK — wrap in type::record()
        optional_parts.append("owner = type::record('user', $owner_id)")
        params["owner_id"] = owner_id

    if source_person:
        # source_person is a record<entity> FK — wrap in type::record()
        optional_parts.append("source_person = type::record('entity', $source_person)")
        params["source_person"] = source_person

    if context_mood:
        optional_parts.append("context_mood = $context_mood")
        params["context_mood"] = context_mood

    if valid_from:
        # valid_from is option<datetime> — pass as string, SurrealDB parses it
        optional_parts.append("valid_from = <datetime>$valid_from")
        params["valid_from"] = valid_from

    if valid_until:
        # valid_until is option<datetime> — same pattern
        optional_parts.append("valid_until = <datetime>$valid_until")
        params["valid_until"] = valid_until

    if embedding:
        # embedding is option<array<float>> — pass the raw list of floats
        optional_parts.append("embedding = $embedding")
        params["embedding"] = embedding

    # Join optional clauses into the query
    if optional_parts:
        optional_clause = ",\n    ".join(optional_parts)
        base_query += f",\n    {optional_clause}"

    # Close the query with a semicolon
    base_query += ";"

    # --- Step 7: Execute the query ---
    # If db was provided (e.g. test fixture), use it directly.
    # Otherwise, create a fresh connection for this operation.
    if db is not None:
        result = await query(db, base_query, params)
    else:
        async with get_db() as conn:
            result = await query(conn, base_query, params)

    # The full record ID as SurrealDB returns it (e.g. "memory:mem1710864000000abc")
    full_memory_id = f"memory:{memory_id_suffix}"

    # Log the result
    if result is not None:
        logger.info("Saved new memory %s (category=%s, salience=%.2f)", full_memory_id, category, salience)
    else:
        logger.warning("Failed to save memory — query returned None")

    # --- Step 8: Return result with nudge ---
    # The _nudge suggests the agent connect this memory to other nodes
    # in the graph. This is the "mind map" pattern — always suggest
    # a next action so the agent keeps building connections.
    nudge = (
        f"Memory saved as {full_memory_id}. "
        f"Connect it: qmemory_link(from_id='{full_memory_id}', "
        f"to_id='...', type='relates_to')"
    )

    # Use the actual decision ("ADD" or "UPDATE") as the action.
    # If dedup said UPDATE but there was no old ID to delete, we still
    # report ADD — the old memory was left active (no harm done).
    final_action = decision if decision in ("ADD", "UPDATE") else "ADD"

    return {
        "action": final_action,
        "memory_id": full_memory_id,
        "_nudge": nudge,
    }
