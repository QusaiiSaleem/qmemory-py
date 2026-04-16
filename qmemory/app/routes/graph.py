"""
Graph Explorer Route — Interactive Cytoscape.js visualization of the memory graph.

Shows memories as colored nodes (by category), entities as distinct nodes (by type),
and relates edges as connections between them. Adapted from the AI-Lawyer explorer
pattern: Cytoscape.js + fcose layout + HTMX detail panel.

Endpoints:
    GET  /graph                           — Explorer page (HTML)
    GET  /api/graph                       — Full graph data (JSON)
    GET  /api/graph/expand/{table}/{id}   — Expand a node's neighbors (JSON)
    GET  /api/graph/node/{table}/{id}     — Node detail panel (HTML partial)
    GET  /api/graph/search                — Search results as mini-graph (JSON)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.routes.auth import get_session_user
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# ---------------------------------------------------------------------------
# Memory fields — never SELECT * (embedding is 10KB per record)
# ---------------------------------------------------------------------------

GRAPH_MEMORY_FIELDS = (
    "id, content, category, salience, scope, confidence, source_type, "
    "evidence_type, is_active, linked, recall_count, last_recalled, "
    "context_mood, source_person, section, created_at, updated_at"
)

GRAPH_ENTITY_FIELDS = (
    "id, name, type, aliases, external_id, external_url, "
    "external_source, external_channel, created_at, updated_at"
)


# ---------------------------------------------------------------------------
# Helpers — format nodes and edges for Cytoscape.js
# ---------------------------------------------------------------------------


def _make_memory_node(mem: dict) -> dict:
    """Format a memory record as a Cytoscape.js node."""
    node_id = str(mem.get("id", ""))
    content = str(mem.get("content", ""))
    # Label: first 60 chars of content
    label = content[:60] + ("..." if len(content) > 60 else "")
    return {
        "data": {
            "id": node_id,
            "label": label,
            "full_label": content,
            "type": "memory",
            "category": mem.get("category", "domain"),
            "salience": mem.get("salience", 0.5),
            "source_type": mem.get("source_type", "conversation"),
            "created_at": str(mem.get("created_at", "")),
        }
    }


def _make_entity_node(ent: dict) -> dict:
    """Format an entity record as a Cytoscape.js node."""
    node_id = str(ent.get("id", ""))
    name = str(ent.get("name", ""))
    label = name[:50] + ("..." if len(name) > 50 else "")
    return {
        "data": {
            "id": node_id,
            "label": label,
            "full_label": name,
            "type": "entity",
            "entity_type": ent.get("type", "concept"),
            "created_at": str(ent.get("created_at", "")),
        }
    }


def _make_edge(row: dict) -> dict:
    """Format a relates edge for Cytoscape.js."""
    return {
        "data": {
            "id": str(row.get("id", "")),
            "source": str(row.get("source", "")),
            "target": str(row.get("target", "")),
            "edge_type": row.get("type", "related"),
            "label": row.get("type", "related"),
            "confidence": row.get("confidence", 0.8),
        }
    }


# ---------------------------------------------------------------------------
# GET /graph — Explorer page
# ---------------------------------------------------------------------------


@router.get("/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    """Render the interactive graph explorer page."""
    user = get_session_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    # Fetch stats for the header badges
    memory_count = 0
    entity_count = 0
    links_count = 0

    try:
        async with get_db() as db:
            mem_r = await query(
                db,
                "SELECT count() AS total FROM memory WHERE is_active = true GROUP ALL",
            )
            if mem_r and isinstance(mem_r, list) and len(mem_r) > 0:
                memory_count = mem_r[0].get("total", 0)

            ent_r = await query(
                db,
                "SELECT count() AS total FROM entity WHERE is_active != false GROUP ALL",
            )
            if ent_r and isinstance(ent_r, list) and len(ent_r) > 0:
                entity_count = ent_r[0].get("total", 0)

            links_r = await query(
                db,
                "SELECT count() AS total FROM relates GROUP ALL",
            )
            if links_r and isinstance(links_r, list) and len(links_r) > 0:
                links_count = links_r[0].get("total", 0)
    except Exception as exc:
        logger.error("graph.stats_failed reason=%s", exc)

    return templates.TemplateResponse(
        request,
        "pages/graph.html",
        context={
            "user": user,
            "memory_count": memory_count,
            "entity_count": entity_count,
            "links_count": links_count,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/graph — Full graph data (JSON)
# ---------------------------------------------------------------------------


@router.get("/api/graph")
async def api_graph(
    request: Request,
    types: str = Query("", description="Comma-separated node types: memory,entity"),
    min_salience: float = Query(0.0, description="Minimum salience for memories"),
    category: str = Query("", description="Filter memories by category"),
    limit: int = Query(500, description="Max nodes to return"),
):
    """Return full graph data for Cytoscape.js."""
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    nodes = []
    edges = []
    node_ids: set[str] = set()

    type_list = [t.strip() for t in types.split(",") if t.strip()] if types else []
    show_memories = not type_list or "memory" in type_list
    show_entities = not type_list or "entity" in type_list

    try:
        async with get_db() as db:
            # --- Memories ---
            if show_memories:
                where_parts = ["is_active = true"]
                params: dict = {}

                if min_salience > 0:
                    where_parts.append("salience >= $min_sal")
                    params["min_sal"] = min_salience

                if category:
                    where_parts.append("category = $cat")
                    params["cat"] = category

                where_clause = " AND ".join(where_parts)
                mem_q = (
                    f"SELECT {GRAPH_MEMORY_FIELDS} FROM memory "
                    f"WHERE {where_clause} "
                    f"ORDER BY salience DESC LIMIT $lim"
                )
                params["lim"] = limit

                mem_result = await query(db, mem_q, params)
                if mem_result and isinstance(mem_result, list):
                    for m in mem_result:
                        node = _make_memory_node(m)
                        nodes.append(node)
                        node_ids.add(node["data"]["id"])

            # --- Entities ---
            if show_entities:
                ent_result = await query(
                    db,
                    f"SELECT {GRAPH_ENTITY_FIELDS} FROM entity LIMIT $lim",
                    {"lim": limit},
                )
                if ent_result and isinstance(ent_result, list):
                    for e in ent_result:
                        node = _make_entity_node(e)
                        nodes.append(node)
                        node_ids.add(node["data"]["id"])

            # --- Edges (only between loaded nodes) ---
            edge_result = await query(
                db,
                "SELECT id, <string> in AS source, <string> out AS target, "
                "type, confidence, created_at FROM relates",
            )
            if edge_result and isinstance(edge_result, list):
                for row in edge_result:
                    src = str(row.get("source", ""))
                    tgt = str(row.get("target", ""))
                    if src in node_ids and tgt in node_ids:
                        edges.append(_make_edge(row))

    except Exception as exc:
        logger.error("graph.api_graph_failed reason=%s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
    })


# ---------------------------------------------------------------------------
# GET /api/graph/expand/{table}/{record_id} — Expand a node
# ---------------------------------------------------------------------------


@router.get("/api/graph/expand/{table}/{record_id}")
async def api_expand(request: Request, table: str, record_id: str):
    """Expand a node — return its direct neighbors via relates edges."""
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    node_id = f"{table}:{record_id}"
    nodes = []
    edges = []
    seen: set[str] = set()

    try:
        async with get_db() as db:
            # Outgoing edges: this node → neighbors
            out_rows = await query(
                db,
                "SELECT id, <string> in AS source, <string> out AS target, "
                "type, confidence FROM relates "
                f"WHERE in = {node_id}",
            )
            if out_rows and isinstance(out_rows, list):
                for row in out_rows:
                    tgt = str(row.get("target", ""))
                    edges.append(_make_edge(row))
                    if tgt and tgt not in seen:
                        seen.add(tgt)

            # Incoming edges: neighbors → this node
            in_rows = await query(
                db,
                "SELECT id, <string> in AS source, <string> out AS target, "
                "type, confidence FROM relates "
                f"WHERE out = {node_id}",
            )
            if in_rows and isinstance(in_rows, list):
                for row in in_rows:
                    src = str(row.get("source", ""))
                    edges.append(_make_edge(row))
                    if src and src not in seen:
                        seen.add(src)

            # Fetch the actual node data for each neighbor
            for nid in seen:
                ntable = nid.split(":")[0] if ":" in nid else ""
                if ntable == "memory":
                    result = await query(
                        db,
                        f"SELECT {GRAPH_MEMORY_FIELDS} FROM {nid}",
                    )
                    if result and isinstance(result, list) and len(result) > 0:
                        nodes.append(_make_memory_node(result[0]))
                elif ntable == "entity":
                    result = await query(
                        db,
                        f"SELECT {GRAPH_ENTITY_FIELDS} FROM {nid}",
                    )
                    if result and isinstance(result, list) and len(result) > 0:
                        nodes.append(_make_entity_node(result[0]))

    except Exception as exc:
        logger.error("graph.expand_failed node=%s reason=%s", node_id, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "nodes": nodes,
        "edges": edges,
        "stats": {"node_count": len(nodes), "edge_count": len(edges)},
    })


# ---------------------------------------------------------------------------
# GET /api/graph/node/{table}/{record_id} — Node detail (HTML partial)
# ---------------------------------------------------------------------------


@router.get("/api/graph/node/{table}/{record_id}", response_class=HTMLResponse)
async def api_node_detail(request: Request, table: str, record_id: str):
    """Return HTML detail panel for a selected node."""
    user = get_session_user(request)
    if not user:
        return HTMLResponse('<div class="empty-state">Not authenticated</div>')

    node_id = f"{table}:{record_id}"
    node_data = None
    neighbors: list[dict] = []

    try:
        async with get_db() as db:
            if table == "memory":
                result = await query(
                    db,
                    f"SELECT {GRAPH_MEMORY_FIELDS} FROM {node_id}",
                )
                if result and isinstance(result, list) and len(result) > 0:
                    node_data = result[0]
                    node_data["_table"] = "memory"

            elif table == "entity":
                result = await query(
                    db,
                    f"SELECT {GRAPH_ENTITY_FIELDS} FROM {node_id}",
                )
                if result and isinstance(result, list) and len(result) > 0:
                    node_data = result[0]
                    node_data["_table"] = "entity"

            if not node_data:
                return HTMLResponse(
                    '<div class="empty-state">'
                    '<div class="empty-state-title">Node not found</div>'
                    '</div>'
                )

            # Fetch neighbors via relates edges
            out_rows = await query(
                db,
                "SELECT <string> out AS target, type, confidence "
                f"FROM relates WHERE in = {node_id} LIMIT 20",
            )
            in_rows = await query(
                db,
                "SELECT <string> in AS source, type, confidence "
                f"FROM relates WHERE out = {node_id} LIMIT 20",
            )

            if out_rows and isinstance(out_rows, list):
                for row in out_rows:
                    neighbors.append({
                        "id": str(row.get("target", "")),
                        "direction": "outgoing",
                        "type": row.get("type", "related"),
                    })
            if in_rows and isinstance(in_rows, list):
                for row in in_rows:
                    neighbors.append({
                        "id": str(row.get("source", "")),
                        "direction": "incoming",
                        "type": row.get("type", "related"),
                    })

    except Exception as exc:
        logger.error("graph.node_detail_failed node=%s reason=%s", node_id, exc)
        return HTMLResponse(
            f'<div class="empty-state"><div class="empty-state-title">Error loading node</div></div>'
        )

    return templates.TemplateResponse(
        request,
        "partials/graph_node_detail.html",
        context={
            "node": node_data,
            "node_id": node_id,
            "neighbors": neighbors,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/graph/search — Search results as mini-graph (JSON)
# ---------------------------------------------------------------------------


@router.get("/api/graph/search")
async def api_graph_search(
    request: Request,
    q: str = Query("", description="Search query"),
):
    """Render BM25 search results + their neighbors as a focused mini-graph."""
    user = get_session_user(request)
    if not user:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)

    if not q or len(q) < 2:
        return JSONResponse({
            "nodes": [], "edges": [],
            "stats": {"node_count": 0, "edge_count": 0, "query": q},
        })

    nodes = []
    edges = []
    seen_ids: set[str] = set()

    # Escape the query for BM25 inline literal (parameterized @@ is broken in v3)
    escaped_q = q.replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')

    try:
        async with get_db() as db:
            # --- BM25 search on memory content ---
            mem_result = await query(
                db,
                f'SELECT {GRAPH_MEMORY_FIELDS} FROM memory '
                f'WHERE content @@ "{escaped_q}" '
                f'AND is_active = true LIMIT 30',
            )
            if mem_result and isinstance(mem_result, list):
                for m in mem_result:
                    node = _make_memory_node(m)
                    nid = node["data"]["id"]
                    if nid not in seen_ids:
                        node["data"]["highlight"] = True
                        nodes.append(node)
                        seen_ids.add(nid)

            # --- BM25 search on entity names ---
            ent_result = await query(
                db,
                f'SELECT {GRAPH_ENTITY_FIELDS} FROM entity '
                f'WHERE name @@ "{escaped_q}" LIMIT 20',
            )
            if ent_result and isinstance(ent_result, list):
                for e in ent_result:
                    node = _make_entity_node(e)
                    nid = node["data"]["id"]
                    if nid not in seen_ids:
                        node["data"]["highlight"] = True
                        nodes.append(node)
                        seen_ids.add(nid)

            # --- Add 1-hop neighbors of matched nodes ---
            matched_ids = list(seen_ids)
            for mid in matched_ids:
                out_rows = await query(
                    db,
                    "SELECT id, <string> in AS source, <string> out AS target, "
                    "type, confidence FROM relates "
                    f"WHERE in = {mid}",
                )
                in_rows = await query(
                    db,
                    "SELECT id, <string> in AS source, <string> out AS target, "
                    "type, confidence FROM relates "
                    f"WHERE out = {mid}",
                )

                for rows in [out_rows, in_rows]:
                    if rows and isinstance(rows, list):
                        for row in rows:
                            edges.append(_make_edge(row))
                            # Add the neighbor node if not seen
                            neighbor_id = str(row.get("target", ""))
                            if neighbor_id == str(row.get("source", "")):
                                neighbor_id = str(row.get("target", ""))
                            for nid_key in ["source", "target"]:
                                nid = str(row.get(nid_key, ""))
                                if nid and nid not in seen_ids:
                                    seen_ids.add(nid)
                                    ntable = nid.split(":")[0]
                                    if ntable == "memory":
                                        nr = await query(
                                            db,
                                            f"SELECT {GRAPH_MEMORY_FIELDS} FROM {nid}",
                                        )
                                        if nr and isinstance(nr, list) and len(nr) > 0:
                                            nodes.append(_make_memory_node(nr[0]))
                                    elif ntable == "entity":
                                        nr = await query(
                                            db,
                                            f"SELECT {GRAPH_ENTITY_FIELDS} FROM {nid}",
                                        )
                                        if nr and isinstance(nr, list) and len(nr) > 0:
                                            nodes.append(_make_entity_node(nr[0]))

    except Exception as exc:
        logger.error("graph.search_failed q=%s reason=%s", q, exc)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "query": q,
        },
    })
