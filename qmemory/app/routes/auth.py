"""
Auth Routes — user_code-based login/logout/signup for multi-user web UI.

Flow:
    /signup  (GET)   — display-name-only form
    /signup  (POST)  — generate user_code, provision user DB, insert admin
                       row, set session cookie, redirect to /dashboard
    /login   (GET)   — paste-your-personal-URL form
    /login   (POST)  — parse user_code from URL or accept raw code, verify
                       against admin DB, set session cookie, redirect to
                       /dashboard
    /logout  (POST)  — clear session, redirect to /login

Session data:
    request.session["user_code"]    — e.g. "abacus-k7m3p"
    request.session["display_name"] — e.g. "Qusai"
    request.session["db_name"]      — e.g. "user_abacus-k7m3p"

The SessionUserMiddleware (in qmemory/app/middleware/session_user.py) reads
session["db_name"] and sets the _user_db ContextVar for web UI requests, so
dashboard / memories / connect routes automatically query the right DB.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter()

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def get_session_user(request: Request) -> dict | None:
    """Return session user dict or None. Used by templates to show/hide nav."""
    user_code = request.session.get("user_code")
    if not user_code:
        return None
    return {
        "user_code": user_code,
        "display_name": request.session.get("display_name", ""),
        "db_name": request.session.get("db_name", ""),
    }


def _set_session(request: Request, user_code: str, display_name: str, db_name: str) -> None:
    request.session["user_code"] = user_code
    request.session["display_name"] = display_name
    request.session["db_name"] = db_name


def _public_base_url(request: Request) -> str:
    """Return `https://mem0.qusai.org` (or whichever host is serving)."""
    override = os.environ.get("QMEMORY_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


# ---------------------------------------------------------------------------
# Login — paste personal URL or user_code
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, return_to: str | None = None):
    if get_session_user(request):
        return RedirectResponse(url=return_to or "/dashboard", status_code=302)
    return templates.TemplateResponse(
        request,
        "pages/login.html",
        context={
            "user": None,
            "error": None,
            "return_to": return_to,
            "base_url": _public_base_url(request),
        },
    )


_USER_CODE_RE = re.compile(r"/mcp/u/([a-z0-9-]+)/?")


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    user_input: str = Form(...),
    return_to: str | None = Form(default=None),
):
    """Accept either a full personal URL or a bare user_code."""
    user_input = user_input.strip()

    # Try to pull user_code out of a pasted URL; fall back to the raw input.
    match = _USER_CODE_RE.search(user_input)
    user_code = match.group(1) if match else user_input

    logger.info("auth.login_attempt user_code=%s", user_code)

    if not user_code or len(user_code) < 4:
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={
                "user": None,
                "error": "Please paste your personal URL or user code.",
                "return_to": return_to,
                "base_url": _public_base_url(request),
            },
        )

    # Lazy import — the admin DB helpers hit SurrealDB at call time.
    from qmemory.db.client import apply_admin_schema, get_admin_db, query

    try:
        async with get_admin_db() as admin:
            await apply_admin_schema(admin)
            rows = await query(
                admin,
                """SELECT user_code, display_name, db_name, is_active
                   FROM user WHERE user_code = $code""",
                {"code": user_code},
            )
    except Exception as exc:
        logger.exception("auth.login_admin_error")
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={
                "user": None,
                "error": f"Server error: {type(exc).__name__}. Try again.",
                "return_to": return_to,
                "base_url": _public_base_url(request),
            },
        )

    if not rows or not rows[0].get("is_active", False):
        logger.info("auth.login_unknown user_code=%s", user_code)
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={
                "user": None,
                "error": "That code isn't in our records. Double-check the URL you saved at signup.",
                "return_to": return_to,
                "base_url": _public_base_url(request),
            },
        )

    row = rows[0]
    _set_session(
        request,
        user_code=row["user_code"],
        display_name=row["display_name"],
        db_name=row["db_name"],
    )
    logger.info("auth.login_success user_code=%s", user_code)
    return RedirectResponse(url=return_to or "/dashboard", status_code=303)


# ---------------------------------------------------------------------------
# Signup — zero-friction flow (also creates a session)
# ---------------------------------------------------------------------------


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pages/signup.html",
        context={
            "user": None,
            "error": None,
            "user_code": None,
            "base_url": _public_base_url(request),
        },
    )


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request, display_name: str = Form(...)):
    display_name = display_name.strip()
    if not display_name:
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={
                "user": None,
                "error": "Please enter a display name.",
                "user_code": None,
                "base_url": _public_base_url(request),
            },
        )
    if len(display_name) > 128:
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={
                "user": None,
                "error": "Display name too long (max 128 characters).",
                "user_code": None,
                "base_url": _public_base_url(request),
            },
        )

    from qmemory.app.user_code import generate_unique_user_code
    from qmemory.db.client import apply_admin_schema, get_admin_db, query
    from qmemory.db.provision import provision_user_db

    try:
        async with get_admin_db() as admin:
            await apply_admin_schema(admin)

        user_code = await generate_unique_user_code()
        db_name = await provision_user_db(user_code)

        async with get_admin_db() as admin:
            await query(
                admin,
                """CREATE user SET
                    user_code = $code,
                    display_name = $name,
                    db_name = $db_name,
                    is_active = true""",
                {"code": user_code, "name": display_name, "db_name": db_name},
            )

        logger.info("auth.signup_success user_code=%s db=%s", user_code, db_name)

        # Create the session so the user lands straight on their dashboard.
        _set_session(request, user_code=user_code, display_name=display_name, db_name=db_name)

        # Render the success state (URL + copy instructions) so the user
        # saves the URL before navigating away.
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={
                "user": None,
                "error": None,
                "user_code": user_code,
                "display_name": display_name,
                "base_url": _public_base_url(request),
            },
        )

    except Exception as exc:
        logger.exception("auth.signup_failed display_name=%s", display_name)
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={
                "user": None,
                "error": f"Something went wrong: {type(exc).__name__}. Please try again.",
                "user_code": None,
                "base_url": _public_base_url(request),
            },
        )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request):
    user_code = request.session.get("user_code", "unknown")
    logger.info("auth.logout user_code=%s", user_code)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
