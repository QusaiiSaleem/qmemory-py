"""
Token Management Routes — Generate, List, and Revoke API tokens.

Users visit /tokens to see their existing tokens, generate new ones,
and revoke tokens they no longer need. Each token is shown by its
prefix (e.g. "qm_ak_abcd") — the full token is only shown ONCE
immediately after generation.

Flow:
1. GET /tokens — list all tokens for the logged-in user
2. POST /tokens/generate — create a new token, flash it, redirect back
3. DELETE /tokens/{token_id} — revoke a token, return HTMX partial

Security:
- Full plaintext token is shown only ONCE (stored temporarily in session)
- Only the SHA-256 hash is stored in the database
- Tokens are scoped to the logged-in user (checked via session)
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.auth import create_api_token_for_user
from qmemory.app.routes.auth import get_session_user
from qmemory.db.client import get_db, query

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Router — gets included in the main FastAPI app
router = APIRouter()

# Jinja2 templates — same pattern as auth.py and connect.py
# This file is at qmemory/app/routes/tokens.py
# Templates are at qmemory/app/templates/
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# GET /tokens — list user's API tokens
# ---------------------------------------------------------------------------


@router.get("/tokens", response_class=HTMLResponse)
async def tokens_page(request: Request):
    """
    Show the token management page with a list of all tokens.

    Queries SurrealDB for tokens belonging to the logged-in user.
    If a token was just generated, it will be shown ONE TIME via
    the session flash mechanism.
    """
    # Check if user is logged in — redirect to /login if not
    user = get_session_user(request)
    if not user:
        logger.info("tokens.redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    user_id = user["user_id"]

    # Extract the user ID part (e.g. "user:abc123" -> "abc123")
    # The session stores the full record ID like "user:abc123"
    user_id_part = user_id.split(":")[-1] if ":" in user_id else user_id

    logger.info("tokens.page_viewed user=%s", user.get("email"))

    # Fetch all tokens for this user from the database
    tokens = []
    try:
        async with get_db() as db:
            result = await query(
                db,
                "SELECT id, prefix, name, created_at, expires_at, last_used "
                "FROM api_token "
                "WHERE user = type::record('user', $user_id) "
                "ORDER BY created_at DESC",
                {"user_id": user_id_part},
            )

            if result and isinstance(result, list):
                tokens = result

        logger.info(
            "tokens.listed user=%s count=%d",
            user.get("email"),
            len(tokens),
        )
    except Exception as exc:
        logger.warning(
            "tokens.list_failed user=%s reason=%s",
            user.get("email"),
            exc,
        )

    # Check if a token was just generated (session flash)
    # Pop it so it only shows once
    just_generated_token = request.session.pop("flash_token", None)

    return templates.TemplateResponse(
        request,
        "pages/tokens.html",
        context={
            "user": user,
            "tokens": tokens,
            "just_generated_token": just_generated_token,
        },
    )


# ---------------------------------------------------------------------------
# POST /tokens/generate — create a new API token
# ---------------------------------------------------------------------------


@router.post("/tokens/generate")
async def generate_token(request: Request):
    """
    Generate a new API token for the logged-in user.

    Creates the token via create_api_token_for_user(), stores the full
    plaintext token in the session for ONE-TIME display, and redirects
    back to /tokens.

    Also stores the token in session["last_generated_token"] so the
    /connect page can show it pre-filled in config snippets.
    """
    # Check if user is logged in
    user = get_session_user(request)
    if not user:
        logger.info("tokens.generate_redirect_to_login reason=not_authenticated")
        return RedirectResponse("/login", status_code=302)

    user_id = user["user_id"]

    # Extract the user ID part (e.g. "user:abc123" -> "abc123")
    user_id_part = user_id.split(":")[-1] if ":" in user_id else user_id

    # Read optional name from form (default to "Default")
    form = await request.form()
    token_name = form.get("name", "").strip() or "Default"

    logger.info(
        "tokens.generate_attempt user=%s name=%s",
        user.get("email"),
        token_name,
    )

    try:
        # Create the token — returns {token, prefix, name, expires_at}
        result = await create_api_token_for_user(
            user_id=user_id_part,
            name=token_name,
        )

        full_token = result["token"]

        # Store the full token in session for ONE-TIME display on /tokens
        request.session["flash_token"] = full_token

        # Also store it for the /connect page to use
        request.session["last_generated_token"] = full_token

        logger.info(
            "tokens.generated user=%s prefix=%s",
            user.get("email"),
            result["prefix"],
        )

    except Exception as exc:
        logger.error(
            "tokens.generate_failed user=%s reason=%s",
            user.get("email"),
            exc,
        )

    # Redirect back to /tokens (PRG pattern — Post/Redirect/Get)
    return RedirectResponse("/tokens", status_code=302)


# ---------------------------------------------------------------------------
# DELETE /tokens/{token_id} — revoke (delete) a token
# ---------------------------------------------------------------------------


@router.delete("/tokens/{token_id}", response_class=HTMLResponse)
async def revoke_token(request: Request, token_id: str):
    """
    Revoke (hard-delete) an API token.

    Called via HTMX DELETE — returns an empty string so HTMX removes
    the table row from the page (hx-swap="outerHTML").

    Args:
        token_id: The ID part of the api_token record (e.g. "abc123").
                  The full record ID is "api_token:abc123".
    """
    # Check if user is logged in
    user = get_session_user(request)
    if not user:
        logger.info("tokens.revoke_redirect_to_login reason=not_authenticated")
        return HTMLResponse("", status_code=401)

    user_id = user["user_id"]
    user_id_part = user_id.split(":")[-1] if ":" in user_id else user_id

    logger.info(
        "tokens.revoke_attempt user=%s token_id=%s",
        user.get("email"),
        token_id,
    )

    try:
        async with get_db() as db:
            # Delete the token — but only if it belongs to this user
            # This prevents users from deleting other users' tokens
            await query(
                db,
                "DELETE type::record('api_token', $token_id) "
                "WHERE user = type::record('user', $user_id)",
                {"token_id": token_id, "user_id": user_id_part},
            )

        logger.info(
            "tokens.revoked user=%s token_id=%s",
            user.get("email"),
            token_id,
        )

    except Exception as exc:
        logger.error(
            "tokens.revoke_failed user=%s token_id=%s reason=%s",
            user.get("email"),
            token_id,
            exc,
        )

    # Return empty string — HTMX will remove the row (hx-swap="outerHTML")
    return HTMLResponse("")
