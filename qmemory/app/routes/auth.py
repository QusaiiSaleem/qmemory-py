"""
Auth Routes — Login, Signup, Logout

Handles the user-facing authentication pages and form submissions.
Uses SurrealDB's built-in DEFINE ACCESS for signup/signin, which means
password hashing (Argon2) happens inside the database — not in Python.

Flow:
1. User visits /login or /signup (GET) → renders the HTML form
2. User submits the form (POST) → we call SurrealDB signup/signin
3. SurrealDB returns a JWT token → we store user info in the session cookie
4. User is redirected to /dashboard

Session storage:
- Uses Starlette's SessionMiddleware (signed cookie via itsdangerous)
- Session stores: user_id, email, name
- /logout clears the session and redirects to /login
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from surrealdb import AsyncSurreal

from qmemory.app.config import get_app_settings

# Note: provision_user_db is available in the feat/qmemory-cloud branch
# For now, user databases are provisioned on first MCP call
# from qmemory.db.provision import provision_user_db

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Router — all routes here get included in the main FastAPI app
router = APIRouter()

# Jinja2 templates — use absolute path based on this file's location
# This file is at qmemory/app/routes/auth.py
# Templates are at qmemory/app/templates/
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Helper: get user from session
# ---------------------------------------------------------------------------


def get_session_user(request: Request) -> dict | None:
    """
    Read the current user from the session cookie.

    Returns a dict with user_id, email, name if logged in, or None if not.
    This is used by templates to show/hide the navigation bar.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return {
        "user_id": user_id,
        "email": request.session.get("email", ""),
        "name": request.session.get("name", ""),
    }


# ---------------------------------------------------------------------------
# Helper: decode JWT to extract user info
# ---------------------------------------------------------------------------


def _decode_jwt_payload(token: str) -> dict:
    """
    Decode the payload section of a JWT token (without verifying the signature).

    SurrealDB returns a JWT after signup/signin. We decode it to extract user
    info (ID, email, name) for the session. We don't verify the signature here
    because we just received the token directly from SurrealDB — it's trusted.

    A JWT has 3 parts separated by dots: header.payload.signature
    The payload is base64url-encoded JSON.
    """
    import base64
    import json

    # Split the JWT into its 3 parts
    parts = token.split(".")
    if len(parts) != 3:
        logger.warning("auth.jwt_decode_failed reason=invalid_format")
        return {}

    # The payload is the second part (index 1)
    payload_b64 = parts[1]

    # Add padding if needed (base64url requires padding to be multiple of 4)
    padding_needed = 4 - len(payload_b64) % 4
    if padding_needed != 4:
        payload_b64 += "=" * padding_needed

    try:
        # Decode from base64url → bytes → JSON string → dict
        payload_bytes = base64.urlsafe_b64decode(payload_b64)
        payload = json.loads(payload_bytes)
        return payload
    except Exception as exc:
        logger.warning("auth.jwt_decode_failed reason=%s", exc)
        return {}


# ---------------------------------------------------------------------------
# Helper: extract user fields from JWT payload
# ---------------------------------------------------------------------------


def _extract_user_from_jwt(payload: dict) -> dict:
    """
    Extract user_id, email, and name from a SurrealDB JWT payload.

    SurrealDB v3 puts the user record ID in the "ID" field of the JWT.
    The email and name may also be in the payload, or we may need to
    query the database for them. For now, we extract what's available.
    """
    # SurrealDB typically puts the record ID in "ID" (e.g., "user:abc123")
    user_id = payload.get("ID", "")

    # Some SurrealDB versions also include the access fields
    # If not present, we'll have empty strings (filled after DB query)
    email = payload.get("email", "")
    name = payload.get("name", "")

    return {
        "user_id": str(user_id),
        "email": str(email),
        "name": str(name),
    }


# ---------------------------------------------------------------------------
# Helper: query user data from DB after auth
# ---------------------------------------------------------------------------


async def _fetch_user_data(user_id: str) -> dict:
    """
    Fetch user email and name from the database using their record ID.

    Called after signup/signin if the JWT payload doesn't contain
    the user's email/name (depends on SurrealDB version).
    """
    settings = get_app_settings()

    db = AsyncSurreal(settings.surreal_url)
    try:
        await db.connect()
        await db.signin({
            "username": settings.surreal_user,
            "password": settings.surreal_pass,
        })
        await db.use(settings.surreal_ns, settings.surreal_db)

        # Query the user record directly
        result = await db.query(
            "SELECT email, name FROM type::record($user_id)",
            {"user_id": user_id},
        )

        # result is typically [[{email: ..., name: ...}]]
        if result and isinstance(result, list):
            # Unwrap: result might be nested like [[{...}]] or [{...}]
            data = result[0] if result else {}
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                return {
                    "email": data.get("email", ""),
                    "name": data.get("name", ""),
                }
    except Exception as exc:
        logger.warning("auth.fetch_user_data_failed user_id=%s reason=%s", user_id, exc)
    finally:
        try:
            await db.close()
        except Exception:
            pass

    return {"email": "", "name": ""}


