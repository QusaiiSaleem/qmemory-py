"""
Dashboard Route — Stats overview + recent activity.

Shows three stat cards (memory count, entity count, links count) and
a list of the 10 most recent memories. All data comes from SurrealDB
via parameterized queries.

Flow:
1. User visits /dashboard (must be logged in)
2. We query SurrealDB for three counts + recent memories
3. Render the dashboard template with the data
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

# Jinja2 templates — same pattern as auth.py and connect.py
# This file is at qmemory/app/routes/dashboard.py
# Templates are at qmemory/app/templates/
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# GET /dashboard — show stats overview + recent memories
# ---------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """
    Show the dashboard with memory stats and recent activity.

    Queries SurrealDB for:
    - Total active memory count
    - Total active entity count
    - Total relates (links) count
    - Last 10 memories (sorted by created_at DESC)
    """
    # Check if user is logged in — redirect to /login if not.
    # The SessionUserMiddleware has already set _user_db for us, so
    # every get_db() call in this handler automatically hits the
    # signed-in user's private database.
    user = get_session_user(request)
    if not user:
        logger.info("dashboard.redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    logger.info("dashboard.page_viewed user=%s", user.get("user_code"))

    # Default values in case queries fail
    memory_count = 0
    entity_count = 0
    links_count = 0
    recent_memories = []

    try:
        async with get_db() as db:
            # --- Count active memories ---
            mem_result = await query(
                db,
                "SELECT count() AS total FROM memory WHERE is_active = true GROUP ALL",
            )
            if mem_result and isinstance(mem_result, list) and len(mem_result) > 0:
                memory_count = mem_result[0].get("total", 0)
            elif mem_result and isinstance(mem_result, dict):
                memory_count = mem_result.get("total", 0)

            # --- Count active entities ---
            ent_result = await query(
                db,
                "SELECT count() AS total FROM entity WHERE is_active = true GROUP ALL",
            )
            if ent_result and isinstance(ent_result, list) and len(ent_result) > 0:
                entity_count = ent_result[0].get("total", 0)
            elif ent_result and isinstance(ent_result, dict):
                entity_count = ent_result.get("total", 0)

            # --- Count links (relates edges) ---
            links_result = await query(
                db,
                "SELECT count() AS total FROM relates GROUP ALL",
            )
            if links_result and isinstance(links_result, list) and len(links_result) > 0:
                links_count = links_result[0].get("total", 0)
            elif links_result and isinstance(links_result, dict):
                links_count = links_result.get("total", 0)

            # --- Fetch recent memories ---
            recent_result = await query(
                db,
                "SELECT * FROM memory WHERE is_active = true "
                "ORDER BY created_at DESC LIMIT 10",
            )
            if recent_result and isinstance(recent_result, list):
                recent_memories = recent_result

        logger.info(
            "dashboard.data_loaded memories=%d entities=%d links=%d recent=%d",
            memory_count,
            entity_count,
            links_count,
            len(recent_memories),
        )

    except Exception as exc:
        logger.error("dashboard.data_load_failed user=%s reason=%s", user.get("user_code"), exc)

    return templates.TemplateResponse(
        request,
        "pages/dashboard.html",
        context={
            "user": user,
            "memory_count": memory_count,
            "entity_count": entity_count,
            "links_count": links_count,
            "recent_memories": recent_memories,
        },
    )
