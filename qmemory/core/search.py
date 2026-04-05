"""
Core Search — Agent's Primary Memory Retrieval with Graph Enrichment

Combines the recall pipeline with:
  1. Pinned separation — high-salience memories in their own section
  2. Entity search — persons/concepts matching the query
  3. Graph enrichment — neighbor hints on top results
  4. Structured actions — next-step tool calls (not text nudges)
  5. Meta — pagination info (returned, offset, has_more)

Response format:
  {
    "pinned": [...],     # salience >= 0.9 (max 3)
    "entities": [...],   # matched persons/concepts with memory_count
    "results": [...],    # relevance-ranked, each with neighbors
    "actions": [...],    # structured tool call suggestions
    "meta": {...}        # returned, offset, has_more
  }
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.core.recall import MEMORY_FIELDS, _format_age, recall
from qmemory.db.client import get_db, query
from qmemory.formatters.response import attach_meta

logger = logging.getLogger(__name__)


# How many top results to enrich with connection hints
TOP_N_ENRICH = 5
# Max connection hints per result
MAX_HINTS_PER_RESULT = 3
# Max pinned memories to separate
MAX_PINNED = 3
# Salience threshold for pinned
PINNED_THRESHOLD = 0.9


async def search_memories(
    query_text: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 20,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
    include_tool_calls: bool = False,
    owner_id: str | None = None,
    source_type: str | None = None,
    db: Any = None,
) -> dict:
    """Search memories with graph enrichment. Returns structured JSON with
    pinned, entities, results, actions, and meta."""

    logger.debug("Searching with owner=%s", owner_id)
    categories = [category] if category else None

    async def _run(conn: Any) -> dict:
        # Step 1: Run recall pipeline (fetch extra to account for pinned extraction)
        raw_results = await recall(
            query_text=query_text,
            scope=scope,
            categories=categories,
            limit=limit + MAX_PINNED,
            offset=offset,
            owner_id=owner_id,
            source_type=source_type,
            after=after,
            before=before,
            db=conn,
        )

        # Step 2: Separate pinned (salience >= threshold) from regular results
        pinned: list[dict] = []
        regular: list[dict] = []
        for r in raw_results:
            if r.get("salience", 0) >= PINNED_THRESHOLD and len(pinned) < MAX_PINNED:
                pinned.append(_format_result(r))
            else:
                regular.append(r)

        # Step 3: Enrich regular results with graph connections
        regular = await _enrich_with_connections(regular, conn)

        # Step 4: Format results
        formatted_results = [_format_result(r) for r in regular[:limit]]

        # Step 5: Search entities in parallel
        entities: list[dict] = []
        if query_text:
            entities = await _search_entities(query_text, conn)

        # Step 6: Build response
        has_more = len(regular) > limit

        # Find first connected result and first entity for action suggestions
        first_connected = next(
            (r for r in formatted_results if r.get("neighbors", {}).get("count", 0) > 0),
            None,
        )
        first_entity = entities[0] if entities else None

        response = {
            "pinned": pinned,
            "entities": entities,
            "results": formatted_results,
        }

        return attach_meta(
            response,
            actions_context={
                "type": "search",
                "memory_id": first_connected["id"] if first_connected else None,
                "neighbor_count": first_connected["neighbors"]["count"] if first_connected else 0,
                "entity_id": first_entity["id"] if first_entity else None,
            },
            returned=len(formatted_results),
            offset=offset,
            has_more=has_more,
        )

    if db is not None:
        return await _run(db)
    else:
        async with get_db() as conn:
            return await _run(conn)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def _format_result(r: dict) -> dict:
    """Format a raw recall result into the agent-facing format."""
    formatted = {
        "id": str(r.get("id", "")),
        "content": r.get("content", ""),
        "category": r.get("category", ""),
        "salience": r.get("salience", 0),
        "relevance": round(r.get("_score", r.get("salience", 0)), 3),
        "source_tier": r.get("source_tier", "unknown"),
        "age": _format_age(r.get("created_at")),
        "created_at": str(r.get("created_at", "")),
    }

    # Convert old connections format to new neighbors format
    if "connections" in r:
        conn = r["connections"]
        formatted["neighbors"] = {
            "count": conn.get("total", 0),
            "items": [
                {
                    "id": h.get("target", ""),
                    "content_preview": h.get("target_name", "")[:80],
                    "edge_type": h.get("type", "relates"),
                    "edge_direction": "out",
                }
                for h in conn.get("hints", [])
            ],
        }
    else:
        formatted["neighbors"] = {"count": 0, "items": []}

    return formatted


# ---------------------------------------------------------------------------
# Entity search
# ---------------------------------------------------------------------------


async def _search_entities(query_text: str, db: Any) -> list[dict]:
    """Search entity table for persons/concepts matching the query."""
    try:
        params: dict[str, Any] = {"query": query_text.lower()}

        entity_surql = """
        SELECT id, name, type
        FROM entity
        WHERE is_active != false
            AND (
                string::contains(string::lowercase(name), $query)
                OR $query IN aliases
            )
        LIMIT 5;
        """

        rows = await query(db, entity_surql, params)
        if not rows or not isinstance(rows, list):
            return []

        entities = []
        for e in rows:
            if not isinstance(e, dict) or not e.get("id"):
                continue

            eid = str(e["id"])
            # Count linked edges (memories + other entities)
            count_rows = await query(
                db,
                "SELECT count() AS c FROM relates WHERE in = <record>$eid OR out = <record>$eid GROUP ALL",
                {"eid": eid},
            )
            mem_count = 0
            if count_rows and isinstance(count_rows, list) and len(count_rows) > 0:
                mem_count = count_rows[0].get("c", 0)

            entities.append({
                "id": eid,
                "name": e.get("name", ""),
                "type": e.get("type", ""),
                "memory_count": mem_count,
            })

        return entities

    except Exception as ex:
        logger.debug("Entity search failed (non-fatal): %s", ex)
        return []


# ---------------------------------------------------------------------------
# Graph enrichment (batch query — NOT N+1)
# ---------------------------------------------------------------------------


async def _enrich_with_connections(
    results: list[dict],
    db: Any,
) -> list[dict]:
    """Enrich the top N search results with graph connection hints."""
    if not results:
        return results

    top_results = results[:TOP_N_ENRICH]
    top_ids = [str(r["id"]) for r in top_results if r.get("id")]

    if not top_ids:
        return results

    try:
        # Fetch neighbors for each top result individually
        # (FROM [id_list] has issues in some SurrealDB v3 configurations)
        enrichment_map: dict[str, dict] = {}
        for tid in top_ids:
            enrichment_surql = f"""
            SELECT id,
                ->relates->memory.{{id, content, category}} AS out_memories,
                ->relates->entity.{{id, name, type}} AS out_entities,
                <-relates<-memory.{{id, content, category}} AS in_memories,
                <-relates<-entity.{{id, name, type}} AS in_entities
            FROM {tid}
            """
            enrichment_rows = await query(db, enrichment_surql)
            if enrichment_rows and isinstance(enrichment_rows, list):
                for row in enrichment_rows:
                    if isinstance(row, dict) and row.get("id"):
                        enrichment_map[str(row["id"])] = row

        # Attach connection hints to top N results
        enriched_results = list(results)

        for i, mem in enumerate(enriched_results[:TOP_N_ENRICH]):
            mem_id = str(mem.get("id", ""))
            connections = enrichment_map.get(mem_id, {})

            out_memories = connections.get("out_memories") or []
            out_entities = connections.get("out_entities") or []
            in_memories = connections.get("in_memories") or []
            in_entities = connections.get("in_entities") or []
            all_links = out_memories + out_entities + in_memories + in_entities

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
                continue

            hints = []
            for link in valid_links[:MAX_HINTS_PER_RESULT]:
                target_name = link.get("name") or (link.get("content") or "")[:80]
                hints.append({
                    "type": "relates",
                    "target": str(link.get("id", "")),
                    "target_name": target_name,
                })

            enriched_results[i] = {
                **mem,
                "connections": {
                    "total": len(valid_links),
                    "hints": hints,
                },
            }

        return enriched_results

    except Exception as e:
        logger.debug("Search enrichment failed (non-fatal): %s", e)
        return results
