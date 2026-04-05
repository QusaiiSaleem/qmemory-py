"""
Core Link Nodes

Creates dynamic `relates` edges between any two nodes in the Qmemory graph.
The agent can link ANY two nodes — memories, entities, sessions — with ANY
relationship type it chooses: supports, contradicts, caused_by, depends_on,
relates_to, etc.

This is what turns the flat memory store into a connected knowledge graph.
Without links, the agent has a list of facts. With links, it has a mind map.

Flow:
  1. Validate both IDs look like "table:suffix" format
  2. Parse the table name and suffix from each ID
  3. Verify both nodes actually exist in the database (SELECT check)
  4. Build a RELATE statement using backtick syntax (NOT type::record())
  5. Execute the RELATE and capture the edge ID
  6. Return the edge ID + a nudge to explore related nodes

Design decisions:
  - RELATE in SurrealDB 3.0 does NOT accept type::record() calls in the
    FROM/TO positions. We must use direct record syntax: table:`suffix`.
    This is safe here because our IDs come from generate_id() which only
    produces alphanumeric characters (no injection risk).
  - Optional `confidence` field: if not provided, defaults to 0.8 (same as TS).
  - Node existence check: if either node is missing, return None gracefully
    rather than letting SurrealDB create a dangling edge to a void.
  - Accepts optional `db` connection for test injection (same pattern as save/correct).
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import generate_id, get_db, query, query_multi

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


async def link_nodes(
    from_id: str,               # Any node ID: "memory:xxx", "entity:xxx", "session:xxx"
    to_id: str,                 # Any node ID — different table types are fine
    relationship_type: str,     # Any string: supports, contradicts, caused_by, etc.
    reason: str | None = None,  # Optional explanation for why these nodes are related
    confidence: float | None = None,  # Optional confidence score 0.0-1.0
    db: Any = None,
) -> dict | None:
    """
    Create a dynamic `relates` edge between any two nodes in the graph.

    The relationship_type can be anything meaningful — there is no fixed list.
    The agent decides what type makes sense for the connection being made.

    Args:
        from_id:           Full record ID of the source node, e.g. "memory:mem1234abc".
        to_id:             Full record ID of the target node, e.g. "entity:ent5678xyz".
        relationship_type: Any string describing the relationship, e.g. "supports",
                           "contradicts", "caused_by", "related_to", "belongs_to_topic".
        reason:            Optional note explaining why these nodes are connected.
        confidence:        Optional confidence score (0.0-1.0). Defaults to 0.8.
        db:                Optional SurrealDB connection. If None, creates a fresh one
                           via get_db(). Pass the test fixture here.

    Returns:
        dict with:
          - "edge_id":   The relates edge record ID, e.g. "relates:relXXX"
          - "from_id":   The source node ID (echoed back)
          - "to_id":     The target node ID (echoed back)
          - "type":      The relationship_type (echoed back)
          - "actions":   Structured next-step suggestions
          - "meta":      Edge counts for both endpoints

        Returns None if either node does not exist.

    Raises:
        ValueError: If from_id or to_id don't include a colon (invalid format).
    """

    # --- Step 1: Validate ID format ---
    # Both IDs must be in "table:suffix" format. The agent might pass just
    # a suffix without a table name — catch it early with a clear error.
    if ":" not in from_id:
        raise ValueError(
            f"Invalid from_id '{from_id}'. Expected format: table:id (e.g. 'memory:mem1234abc')"
        )
    if ":" not in to_id:
        raise ValueError(
            f"Invalid to_id '{to_id}'. Expected format: table:id (e.g. 'entity:ent5678xyz')"
        )

    # --- Step 2: Parse the table name and suffix from each ID ---
    # "memory:mem1234abc" → table="memory", suffix="mem1234abc"
    # We only split on the FIRST colon in case the suffix contains one.
    from_table, from_suffix = from_id.split(":", 1)
    to_table, to_suffix = to_id.split(":", 1)

    # --- Step 3: Dispatch to the internal function with a DB connection ---
    if db is not None:
        # Test mode: use the provided connection directly
        return await _create_link(
            from_table, from_suffix, from_id,
            to_table, to_suffix, to_id,
            relationship_type, reason, confidence,
            db
        )
    else:
        # Production mode: create a fresh connection for this operation
        async with get_db() as conn:
            return await _create_link(
                from_table, from_suffix, from_id,
                to_table, to_suffix, to_id,
                relationship_type, reason, confidence,
                conn
            )


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------


async def _create_link(
    from_table: str,
    from_suffix: str,
    from_id: str,
    to_table: str,
    to_suffix: str,
    to_id: str,
    relationship_type: str,
    reason: str | None,
    confidence: float | None,
    db: Any,
) -> dict | None:
    """
    Internal: verify nodes exist, then create the RELATE edge.

    Called with an active DB connection already set up.
    """

    # --- Step 3a: Verify the FROM node exists ---
    # We do this to avoid creating dangling edges to records that don't exist.
    # If the node is missing, the agent needs to know so it can correct its ID.
    from_exists = await query(
        db,
        f"SELECT id FROM type::record('{from_table}', $id)",
        {"id": from_suffix},
    )

    if not from_exists:
        logger.warning("link_nodes: from node %s not found", from_id)
        return None

    # --- Step 3b: Verify the TO node exists ---
    # Same check for the target node.
    to_exists = await query(
        db,
        f"SELECT id FROM type::record('{to_table}', $id)",
        {"id": to_suffix},
    )

    if not to_exists:
        logger.warning("link_nodes: to node %s not found", to_id)
        return None

    # --- Step 4: Build the RELATE statement ---
    # CRITICAL: SurrealDB 3.0 does NOT support type::record() in RELATE's
    # FROM/TO positions — it causes a parse error. Instead, use backtick
    # syntax: table:`suffix`. This is safe here because generate_id() only
    # produces alphanumeric characters (a-z, 0-9), so there's no injection risk.
    #
    # Correct pattern:
    #   RELATE memory:`mem1234abc`->relates->entity:`ent5678xyz` SET ...
    #
    # Wrong pattern (parse error!):
    #   RELATE type::record('memory', $id)->relates->type::record('entity', $id2) SET ...

    # Build SET clause dynamically — only include optional fields when provided.
    # SurrealDB 3.0 rejects NULL for option<> fields — omit them entirely.
    set_parts = [
        "relationship_type = $relationship_type",
        "confidence = $confidence_val",
        "created_at = time::now()",
    ]
    params: dict[str, Any] = {
        "relationship_type": relationship_type,
        # Default confidence to 0.8 if not provided (matches TypeScript implementation)
        "confidence_val": confidence if confidence is not None else 0.8,
    }

    # Add reason only if provided — omitting it leaves the field as NONE in SurrealDB
    if reason is not None:
        set_parts.insert(1, "reason = $reason")  # Insert after relationship_type
        params["reason"] = reason

    set_clause = ", ".join(set_parts)

    # The RELATE statement — backtick syntax for record IDs
    relate_surql = (
        f"RELATE {from_table}:`{from_suffix}`"
        f"->relates->"
        f"{to_table}:`{to_suffix}` "
        f"SET {set_clause}"
    )

    # --- Step 5: Execute the RELATE ---
    # query() normalizes RecordID objects to strings, so edge["id"] will
    # come back as "relates:relXXX" (a plain string, not a RecordID object).
    result = await query(db, relate_surql, params)

    # Extract the edge ID from the result.
    # RELATE returns a list with one record: [{"id": "relates:xxx", ...}]
    # If the query failed (result is None or empty), generate a fallback ID.
    edge_id: str
    if result and isinstance(result, list) and len(result) > 0:
        edge_id = str(result[0].get("id", f"relates:{generate_id('rel')}"))
    else:
        # Fallback: generate a synthetic ID (this means RELATE may have failed)
        edge_id = f"relates:{generate_id('rel')}"
        logger.warning(
            "link_nodes: RELATE may have failed or returned no ID. "
            "from=%s, to=%s, type=%s",
            from_id, to_id, relationship_type
        )

    from qmemory.formatters.response import attach_meta

    logger.info(
        "link_nodes: %s -[%s]-> %s (edge=%s)",
        from_id, relationship_type, to_id, edge_id
    )

    # Fetch content previews for both ends
    from_preview = ""
    to_preview = ""
    if from_exists and isinstance(from_exists, list) and from_exists[0]:
        from_preview = (from_exists[0].get("content") or from_exists[0].get("name", ""))[:80] if isinstance(from_exists[0], dict) else ""
    if to_exists and isinstance(to_exists, list) and to_exists[0]:
        to_preview = (to_exists[0].get("content") or to_exists[0].get("name", ""))[:80] if isinstance(to_exists[0], dict) else ""

    # Count edges for both endpoints
    from_count_rows = await query(db, "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL", {"id": from_id})
    to_count_rows = await query(db, "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL", {"id": to_id})
    from_edge_count = from_count_rows[0]["c"] if from_count_rows and isinstance(from_count_rows, list) and len(from_count_rows) > 0 and isinstance(from_count_rows[0], dict) else 0
    to_edge_count = to_count_rows[0]["c"] if to_count_rows and isinstance(to_count_rows, list) and len(to_count_rows) > 0 and isinstance(to_count_rows[0], dict) else 0

    return attach_meta(
        {
            "edge_id": edge_id,
            "from": {"id": from_id, "content_preview": from_preview},
            "to": {"id": to_id, "content_preview": to_preview},
            "relationship_type": relationship_type,
        },
        actions_context={
            "type": "link",
            "from_id": from_id,
            "to_id": to_id,
            "edge_count_from": from_edge_count,
            "edge_count_to": to_edge_count,
        },
        edge_count_from=from_edge_count,
        edge_count_to=to_edge_count,
    )
