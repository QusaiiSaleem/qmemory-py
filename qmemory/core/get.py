"""
Core Get — Fetch memories/entities by ID with optional neighbor traversal.

The fundamental "read by ID" operation. Supports batch fetch (up to 20 IDs)
and graph neighbor expansion (depth 1-2).

This was the #1 missing tool identified in real-agent UX testing:
agents had IDs but no way to retrieve them directly.
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.core.recall import MEMORY_FIELDS, _format_age
from qmemory.db.client import get_db, query
from qmemory.formatters.response import attach_meta

logger = logging.getLogger(__name__)

MAX_IDS = 20
MAX_NEIGHBORS_PER_NODE = 10


async def get_memories(
    ids: list[str],
    include_neighbors: bool = False,
    neighbor_depth: int = 1,
    db: Any = None,
) -> dict:
    """Fetch memories or entities by ID, optionally with graph neighbors.

    Args:
        ids:                List of record IDs (e.g. ["memory:mem123", "entity:ent456"]).
        include_neighbors:  If True, fetch connected nodes for each result.
        neighbor_depth:     How deep to traverse (1 or 2). Max 2.
        db:                 Optional SurrealDB connection for test injection.

    Returns:
        dict with: memories, not_found, actions, meta

    Raises:
        ValueError: If more than 20 IDs are requested.
    """
    if len(ids) > MAX_IDS:
        raise ValueError(f"Maximum {MAX_IDS} IDs per request. Got {len(ids)}.")

    neighbor_depth = min(neighbor_depth, 2)

    if db is not None:
        return await _get_impl(ids, include_neighbors, neighbor_depth, db)
    else:
        async with get_db() as conn:
            return await _get_impl(ids, include_neighbors, neighbor_depth, conn)


async def _get_impl(
    ids: list[str],
    include_neighbors: bool,
    neighbor_depth: int,
    db: Any,
) -> dict:
    """Internal implementation — called with an active DB connection."""

    # Fetch from memory and entity tables separately to avoid cross-table issues
    # Group IDs by table prefix
    memory_ids = [rid for rid in ids if rid.startswith("memory:")]
    entity_ids = [rid for rid in ids if rid.startswith("entity:")]
    other_ids = [rid for rid in ids if not rid.startswith("memory:") and not rid.startswith("entity:")]

    rows: list[dict] = []

    # Fetch memories
    if memory_ids:
        mem_id_list = ", ".join(memory_ids)
        mem_rows = await query(
            db,
            f"SELECT {MEMORY_FIELDS} FROM [{mem_id_list}] WHERE is_active = true",
        )
        if mem_rows and isinstance(mem_rows, list):
            rows.extend(mem_rows)

    # Fetch entities
    if entity_ids:
        ent_id_list = ", ".join(entity_ids)
        ent_rows = await query(
            db,
            f"SELECT id, name, type, aliases, external_source, external_id, created_at, updated_at FROM [{ent_id_list}]",
        )
        if ent_rows and isinstance(ent_rows, list):
            rows.extend(ent_rows)

    # Fetch any other table types (session, etc.)
    if other_ids:
        other_id_list = ", ".join(other_ids)
        other_rows = await query(db, f"SELECT * FROM [{other_id_list}]")
        if other_rows and isinstance(other_rows, list):
            rows.extend(other_rows)

    # Build found/not_found lists
    found_ids = {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}
    not_found = [rid for rid in ids if rid not in found_ids]

    # Format results
    memories = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue

        mem: dict[str, Any] = {
            "id": str(r["id"]),
            "content": r.get("content") or r.get("name", ""),
            "category": r.get("category") or r.get("type", ""),
            "salience": r.get("salience", 0),
            "age": _format_age(r.get("created_at")),
            "created_at": str(r.get("created_at", "")),
        }

        # Entity-specific fields
        if r.get("name"):
            mem["name"] = r["name"]
        if r.get("type"):
            mem["type"] = r["type"]
        if r.get("aliases"):
            mem["aliases"] = r["aliases"]

        # Neighbor stub — filled below if requested
        mem["neighbors"] = {"count": 0, "items": []}

        memories.append(mem)

    # Fetch neighbors if requested
    if include_neighbors and memories:
        await _attach_neighbors(memories, neighbor_depth, db)

    response = {
        "memories": memories,
        "not_found": not_found,
    }

    return attach_meta(
        response,
        actions_context={
            "type": "get",
            "ids": ids,
            "include_neighbors": include_neighbors,
        },
        found=len(memories),
        requested=len(ids),
    )


async def _attach_neighbors(
    memories: list[dict],
    depth: int,
    db: Any,
) -> None:
    """Fetch graph neighbors for each memory and attach them in place."""

    # Fetch neighbors for each memory individually to avoid cross-table FROM issues
    all_rows: list[dict] = []
    for mem in memories:
        mid = mem["id"]
        # Use direct record reference instead of FROM [list]
        neighbor_surql = f"""
        SELECT id,
            ->relates.{{id, type, out}} AS out_edges,
            <-relates.{{id, type, in}} AS in_edges,
            ->relates->memory.{{id, content, category, salience}} AS out_memories,
            ->relates->entity.{{id, name, type}} AS out_entities,
            <-relates<-memory.{{id, content, category, salience}} AS in_memories,
            <-relates<-entity.{{id, name, type}} AS in_entities
        FROM {mid}
        """
        rows = await query(db, neighbor_surql)
        if rows and isinstance(rows, list):
            all_rows.extend(rows)

    if not all_rows:
        return

    rows = all_rows

    # Build lookup map: node_id → row data
    neighbor_map: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            neighbor_map[str(row["id"])] = row

    # Build edge type lookup: {node_id: {target_id: edge_type}}
    edge_types: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        node_id = str(row.get("id", ""))
        edge_types[node_id] = {}

        for edge in (row.get("out_edges") or []):
            if isinstance(edge, dict):
                target = str(edge.get("out", ""))
                edge_types[node_id][target] = edge.get("type", "relates")

        for edge in (row.get("in_edges") or []):
            if isinstance(edge, dict):
                source = str(edge.get("in", ""))
                edge_types[node_id][source] = edge.get("type", "relates")

    # Attach neighbors to each memory
    for mem in memories:
        mid = mem["id"]
        data = neighbor_map.get(mid, {})

        items: list[dict] = []
        seen: set[str] = set()

        # Helper to add a neighbor item
        def _add_memory_neighbor(m: dict, direction: str) -> None:
            if not isinstance(m, dict) or not m.get("id"):
                return
            tid = str(m["id"])
            if tid in seen:
                return
            seen.add(tid)
            items.append({
                "id": tid,
                "content_preview": (m.get("content") or "")[:80],
                "category": m.get("category", ""),
                "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                "edge_direction": direction,
            })

        def _add_entity_neighbor(e: dict, direction: str) -> None:
            if not isinstance(e, dict) or not e.get("id"):
                return
            tid = str(e["id"])
            if tid in seen:
                return
            seen.add(tid)
            items.append({
                "id": tid,
                "type": e.get("type", ""),
                "name": e.get("name", ""),
                "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                "edge_direction": direction,
            })

        for m in (data.get("out_memories") or []):
            _add_memory_neighbor(m, "out")
        for e in (data.get("out_entities") or []):
            _add_entity_neighbor(e, "out")
        for m in (data.get("in_memories") or []):
            _add_memory_neighbor(m, "in")
        for e in (data.get("in_entities") or []):
            _add_entity_neighbor(e, "in")

        mem["neighbors"] = {
            "count": len(items),
            "items": items[:MAX_NEIGHBORS_PER_NODE],
        }
