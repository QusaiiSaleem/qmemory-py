"""
Connect page — shows the user's personal MCP URL + copy-paste configs.

After signup/login, the user visits /connect to see their personal URL
and ready-to-paste configuration snippets for Claude Code and Claude.ai.
There are no tokens — the URL is the credential.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.routes.auth import get_session_user

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _public_base_url(request: Request) -> str:
    override = os.environ.get("QMEMORY_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


@router.get("/connect", response_class=HTMLResponse)
async def connect_page(request: Request):
    """Show the user's personal URL + copy-paste snippets for Claude Code & Claude.ai."""
    user = get_session_user(request)
    if not user:
        logger.info("connect.redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    user_code = user["user_code"]
    base_url = _public_base_url(request)
    personal_url = f"{base_url}/mcp/u/{user_code}/"

    logger.info("connect.page_viewed user_code=%s", user_code)

    return templates.TemplateResponse(
        request,
        "pages/connect.html",
        context={
            "user": user,
            "user_code": user_code,
            "personal_url": personal_url,
            "base_url": base_url,
        },
    )
