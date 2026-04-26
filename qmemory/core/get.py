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
    """Fetch graph neighbors (depth 1 or 2) and attach in place.

    Strategy: 1 batched query per depth level (not per input memory).

    Step 1 — Fetch direct (depth-1) neighbors for every input id in one
             query: `FROM [id1, id2, ...]` projects the same arrow
             expressions for every row.
    Step 2 — If depth >= 2: collect the depth-1 neighbor ids, batch the
             same projection on those, then stitch second-degree
             neighbors back to the originating input memory with
             `depth: 2` and `via: <d1 neighbor id>` so the agent can
             trace the path.

    SurrealDB v3 quirk: `SELECT id, ... FROM [r1, r2]` returns id=None
    for every row (the planner doesn't preserve the row record id when
    FROM is a list of explicit records). Use `meta::id(id)` and rebuild
    the full record id with `_id_with_prefix()` so we can stitch results
    by id rather than by position (defensive in case a row is missing).
    """
    if not memories:
        return

    input_ids = [m["id"] for m in memories if m.get("id")]
    if not input_ids:
        return

    # ----- Step 1: depth-1 neighbors for every input id, in one query
    d1_by_id = await _fetch_neighbors_batch(input_ids, db)

    # Build edge-type lookup: per-source, target_id -> edge_type
    edge_types: dict[str, dict[str, str]] = {}
    for src_id, data in d1_by_id.items():
        edge_types[src_id] = _build_edge_type_map(data)

    # Attach depth-1 items to each input memory
    per_mem_items: dict[str, list[dict]] = {}
    per_mem_seen: dict[str, set[str]] = {}

    for mem in memories:
        mid = mem.get("id")
        if not mid:
            continue
        data = d1_by_id.get(mid, {})
        items: list[dict] = []
        seen: set[str] = set()

        for m in (data.get("out_memories") or []):
            _maybe_add_memory(items, seen, m, "out", edge_types.get(mid, {}), depth_level=1)
        for e in (data.get("out_entities") or []):
            _maybe_add_entity(items, seen, e, "out", edge_types.get(mid, {}), depth_level=1)
        for m in (data.get("in_memories") or []):
            _maybe_add_memory(items, seen, m, "in", edge_types.get(mid, {}), depth_level=1)
        for e in (data.get("in_entities") or []):
            _maybe_add_entity(items, seen, e, "in", edge_types.get(mid, {}), depth_level=1)

        per_mem_items[mid] = items
        per_mem_seen[mid] = seen | {mid}  # never re-add the input itself

    # ----- Step 2 (only if requested): depth-2 neighbors
    if depth >= 2:
        # Collect every depth-1 neighbor across all input memories.
        d1_neighbor_ids: set[str] = set()
        for mid, items in per_mem_items.items():
            for item in items:
                d1_neighbor_ids.add(item["id"])
        # Don't re-fetch nodes we already have results for.
        d1_neighbor_ids -= set(input_ids)

        if d1_neighbor_ids:
            d2_by_id = await _fetch_neighbors_batch(list(d1_neighbor_ids), db)

            # Edge-type lookup for the d1->d2 hop (originates at d1 node).
            d2_edge_types: dict[str, dict[str, str]] = {}
            for src_id, data in d2_by_id.items():
                d2_edge_types[src_id] = _build_edge_type_map(data)

            # For each input memory, walk its d1 items and pull each d1
            # neighbor's own neighbors as d2 items, skipping anything
            # already in the items list (or the input memory itself).
            for mid, items in per_mem_items.items():
                seen = per_mem_seen[mid]
                # Iterate over a copy: items grows as we add d2.
                for d1_item in list(items):
                    d1_id = d1_item["id"]
                    d1_data = d2_by_id.get(d1_id)
                    if not d1_data:
                        continue
                    edge_map = d2_edge_types.get(d1_id, {})
                    for m in (d1_data.get("out_memories") or []):
                        _maybe_add_memory(items, seen, m, "out", edge_map, depth_level=2, via=d1_id)
                    for e in (d1_data.get("out_entities") or []):
                        _maybe_add_entity(items, seen, e, "out", edge_map, depth_level=2, via=d1_id)
                    for m in (d1_data.get("in_memories") or []):
                        _maybe_add_memory(items, seen, m, "in", edge_map, depth_level=2, via=d1_id)
                    for e in (d1_data.get("in_entities") or []):
                        _maybe_add_entity(items, seen, e, "in", edge_map, depth_level=2, via=d1_id)

    # Cap and attach. d1 items always come first (insertion order), so
    # the cap prefers direct neighbors over second-degree.
    for mem in memories:
        mid = mem.get("id")
        if not mid:
            continue
        items = per_mem_items.get(mid, [])
        mem["neighbors"] = {
            "count": len(items),
            "items": items[:MAX_NEIGHBORS_PER_NODE],
        }