# ---------------------------------------------------------------------------
# GET /login — show login form
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, return_to: str | None = None):
    """
    Render the login page.

    If return_to is provided (e.g. from OAuth flow), it gets passed to the
    template as a hidden form field so we can redirect there after login.
    """
    logger.info("auth.login_page_viewed return_to=%s", return_to)

    # If already logged in, redirect to return_to or dashboard
    if get_session_user(request):
        redirect_url = return_to or "/dashboard"
        return RedirectResponse(url=redirect_url, status_code=302)

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        context={"user": None, "error": None, "return_to": return_to},
    )


# ---------------------------------------------------------------------------
# POST /login — authenticate user
# ---------------------------------------------------------------------------


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    """
    Process the login form submission.

    1. Read email + password from the form
    2. Call SurrealDB SIGNIN with the qmemory_user access method
    3. If successful, store user info in session and redirect to /dashboard
    4. If failed, re-render the login page with an error message
    """
    # Read the form data
    form = await request.form()
    email = form.get("email", "").strip()
    password = form.get("password", "")
    return_to = form.get("return_to", "").strip() or None

    logger.info("auth.login_attempt email=%s return_to=%s", email, return_to)

    # Basic validation
    if not email or not password:
        logger.warning("auth.login_failed reason=empty_fields email=%s", email)
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={"user": None, "error": "يرجى إدخال البريد الإلكتروني وكلمة المرور", "return_to": return_to},
        )

    settings = get_app_settings()

    # Try to authenticate with SurrealDB
    db = AsyncSurreal(settings.surreal_url)
    try:
        await db.connect()

        # Call SurrealDB's SIGNIN using the DEFINE ACCESS qmemory_user
        # This checks email + password (Argon2 compare) inside the DB
        token = await db.signin({
            "namespace": settings.surreal_ns,
            "database": settings.surreal_db,
            "access": "qmemory_user",
            "variables": {
                "email": email,
                "password": password,
            },
        })

        logger.info("auth.login_success email=%s", email)

        # Decode the JWT to get user info
        payload = _decode_jwt_payload(token)
        user_info = _extract_user_from_jwt(payload)

        # If JWT didn't have email/name, fetch from DB
        if not user_info["email"] and user_info["user_id"]:
            extra = await _fetch_user_data(user_info["user_id"])
            user_info["email"] = extra.get("email", email)
            user_info["name"] = extra.get("name", "")

        # Fall back to the submitted email if still empty
        if not user_info["email"]:
            user_info["email"] = email

        # Store user info in the session cookie
        request.session["user_id"] = user_info["user_id"]
        request.session["email"] = user_info["email"]
        request.session["name"] = user_info["name"]

        # Redirect to return_to (OAuth flow) or dashboard
        redirect_url = return_to or "/dashboard"
        logger.info("auth.login_redirect email=%s redirect=%s", email, redirect_url)
        return RedirectResponse(url=redirect_url, status_code=302)

    except Exception as exc:
        error_msg = str(exc)
        logger.warning("auth.login_failed email=%s reason=%s", email, error_msg)

        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={"user": None, "error": "البريد الإلكتروني أو كلمة المرور غير صحيحة", "return_to": return_to},
        )
    finally:
        try:
            await db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GET /signup — show signup form
# ---------------------------------------------------------------------------


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Render the zero-friction signup page (display name only)."""
    logger.info("auth.signup_page_viewed")
    return templates.TemplateResponse(
        request,
        "pages/signup.html",
        context={"user": None, "error": None, "user_code": None, "base_url": _public_base_url(request)},
    )


# ---------------------------------------------------------------------------
# POST /signup — zero-friction: generate user_code + provision DB
# ---------------------------------------------------------------------------


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request):
    """
    Zero-friction signup:
      1. Read display_name from the form
      2. Generate a unique user_code
      3. Provision the user's private database
      4. Insert the admin user row pointing at that database
      5. Render the signup page with the new personal URL (no session created)
    """
    form = await request.form()
    display_name = form.get("display_name", "").strip()

    logger.info("auth.signup_attempt display_name=%s", display_name)

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

    # Import lazily — these touch SurrealDB at call time, keeps import-time fast.
    from qmemory.app.user_code import generate_unique_user_code
    from qmemory.db.client import apply_admin_schema, get_admin_db, query
    from qmemory.db.provision import provision_user_db

    try:
        # Ensure admin schema exists (idempotent)
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


def _public_base_url(request: Request) -> str:
    """Return https://mem0.qusai.org or the scheme+host of the current request."""
    import os
    override = os.environ.get("QMEMORY_PUBLIC_URL")
    if override:
        return override.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


# ---------------------------------------------------------------------------
# POST /logout — clear session and redirect
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(request: Request):
    """Clear the session and redirect to the login page."""
    user_email = request.session.get("email", "unknown")
    logger.info("auth.logout email=%s", user_email)

    # Clear all session data
    request.session.clear()

    return RedirectResponse(url="/login", status_code=302)
