"""
Core Correct Memory

Modifies existing memory nodes in SurrealDB. Supports 4 actions:

  "correct" — Fix a memory's content. Soft-deletes the old record, creates
               a new one with the corrected content, and links them with a
               `prev_version` edge for a full audit trail. Use this when
               a fact is WRONG and needs to be replaced.

  "delete"  — Soft-delete only. Sets is_active = false. The record stays
               in SurrealDB forever for audit purposes. Use this when a fact
               is no longer relevant at all.

  "update"  — Mutate metadata fields WITHOUT creating a new version. Good
               for changing salience, scope, valid_until, or category.
               No version chain is created — this is a direct edit.

  "unlink"  — Remove a `relates` edge from the graph. Use this to fix bad
               connections the linker service made automatically.

Design decisions:
  - We NEVER hard-delete memories — soft-delete only (is_active = false).
  - The "correct" action uses a `prev_version` RELATE edge so the agent
    can always trace back through the history of a fact.
  - The "update" action is for lightweight metadata changes that don't
    warrant a full version bump (e.g. bumping salience, expiring a fact).
  - "unlink" hard-deletes the EDGE, not the node — the two memories
    remain, just without the connection between them.
  - All queries are parameterized — never string-interpolate into SurrealQL.
  - Accepts an optional `db` connection so tests can inject the fixture.
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import generate_id, get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


async def correct_memory(
    memory_id: str,
    action: str,
    new_content: str | None = None,   # Required if action="correct"
    updates: dict | None = None,       # For action="update" (metadata changes)
    edge_id: str | None = None,        # For action="unlink" (remove a relates edge)
    reason: str | None = None,         # Optional note explaining why
    db: Any = None,
) -> dict:
    """
    Correct, delete, update, or unlink a memory.

    Args:
        memory_id:    Full SurrealDB record ID, e.g. "memory:mem1710864000000abc".
        action:       One of "correct", "delete", "update", "unlink".
        new_content:  The corrected fact text. Required when action="correct".
        updates:      Dict of metadata fields to change. Required when action="update".
                      Allowed keys: salience, scope, valid_until, category, confidence.
        edge_id:      The `relates` edge ID to delete. Required when action="unlink".
        reason:       Optional explanation stored in the log (not in DB).
        db:           Optional SurrealDB connection. If None, creates a fresh one
                      via get_db(). Pass the test fixture here.

    Returns:
        dict with:
          - "action":        What was done ("corrected", "deleted", "updated", "unlinked")
          - "memory_id":     The affected memory's record ID
          - "new_memory_id": The newly created memory ID (only for action="correct")
          - "_nudge":        Suggested next action for the agent

    Raises:
        ValueError: If action is not one of the 4 allowed values, or if a
                    required argument is missing for the chosen action.
    """

    # --- Step 1: Validate the action ---
    # Catch bad action values early with a clear message — the agent might
    # hallucinate an action name like "fix" or "replace".
    valid_actions = {"correct", "delete", "update", "unlink"}
    if action not in valid_actions:
        raise ValueError(
            f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}"
        )

    # --- Step 2: Validate action-specific required arguments ---
    # Each action has its own required argument. Check before touching the DB.
    if action == "correct" and not new_content:
        raise ValueError("new_content is required when action is 'correct'")

    if action == "update" and not updates:
        raise ValueError("updates dict is required when action is 'update'")

    if action == "unlink" and not edge_id:
        raise ValueError("edge_id is required when action is 'unlink'")

    # --- Extract the ID suffix for SurrealDB queries ---
    # memory_id is "memory:mem1710864000000abc" — SurrealDB's type::record()
    # needs just the suffix part after the colon: "mem1710864000000abc"
    if ":" in memory_id:
        id_suffix = memory_id.split(":", 1)[1]
    else:
        id_suffix = memory_id

    # --- Dispatch to the right handler based on action ---
    if db is not None:
        # Test mode: use the provided connection directly
        return await _dispatch(action, id_suffix, memory_id, new_content, updates, edge_id, reason, db)
    else:
        # Production mode: create a fresh connection for this operation
        async with get_db() as conn:
            return await _dispatch(action, id_suffix, memory_id, new_content, updates, edge_id, reason, conn)


# ---------------------------------------------------------------------------
# Internal dispatcher
# ---------------------------------------------------------------------------


async def _dispatch(
    action: str,
    id_suffix: str,
    memory_id: str,
    new_content: str | None,
    updates: dict | None,
    edge_id: str | None,
    reason: str | None,
    db: Any,
) -> dict:
    """Route to the correct handler. Called with an active DB connection."""

    if action == "delete":
        return await _handle_delete(id_suffix, memory_id, reason, db)

    if action == "update":
        return await _handle_update(id_suffix, memory_id, updates or {}, reason, db)

    if action == "unlink":
        return await _handle_unlink(edge_id or "", reason, db)

    # action == "correct"
    return await _handle_correct(id_suffix, memory_id, new_content or "", reason, db)


# ---------------------------------------------------------------------------
# Action: delete
# ---------------------------------------------------------------------------


async def _handle_delete(id_suffix: str, memory_id: str, reason: str | None, db: Any) -> dict:
    """
    Soft-delete: set is_active = false. The record stays in SurrealDB forever.

    We use type::record('memory', $id) so the query is parameterized and
    SurrealDB resolves the full record reference without string interpolation.
    """
    # First, check if the memory actually exists and is active.
    # We do this so we can give a useful "not_found" response instead of
    # silently succeeding on a non-existent ID.
    existing = await query(
        db,
        "SELECT id, content FROM type::record('memory', $id)",
        {"id": id_suffix},
    )

    # If not found or empty result, return a not_found response
    from qmemory.formatters.response import attach_meta

    if not existing:
        logger.warning("correct_memory(delete): memory %s not found", memory_id)
        return attach_meta(
            {"ok": False, "action": "not_found", "memory_id": memory_id},
        )

    # Soft-delete — set is_active = false and bump updated_at
    await query(
        db,
        "UPDATE type::record('memory', $id) SET is_active = false, updated_at = time::now()",
        {"id": id_suffix},
    )

    logger.info("correct_memory: soft-deleted %s (reason: %s)", memory_id, reason or "none")

    return attach_meta(
        {"ok": True, "action": "deleted", "memory_id": memory_id},
        actions_context={"type": "correct", "memory_id": memory_id},
    )


# ---------------------------------------------------------------------------
# Action: update
# ---------------------------------------------------------------------------


async def _handle_update(id_suffix: str, memory_id: str, updates: dict, reason: str | None, db: Any) -> dict:
    """
    Update metadata fields directly WITHOUT creating a new version.

    This is for lightweight changes — bumping salience, expiring a fact,
    changing scope. No version chain is created.

    Allowed fields: salience, scope, valid_until, category, confidence.
    Any other keys in the updates dict are silently ignored to prevent
    injecting arbitrary fields into the DB.
    """
    # These are the only fields we allow direct updates on.
    # We deliberately whitelist to avoid the agent setting system fields
    # like is_active, created_at, or id directly.
    ALLOWED_UPDATE_FIELDS = {"salience", "scope", "valid_until", "category", "confidence", "content"}

    # Build the SET clause dynamically, only including allowed fields.
    # This produces something like: "salience = $salience, scope = $scope"
    set_clauses = []
    params: dict[str, Any] = {"id": id_suffix}

    for field, value in updates.items():
        if field not in ALLOWED_UPDATE_FIELDS:
            # Skip unrecognized fields — log a warning but don't crash
            logger.warning("correct_memory(update): ignoring unknown field '%s'", field)
            continue

        if field == "valid_until":
            # valid_until is option<datetime> — needs explicit type cast in SurrealQL
            set_clauses.append("valid_until = <datetime>$valid_until")
            params["valid_until"] = value
        else:
            # All other allowed fields are simple value assignments
            set_clauses.append(f"{field} = ${field}")
            params[field] = value

    # If nothing valid was provided, bail out early
    if not set_clauses:
        from qmemory.formatters.response import attach_meta
        return attach_meta(
            {"ok": True, "action": "updated", "memory_id": memory_id,
             "changes": {}},
        )

    # Always bump updated_at so we know when the last edit happened
    set_clauses.append("updated_at = time::now()")

    # Execute the UPDATE query
    set_clause_str = ", ".join(set_clauses)
    await query(
        db,
        f"UPDATE type::record('memory', $id) SET {set_clause_str}",
        params,
    )

    from qmemory.formatters.response import attach_meta

    logger.info("correct_memory: updated %s fields=%s (reason: %s)", memory_id, list(updates.keys()), reason or "none")

    return attach_meta(
        {
            "ok": True,
            "action": "updated",
            "memory_id": memory_id,
            "changes": {k: v for k, v in updates.items() if k in ALLOWED_UPDATE_FIELDS},
        },
        actions_context={"type": "correct", "memory_id": memory_id},
    )


# ---------------------------------------------------------------------------
# Action: unlink
# ---------------------------------------------------------------------------


async def _handle_unlink(edge_id: str, reason: str | None, db: Any) -> dict:
    """
    Hard-delete a `relates` edge from the graph.

    Unlike memories (which are always soft-deleted), EDGES are hard-deleted.
    The two memory nodes remain — just the connection between them is removed.

    edge_id should be the full record ID like "relates:rel1710864000000abc"
    or just the suffix "rel1710864000000abc".
    """
    # Normalize the edge_id — strip the "relates:" table prefix if present
    if ":" in edge_id:
        edge_table, edge_suffix = edge_id.split(":", 1)
    else:
        edge_table = "relates"
        edge_suffix = edge_id

    # Hard-delete the edge record
    await query(
        db,
        "DELETE type::record($table, $id)",
        {"table": edge_table, "id": edge_suffix},
    )

    from qmemory.formatters.response import attach_meta

    logger.info("correct_memory: deleted edge %s (reason: %s)", edge_id, reason or "none")

    return attach_meta(
        {"ok": True, "action": "unlinked", "edge_id": edge_id},
    )


# ---------------------------------------------------------------------------
# Action: correct
# ---------------------------------------------------------------------------


async def _handle_correct(id_suffix: str, memory_id: str, new_content: str, reason: str | None, db: Any) -> dict:
    """
    Fix a memory's content with a full version chain.

    Steps:
    1. Read the existing memory (to copy its metadata to the new version)
    2. Soft-delete the old memory
    3. Create a new memory with the corrected content + same metadata
    4. RELATE new_memory->prev_version->old_memory (audit trail edge)

    The old memory is preserved in the DB with is_active=false.
    The agent can always look back at history using the prev_version edge.
    """

    # --- Step 1: Read the existing memory ---
    # We need the old metadata (category, salience, scope, etc.) to copy
    # to the new version. We don't require is_active = true here so that
    # we can detect already-deleted memories and give a clear error.
    existing = await query(
        db,
        "SELECT * FROM type::record('memory', $id)",
        {"id": id_suffix},
    )

    from qmemory.formatters.response import attach_meta

    # Not found at all
    if not existing:
        logger.warning("correct_memory(correct): memory %s not found", memory_id)
        return attach_meta(
            {"ok": False, "action": "not_found", "memory_id": memory_id},
        )

    old_memory = existing[0]

    # --- Step 2: Soft-delete the old memory ---
    await query(
        db,
        "UPDATE type::record('memory', $id) SET is_active = false, updated_at = time::now()",
        {"id": id_suffix},
    )

    # --- Step 3: Create the corrected version ---
    # We copy all the metadata from the old memory. The agent's correction
    # only changes the content — everything else (category, salience, scope)
    # carries over unless explicitly changed later via action="update".
    new_id_suffix = generate_id("mem")
    new_memory_id = f"memory:{new_id_suffix}"

    # Build the CREATE query with required fields copied from the old memory
    # Optional fields (source_person, context_mood, valid_from, valid_until)
    # are only added if they existed in the old memory — SurrealDB 3.0
    # rejects NULL for option<> fields.
    base_create_query = """CREATE type::record('memory', $new_id) SET
    content = $new_content,
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

    create_params: dict[str, Any] = {
        "new_id": new_id_suffix,
        "new_content": new_content,
        # Copy metadata from old memory, with safe fallbacks
        "category":      old_memory.get("category", "context"),
        "salience":      old_memory.get("salience", 0.5),
        "scope":         old_memory.get("scope", "global"),
        "confidence":    old_memory.get("confidence", 0.8),
        "evidence_type": old_memory.get("evidence_type", "observed"),
        "source_type":   old_memory.get("source_type", "conversation"),
    }

    # Add optional fields from the old memory only if they exist
    optional_parts: list[str] = []

    if old_memory.get("source_person"):
        # source_person is a record<entity> FK — need type::record() wrapper
        optional_parts.append("source_person = type::record('entity', $source_person)")
        # The value might be "entity:entXXX" — extract the suffix
        sp = old_memory["source_person"]
        create_params["source_person"] = sp.split(":", 1)[1] if ":" in str(sp) else str(sp)

    if old_memory.get("context_mood"):
        optional_parts.append("context_mood = $context_mood")
        create_params["context_mood"] = old_memory["context_mood"]

    if old_memory.get("valid_from"):
        optional_parts.append("valid_from = <datetime>$valid_from")
        create_params["valid_from"] = str(old_memory["valid_from"])

    if old_memory.get("valid_until"):
        optional_parts.append("valid_until = <datetime>$valid_until")
        create_params["valid_until"] = str(old_memory["valid_until"])

    if optional_parts:
        base_create_query += ",\n    " + ",\n    ".join(optional_parts)

    base_create_query += ";"

    await query(db, base_create_query, create_params)

    # --- Step 4: Create the prev_version edge ---
    # RELATE new_memory->prev_version->old_memory
    # This is the audit trail — the agent can traverse backwards through
    # the version history to see what the fact used to say.
    # RELATE needs record syntax, not type::record() function calls.
    # Build the statement with the IDs directly (they come from our own generate_id, safe).
    await query(
        db,
        f"RELATE memory:`{new_id_suffix}`->prev_version->memory:`{id_suffix}` SET created_at = time::now()",
    )

    logger.info(
        "correct_memory: corrected %s → %s (reason: %s)",
        memory_id, new_memory_id, reason or "none"
    )

    return attach_meta(
        {
            "ok": True,
            "action": "corrected",
            "old_id": memory_id,
            "new_id": new_memory_id,
            "changes": {"content": f"{(old_memory.get('content', ''))[:40]} → {new_content[:40]}"},
        },
        actions_context={"type": "correct", "new_memory_id": new_memory_id},
    )
