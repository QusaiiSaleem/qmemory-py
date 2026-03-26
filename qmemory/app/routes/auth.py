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
async def login_page(request: Request):
    """Render the login page."""
    logger.info("auth.login_page_viewed")

    # If already logged in, redirect to dashboard
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        context={"user": None, "error": None},
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

    logger.info("auth.login_attempt email=%s", email)

    # Basic validation
    if not email or not password:
        logger.warning("auth.login_failed reason=empty_fields email=%s", email)
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={"user": None, "error": "يرجى إدخال البريد الإلكتروني وكلمة المرور"},
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

        # Redirect to dashboard
        return RedirectResponse(url="/dashboard", status_code=302)

    except Exception as exc:
        error_msg = str(exc)
        logger.warning("auth.login_failed email=%s reason=%s", email, error_msg)

        return templates.TemplateResponse(
            request,
            "pages/login.html",
            context={"user": None, "error": "البريد الإلكتروني أو كلمة المرور غير صحيحة"},
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
    """Render the signup page."""
    logger.info("auth.signup_page_viewed")

    # If already logged in, redirect to dashboard
    if get_session_user(request):
        return RedirectResponse(url="/dashboard", status_code=302)

    return templates.TemplateResponse(
        request,
        "pages/signup.html",
        context={"user": None, "error": None},
    )


# ---------------------------------------------------------------------------
# POST /signup — create new user
# ---------------------------------------------------------------------------


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(request: Request):
    """
    Process the signup form submission.

    1. Read name, email, password from the form
    2. Call SurrealDB SIGNUP with the qmemory_user access method
    3. If successful, store user info in session and redirect to /dashboard
    4. If failed, re-render the signup page with an error message
    """
    # Read the form data
    form = await request.form()
    name = form.get("name", "").strip()
    email = form.get("email", "").strip()
    password = form.get("password", "")

    logger.info("auth.signup_attempt email=%s", email)

    # Basic validation
    if not name or not email or not password:
        logger.warning("auth.signup_failed reason=empty_fields email=%s", email)
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={"user": None, "error": "يرجى تعبئة جميع الحقول"},
        )

    if len(password) < 8:
        logger.warning("auth.signup_failed reason=short_password email=%s", email)
        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={"user": None, "error": "كلمة المرور يجب أن تكون 8 أحرف على الأقل"},
        )

    settings = get_app_settings()

    # Try to create the user with SurrealDB
    db = AsyncSurreal(settings.surreal_url)
    try:
        await db.connect()

        # Call SurrealDB's SIGNUP using the DEFINE ACCESS qmemory_user
        # This creates a new user record with Argon2-hashed password
        token = await db.signup({
            "namespace": settings.surreal_ns,
            "database": settings.surreal_db,
            "access": "qmemory_user",
            "variables": {
                "email": email,
                "password": password,
                "name": name,
            },
        })

        logger.info("auth.signup_success email=%s", email)

        # Decode the JWT to get user info
        payload = _decode_jwt_payload(token)
        user_info = _extract_user_from_jwt(payload)

        # If JWT didn't have email/name, use what the user submitted
        if not user_info["email"]:
            user_info["email"] = email
        if not user_info["name"]:
            user_info["name"] = name

        # Create the user's private database with full memory schema
        # TODO: Enable when provision.py is available
        # try:
        #     await provision_user_db(user_info["user_id"])
        #     logger.info("auth.user_db_provisioned user_id=%s", user_info["user_id"])
        # except Exception as exc:
        #     logger.error(
        #         "auth.user_db_provision_failed user_id=%s reason=%s",
        #         user_info["user_id"],
        #         exc,
        #     )
        #     # Don't fail signup — user can still use the web UI
        #     # Database can be provisioned later on first MCP call
        logger.info("auth.signup_complete user_id=%s (DB provisioning deferred)", user_info["user_id"])

        # Store user info in the session cookie
        request.session["user_id"] = user_info["user_id"]
        request.session["email"] = user_info["email"]
        request.session["name"] = user_info["name"]

        # Redirect to dashboard
        return RedirectResponse(url="/dashboard", status_code=302)

    except Exception as exc:
        error_msg = str(exc)
        logger.warning("auth.signup_failed email=%s reason=%s", email, error_msg)

        # Check for duplicate email error
        if "unique" in error_msg.lower() or "already" in error_msg.lower():
            error_display = "هذا البريد الإلكتروني مسجّل بالفعل"
        else:
            error_display = "حدث خطأ أثناء إنشاء الحساب. يرجى المحاولة مرة أخرى"

        return templates.TemplateResponse(
            request,
            "pages/signup.html",
            context={"user": None, "error": error_display},
        )
    finally:
        try:
            await db.close()
        except Exception:
            pass


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
