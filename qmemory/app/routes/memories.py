"""
Memory Routes — Browser, Search, and Detail pages.

Three endpoints:
1. GET /memories       — render the memory browser page (search + filters)
2. GET /memories/search — HTMX endpoint, returns search_results partial
3. GET /memories/{memory_id} — single memory detail with linked nodes

Search uses SurrealDB's built-in BM25 full-text search when a query is
provided, or filters by category when only a category filter is set.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.routes.auth import get_session_user
from qmemory.db.client import get_db, query

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Router — gets included in the main FastAPI app
router = APIRouter()

# Jinja2 templates — same pattern as other route files
# This file is at qmemory/app/routes/memories.py
# Templates are at qmemory/app/templates/
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Category badge colors — maps each memory category to a DaisyUI badge class
CATEGORY_COLORS = {
    "self": "badge-primary",
    "style": "badge-secondary",
    "preference": "badge-accent",
    "context": "badge-info",
    "decision": "badge-warning",
    "idea": "badge-success",
    "feedback": "badge-error",
    "domain": "badge-neutral",
}

# All 8 memory categories (used for filter buttons)
ALL_CATEGORIES = ["self", "style", "preference", "context", "decision", "idea", "feedback", "domain"]


# ---------------------------------------------------------------------------
# GET /memories — render the memory browser page
# ---------------------------------------------------------------------------


@router.get("/memories", response_class=HTMLResponse)
async def memories_page(request: Request):
    """
    Show the memory browser page with search input and category filters.

    The page loads empty results initially — the user types a query or
    clicks a category filter, and HTMX fetches results from /memories/search.
    """
    # Check if user is logged in — redirect to /login if not
    user = get_session_user(request)
    if not user:
        logger.info("memories.redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    logger.info("memories.page_viewed user=%s", user.get("email"))

    return templates.TemplateResponse(
        request,
        "pages/memories.html",
        context={
            "user": user,
            "categories": ALL_CATEGORIES,
            "category_colors": CATEGORY_COLORS,
        },
    )


# ---------------------------------------------------------------------------
# GET /memories/search — HTMX endpoint, returns search_results partial
# ---------------------------------------------------------------------------


@router.get("/memories/search", response_class=HTMLResponse)
async def memories_search(request: Request, q: str = "", category: str = ""):
    """
    Search memories and return an HTML partial for HTMX to swap in.

    Called by HTMX when the user types in the search box or clicks a
    category filter button. Returns the search_results partial.

    Args:
        q:        Free-text search query (uses BM25 full-text search).
        category: Filter to a specific category (one of the 8 categories).
    """
    # Check if user is logged in
    user = get_session_user(request)
    if not user:
        return HTMLResponse("", status_code=401)

    logger.info(
        "memories.search user=%s query=%s category=%s",
        user.get("email"),
        q,
        category,
    )

    results = []

    try:
        async with get_db() as db:
            if q:
                # --- BM25 full-text search ---
                # Uses @@ operator for full-text match (SurrealDB built-in)
                if category and category in ALL_CATEGORIES:
                    # Search with category filter
                    results_raw = await query(
                        db,
                        "SELECT * FROM memory "
                        "WHERE content @@ $query AND category = $category "
                        "AND is_active = true "
                        "ORDER BY salience DESC LIMIT 20",
                        {"query": q, "category": category},
                    )
                else:
                    # Search all categories
                    results_raw = await query(
                        db,
                        "SELECT * FROM memory "
                        "WHERE content @@ $query AND is_active = true "
                        "ORDER BY salience DESC LIMIT 20",
                        {"query": q},
                    )

                if results_raw and isinstance(results_raw, list):
                    results = results_raw

            elif category and category in ALL_CATEGORIES:
                # --- Category filter only (no text search) ---
                results_raw = await query(
                    db,
                    "SELECT * FROM memory "
                    "WHERE category = $category AND is_active = true "
                    "ORDER BY salience DESC LIMIT 20",
                    {"category": category},
                )
                if results_raw and isinstance(results_raw, list):
                    results = results_raw

            else:
                # --- No query, no filter — show recent memories ---
                results_raw = await query(
                    db,
                    "SELECT * FROM memory WHERE is_active = true "
                    "ORDER BY created_at DESC LIMIT 20",
                )
                if results_raw and isinstance(results_raw, list):
                    results = results_raw

        logger.info("memories.search_completed results=%d", len(results))

    except Exception as exc:
        logger.error(
            "memories.search_failed user=%s reason=%s",
            user.get("email"),
            exc,
        )

    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        context={
            "results": results,
            "result_count": len(results),
            "category_colors": CATEGORY_COLORS,
            "query": q,
        },
    )


# ---------------------------------------------------------------------------
# GET /memories/{memory_id} — single memory detail page
# ---------------------------------------------------------------------------


@router.get("/memories/{memory_id}", response_class=HTMLResponse)
async def memory_detail(request: Request, memory_id: str):
    """
    Show the full detail page for a single memory.

    Displays all metadata (category, salience, confidence, scope, etc.)
    and a list of linked nodes (memories/entities connected via relates edges).

    Args:
        memory_id: The ID part after "memory:" (e.g. "mem1710864000000abc").
                   The full record ID is "memory:{memory_id}".
    """
    # Check if user is logged in — redirect to /login if not
    user = get_session_user(request)
    if not user:
        logger.info("memories.detail_redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    logger.info(
        "memories.detail_viewed user=%s memory_id=%s",
        user.get("email"),
        memory_id,
    )

    memory = None
    linked_nodes = []

    try:
        async with get_db() as db:
            # --- Fetch the memory record ---
            mem_result = await query(
                db,
                "SELECT * FROM type::record('memory', $id)",
                {"id": memory_id},
            )
            if mem_result and isinstance(mem_result, list) and len(mem_result) > 0:
                memory = mem_result[0]
            elif mem_result and isinstance(mem_result, dict):
                memory = mem_result

            # --- Fetch linked nodes (outgoing relates edges) ---
            if memory:
                links_result = await query(
                    db,
                    "SELECT *, out.content AS target_content, out.id AS target_id, type "
                    "FROM relates WHERE in = type::record('memory', $id)",
                    {"id": memory_id},
                )
                if links_result and isinstance(links_result, list):
                    linked_nodes = links_result

                # --- Also fetch incoming relates edges ---
                incoming_result = await query(
                    db,
                    "SELECT *, in.content AS source_content, in.id AS source_id, type "
                    "FROM relates WHERE out = type::record('memory', $id)",
                    {"id": memory_id},
                )
                if incoming_result and isinstance(incoming_result, list):
                    linked_nodes.extend(incoming_result)

        logger.info(
            "memories.detail_loaded memory_id=%s found=%s linked=%d",
            memory_id,
            bool(memory),
            len(linked_nodes),
        )

    except Exception as exc:
        logger.error(
            "memories.detail_failed memory_id=%s reason=%s",
            memory_id,
            exc,
        )

    return templates.TemplateResponse(
        request,
        "pages/memory_detail.html",
        context={
            "user": user,
            "memory": memory,
            "memory_id": memory_id,
            "linked_nodes": linked_nodes,
            "category_colors": CATEGORY_COLORS,
        },
    )