async def _fetch_neighbors_batch(ids: list[str], db: Any) -> dict[str, dict]:
    """Fetch neighbors for all `ids` in one query. Returns {full_id: row_data}.

    Uses `meta::id(id)` projection because SurrealDB v3 returns id=None
    when FROM is a list of explicit records. We reconstruct the full id
    by prefixing the table name (extracted from the input id).
    """
    if not ids:
        return {}

    id_list = ", ".join(ids)
    surql = f"""
    SELECT
        meta::id(id) AS _row_id,
        ->relates.{{id, type, out}} AS out_edges,
        <-relates.{{id, type, in}} AS in_edges,
        ->relates->memory.{{id, content, category, salience}} AS out_memories,
        ->relates->entity.{{id, name, type}} AS out_entities,
        <-relates<-memory.{{id, content, category, salience}} AS in_memories,
        <-relates<-entity.{{id, name, type}} AS in_entities
    FROM [{id_list}]
    """

    try:
        rows = await query(db, surql)
    except Exception as ex:
        logger.debug("Neighbor batch fetch failed (non-fatal): %s", ex)
        return {}

    if not rows or not isinstance(rows, list):
        return {}

    # Stitch by id. Rows arrive in input order so we use that to
    # recover the table prefix (memory:xxx or entity:xxx) for each row.
    by_id: dict[str, dict] = {}
    for input_id, row in zip(ids, rows):
        if not isinstance(row, dict) or not row.get("_row_id"):
            continue
        # Prefer reconstructing from the input id (which we trust) so
        # heterogeneous inputs (memory + entity) keep the right prefix.
        by_id[input_id] = row
    return by_id


def _build_edge_type_map(data: dict) -> dict[str, str]:
    """Build {target_id: edge_type} from the projected out/in_edges arrays."""
    edge_map: dict[str, str] = {}
    for edge in (data.get("out_edges") or []):
        if isinstance(edge, dict):
            edge_map[str(edge.get("out", ""))] = edge.get("type", "relates")
    for edge in (data.get("in_edges") or []):
        if isinstance(edge, dict):
            edge_map[str(edge.get("in", ""))] = edge.get("type", "relates")
    return edge_map


def _maybe_add_memory(
    items: list[dict],
    seen: set[str],
    m: dict,
    direction: str,
    edge_map: dict[str, str],
    depth_level: int,
    via: str | None = None,
) -> None:
    """Append a memory neighbor if not already seen."""
    if not isinstance(m, dict) or not m.get("id"):
        return
    tid = str(m["id"])
    if tid in seen:
        return
    seen.add(tid)
    item: dict[str, Any] = {
        "id": tid,
        "content_preview": (m.get("content") or "")[:80],
        "category": m.get("category", ""),
        "edge_type": edge_map.get(tid, "relates"),
        "edge_direction": direction,
        "depth": depth_level,
    }
    if via:
        item["via"] = via
    items.append(item)


def _maybe_add_entity(
    items: list[dict],
    seen: set[str],
    e: dict,
    direction: str,
    edge_map: dict[str, str],
    depth_level: int,
    via: str | None = None,
) -> None:
    """Append an entity neighbor if not already seen."""
    if not isinstance(e, dict) or not e.get("id"):
        return
    tid = str(e["id"])
    if tid in seen:
        return
    seen.add(tid)
    item: dict[str, Any] = {
        "id": tid,
        "type": e.get("type", ""),
        "name": e.get("name", ""),
        "edge_type": edge_map.get(tid, "relates"),
        "edge_direction": direction,
        "depth": depth_level,
    }
    if via:
        item["via"] = via
    items.append(item)
