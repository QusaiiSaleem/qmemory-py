"""
Core Search — Agent's Primary Memory Retrieval with Graph Enrichment

This module is the agent's main way to find memories. It wraps the
4-tier recall pipeline (from recall.py) and adds two things on top:

  1. Graph enrichment — for the top 5 results, it fetches their connections
     (relates edges) in a SINGLE batch query (not N+1!) and attaches
     "connection hints" to each result.

  2. Adaptive nudge — a `_nudge` string that tells the agent what to do
     next based on what was found.

Why the nudge matters:
  The agent only explores the graph if it SEES connections. Without hints,
  a result is just a flat fact. With hints, it becomes a node in a knowledge
  graph — the agent can follow edges to discover related memories.

The "mind map" principle:
  Every search result should feel like a node with visible edges, not a
  row in a flat database. The hints + nudge implement this.

Flow:
  1. Call recall() from core/recall.py to get base results (4-tier pipeline)
  2. For top 5 results, batch-query ALL their graph connections (single query)
  3. Attach connection hints (total count + 3 best targets) to each result
  4. Generate an adaptive nudge based on what was found
  5. Return {"results": [...], "_nudge": "..."}
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.core.recall import recall
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# How many top results to enrich with connection hints
# ---------------------------------------------------------------------------

TOP_N_ENRICH = 5

# Max connection hints to show per result (enough to trigger curiosity,
# not so many that they overwhelm the context window)
MAX_HINTS_PER_RESULT = 3


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------


async def search_memories(
    query_text: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 20,
    include_tool_calls: bool = False,
    owner_id: str | None = None,
    db: Any = None,
) -> dict:
    """
    Search memories and enrich top results with graph connection hints.

    This is the agent-facing search function. It combines the 4-tier recall
    pipeline with graph enrichment and adaptive nudges to help the agent
    navigate the knowledge graph.

    Args:
        query_text:        Free-text search query (BM25 + vector). Pass None
                           to get recent memories without text search.
        category:          Filter to a single category (e.g. "context", "self").
                           Passed to recall() as a single-element list.
        scope:             Filter to a specific scope (e.g. "global", "topic:7").
                           Passed directly to recall().
        limit:             Max number of results to return. Default 20.
        include_tool_calls: If True, a future version will include tool call
                           history in results. Placeholder for now — not yet
                           implemented (tool_call table search comes later).
        owner_id:          Optional user ID for multi-tenant cloud mode. When set,
                           filters results to only memories owned by this user.
                           When None (local mode), no owner filter is applied.
        db:                Optional SurrealDB connection. If None, creates a
                           fresh one via get_db(). Pass a test fixture here.

    Returns:
        dict with two keys:
          - "results": list of memory dicts, sorted by salience DESC.
                       Top 5 have a "connections" key with graph hints.
          - "_nudge":  A string telling the agent what to do next.

    Example return:
        {
          "results": [
            {
              "id": "memory:mem123",
              "content": "Budget is 500K",
              "category": "context",
              "salience": 0.9,
              "connections": {
                "total": 2,
                "hints": [
                  {"type": "relates", "target": "entity:ent456", "target_name": "Budget Project"}
                ]
              }
            },
            ...
          ],
          "_nudge": "Memory memory:mem123 has connections. Explore: qmemory_search(query='...')"
        }
    """
    logger.debug("Searching with owner=%s", owner_id)

    # Convert single category to the list format that recall() expects
    categories = [category] if category else None

    # --- Step 1: Run the 4-tier recall pipeline ---
    # recall() handles: graph traversal → BM25+vector → category filter → recent fallback
    # It deduplicates, sorts by salience DESC, and trims to limit.
    if db is not None:
        # Test mode: use the provided connection
        results = await recall(
            query_text=query_text,
            scope=scope,
            categories=categories,
            limit=limit,
            owner_id=owner_id,
            db=db,
        )
    else:
        # Production mode: create a fresh connection for the full operation
        # We open ONE connection here so both recall() and enrichment
        # share the same connection (avoids opening two connections).
        async with get_db() as conn:
            results = await recall(
                query_text=query_text,
                scope=scope,
                categories=categories,
                limit=limit,
                owner_id=owner_id,
                db=conn,
            )
            # Run graph enrichment on the same connection
            results = await _enrich_with_connections(results, conn)
            nudge = _build_nudge(results)
            return {"results": results, "_nudge": nudge}

    # Test mode: enrich using the provided db connection
    results = await _enrich_with_connections(results, db)

    # --- Step 4: Generate adaptive nudge ---
    nudge = _build_nudge(results)

    return {"results": results, "_nudge": nudge}


# ---------------------------------------------------------------------------
# Step 2+3: Graph enrichment (batch query — NOT N+1)
# ---------------------------------------------------------------------------


async def _enrich_with_connections(
    results: list[dict],
    db: Any,
) -> list[dict]:
    """
    Enrich the top N search results with graph connection hints.

    Fetches ALL connections for the top N results in a SINGLE batch query,
    then attaches hints to each result. This is O(1) queries, not O(N).

    What "connection hints" look like on a result:
        {
          "connections": {
            "total": 3,                          # Total edges from this memory
            "hints": [                           # Up to 3 most useful targets
              {"type": "relates", "target": "entity:ent123", "target_name": "Project X"},
              {"type": "relates", "target": "memory:mem456", "target_name": "Budget is 500K"}
            ]
          }
        }

    If a result has no connections, the "connections" key is omitted
    (not added with zeros — that would just clutter the output).

    Args:
        results:  The full list of recall results. Only top TOP_N_ENRICH
                  are enriched; the rest are returned unchanged.
        db:       An active SurrealDB connection.

    Returns:
        The same results list with connection hints added to the top N.
    """
    if not results:
        return results

    # Only enrich the top N results (saves query time)
    top_results = results[:TOP_N_ENRICH]

    # Collect IDs for the top N results
    # IDs are strings like "memory:mem123abc" — we built them with generate_id()
    # using only alphanumeric chars, so they're safe to embed in the query.
    top_ids = [str(r["id"]) for r in top_results if r.get("id")]

    if not top_ids:
        return results

    try:
        # --- BATCH GRAPH QUERY ---
        # We fetch both outgoing (→) and incoming (←) relates edges
        # for all top IDs in a single SELECT.
        #
        # Why we can't use $ids parameter for record IDs:
        # SurrealDB's IN operator works with record IDs in a list, but
        # we're selecting FROM a dynamic list of IDs. The FROM [...] syntax
        # requires literal IDs. Since our IDs are generated by generate_id()
        # (format: "mem" + digits + 3 lowercase letters), they contain only
        # alphanumeric chars — safe to embed directly.
        #
        # Graph traversal syntax:
        #   ->relates->memory   means: follow outgoing "relates" edges to memory nodes
        #   ->relates->entity   means: follow outgoing "relates" edges to entity nodes
        #   <-relates<-memory   means: follow INCOMING "relates" edges from memory nodes
        #   <-relates<-entity   means: follow INCOMING "relates" edges from entity nodes
        #
        # The .{id, content, category} syntax selects specific fields from the targets.
        id_list = ", ".join(top_ids)

        enrichment_surql = f"""
        SELECT id,
            ->relates->memory.{{id, content, category}} AS out_memories,
            ->relates->entity.{{id, name, type}} AS out_entities,
            <-relates<-memory.{{id, content, category}} AS in_memories,
            <-relates<-entity.{{id, name, type}} AS in_entities
        FROM [{id_list}]
        """

        enrichment_rows = await query(db, enrichment_surql)

        if not enrichment_rows or not isinstance(enrichment_rows, list):
            # Enrichment failed gracefully — return results without hints
            logger.debug("Search enrichment: no rows returned, skipping hints")
            return results

        # --- Build a lookup map: memory_id → connections ---
        # Key: the string ID of the memory (e.g. "memory:mem123")
        # Value: dict with out_memories, out_entities, in_memories, in_entities
        enrichment_map: dict[str, dict] = {}
        for row in enrichment_rows:
            if isinstance(row, dict) and row.get("id"):
                enrichment_map[str(row["id"])] = row

        # --- Attach connection hints to top N results ---
        # We modify a copy of the list (don't mutate the original in place)
        enriched_results = list(results)  # shallow copy

        for i, mem in enumerate(enriched_results[:TOP_N_ENRICH]):
            mem_id = str(mem.get("id", ""))
            connections = enrichment_map.get(mem_id, {})

            # Combine all connections: outgoing + incoming, memories + entities.
            # Note: use explicit list() calls to avoid operator precedence bugs
            # with `or`. The `or []` pattern must wrap each get() individually
            # before concatenation.
            out_memories = connections.get("out_memories") or []
            out_entities = connections.get("out_entities") or []
            in_memories = connections.get("in_memories") or []
            in_entities = connections.get("in_entities") or []
            all_links = out_memories + out_entities + in_memories + in_entities

            # Filter out None/empty entries and deduplicate by target ID
            valid_links = []
            seen_targets: set[str] = set()
            for link in all_links:
                if not isinstance(link, dict):
                    continue
                link_id = str(link.get("id", ""))
                if link_id and link_id not in seen_targets:
                    seen_targets.add(link_id)
                    valid_links.append(link)

            if not valid_links:
                # No connections — don't add the key (keep output clean)
                continue

            # Build hint objects: at most MAX_HINTS_PER_RESULT
            hints = []
            for link in valid_links[:MAX_HINTS_PER_RESULT]:
                # "name" for entities, first 80 chars of "content" for memories
                target_name = link.get("name") or (link.get("content") or "")[:80]
                hints.append({
                    "type": "relates",
                    "target": str(link.get("id", "")),
                    "target_name": target_name,
                })

            # Attach to the result dict
            # We need to make a copy so we don't mutate the original recall result
            enriched_results[i] = {
                **mem,
                "connections": {
                    "total": len(valid_links),
                    "hints": hints,
                },
            }

        logger.debug(
            "Search enrichment: enriched %d of %d results",
            len([r for r in enriched_results[:TOP_N_ENRICH] if "connections" in r]),
            len(top_ids),
        )
        return enriched_results

    except Exception as e:
        # Graph enrichment is non-fatal — always return base results
        logger.debug("Search enrichment failed (non-fatal): %s", e)
        return results


# ---------------------------------------------------------------------------
# Step 4: Adaptive nudge
# ---------------------------------------------------------------------------


def _build_nudge(results: list[dict]) -> str:
    """
    Build an adaptive nudge string based on the search results.

    The nudge tells the agent what to do next:
      - If connections exist → explore them via another search
      - If results exist but no connections → build graph by linking memories
      - If no results → save what you learn

    This implements the "Visible Connections Drive Agent Behavior" principle:
    always give the agent a clear next step.

    Args:
        results: The enriched search results (with optional "connections" keys).

    Returns:
        A short instruction string for the agent.
    """
    # Check if any result has connections
    has_connections = any(
        r.get("connections", {}).get("total", 0) > 0
        for r in results
    )

    if has_connections:
        # Find the first result that has connections, use its ID in the nudge
        connected_mem = next(
            (r for r in results if r.get("connections", {}).get("total", 0) > 0),
            results[0],
        )
        mem_id = str(connected_mem.get("id", ""))
        total = connected_mem.get("connections", {}).get("total", 0)
        return (
            f"Memory {mem_id} has {total} connection(s). "
            f"Explore: qmemory_search(query='...')"
        )

    elif results:
        # We have results but no connections — encourage the agent to link them
        first_id = str(results[0].get("id", ""))
        second_id = str(results[1].get("id", "")) if len(results) > 1 else "..."
        return (
            f"Build the knowledge graph: "
            f"qmemory_link(from_id='{first_id}', to_id='{second_id}', type='relates_to')"
        )

    else:
        # No results at all — encourage the agent to save what it learns
        return (
            "No memories found. Save what you learn: "
            "qmemory_save(content='...', category='context')"
        )
