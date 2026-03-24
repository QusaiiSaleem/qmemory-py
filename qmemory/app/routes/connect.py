"""
Connect page — shows copy-paste MCP configs for AI tools.

After login, users visit /connect to see ready-to-use configuration
snippets for Claude Code, Claude.ai, and NanoBot. Each snippet includes
the user's API token pre-filled so they can just copy and paste.

Three tabs:
1. Claude Code — CLI command + manual ~/.claude.json config
2. Claude.ai — Settings → Integrations flow
3. NanoBot — ~/.nanobot/config.json config
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.routes.auth import get_session_user

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Router — gets included in the main FastAPI app
router = APIRouter()

# Jinja2 templates — same pattern as auth.py
# This file is at qmemory/app/routes/connect.py
# Templates are at qmemory/app/templates/
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# GET /connect — show MCP connection instructions
# ---------------------------------------------------------------------------


@router.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    """
    Show MCP connection instructions with the user's API token.

    If the user has a token stored in their session (from /tokens page),
    it will be pre-filled in the config snippets. If not, they'll see
    a prompt to generate one first.
    """
    # Check if user is logged in — redirect to /login if not
    user = get_session_user(request)
    if not user:
        logger.info("connect.redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    # Get the user's most recent API token from session (if any)
    # This is set by the /tokens page (Task 8) when a token is generated
    token = request.session.get("last_generated_token")

    logger.info(
        "connect.page_viewed user=%s has_token=%s",
        user.get("email"),
        bool(token),
    )

    return templates.TemplateResponse(
        request,
        "pages/connect.html",
        context={"user": user, "token": token},
    )
