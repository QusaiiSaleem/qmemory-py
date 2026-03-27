"""
OAuth 2.0 Routes — Authorization Code Flow for Claude.ai

Implements the standard OAuth 2.0 Authorization Code Flow:
1. GET /oauth/authorize - Show consent page (or redirect to login)
2. POST /oauth/consent - Handle user's consent decision
3. POST /oauth/token - Exchange authorization code for access token

The access tokens generated are the same qm_ak_xxx format used for manual tokens,
so the existing auth middleware works unchanged.

See: https://datatracker.ietf.org/doc/html/rfc6749
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from qmemory.app.routes.auth import get_session_user
from qmemory.auth import generate_api_token, hash_token
from qmemory.db.client import get_db, query

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])  # No prefix - routes at /authorize, /token, /consent

# Jinja2 templates
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Authorization code expiration (10 minutes)
AUTHORIZATION_CODE_EXPIRES_MINUTES = 10

# Access token expiration (30 days, same as manual tokens)
ACCESS_TOKEN_EXPIRES_DAYS = 30

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_auth_code() -> str:
    """Generate a random 32-character authorization code."""
    return secrets.token_urlsafe(24)[:32]


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code verifier against challenge."""
    if method == "plain":
        return code_verifier == code_challenge
    elif method == "S256":
        # SHA-256 of verifier, base64url-encoded without padding
        digest = hashlib.sha256(code_verifier.encode()).digest()
        import base64

        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == code_challenge
    return False


async def _get_client_by_id(client_id: str) -> dict | None:
    """Fetch OAuth client by ID (works for both static and dynamically-registered clients)."""
    async with get_db() as db:
        result = await query(
            db,
            "SELECT * FROM oauth_client WHERE id = type::record('oauth_client', $client_id) LIMIT 1",
            {"client_id": client_id},
        )
        if result and isinstance(result, list) and len(result) > 0:
            logger.info("oauth.client_found client_id=%s name=%s", client_id, result[0].get("name"))
            return result[0]
    logger.warning("oauth.client_not_found client_id=%s", client_id)
    return None


async def _create_authorization_code(
    client_id: str,
    user_id: str,
    redirect_uri: str,
    scope: str,
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> str:
    """Create an authorization code and return it."""
    code = _generate_auth_code()
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=AUTHORIZATION_CODE_EXPIRES_MINUTES
    )

    async with get_db() as db:
        await query(
            db,
            "CREATE oauth_authorization_code CONTENT {"
            "  code: $code,"
            "  client_id: type::record('oauth_client', $client_id),"
            "  user_id: type::record('user', $user_id),"
            "  redirect_uri: $redirect_uri,"
            "  scope: $scope,"
            "  state: $state,"
            "  code_challenge: $code_challenge,"
            "  code_challenge_method: $code_challenge_method,"
            "  expires_at: $expires_at,"
            "  used: false"
            "}",
            {
                "code": code,
                "client_id": client_id,
                "user_id": user_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": code_challenge_method,
                "expires_at": expires_at.isoformat(),
            },
        )

    return code


async def _get_and_consume_auth_code(code: str) -> dict | None:
    """Get authorization code and mark it as used (prevents replay)."""
    async with get_db() as db:
        # Fetch the code
        result = await query(
            db,
            "SELECT * FROM oauth_authorization_code "
            "WHERE code = $code AND used = false AND expires_at > time::now() "
            "LIMIT 1",
            {"code": code},
        )

        if not result or not isinstance(result, list) or len(result) == 0:
            return None

        auth_code = result[0]

        # Mark as used
        await query(
            db,
            "UPDATE $id SET used = true",
            {"id": auth_code["id"]},
        )

        return auth_code


# ---------------------------------------------------------------------------
# GET /oauth/authorize - Start OAuth flow
# ---------------------------------------------------------------------------


@router.get("/authorize", response_class=HTMLResponse)
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: str = "read write",
    state: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
):
    """
    OAuth 2.0 Authorization Endpoint.

    If user is logged in, show consent page.
    If not logged in, redirect to login page with return URL.
    """
    # Validate response_type
    if response_type != "code":
        raise HTTPException(400, "Unsupported response_type. Only 'code' is supported.")

    # Validate client
    client = await _get_client_by_id(client_id)
    if not client:
        raise HTTPException(400, f"Unknown client_id: {client_id}")

    # Validate redirect_uri
    allowed_uris = client.get("redirect_uris", [])
    if redirect_uri not in allowed_uris:
        raise HTTPException(400, "Invalid redirect_uri for this client.")

    logger.info(
        "oauth.authorize_request client=%s redirect=%s scope=%s",
        client_id,
        redirect_uri,
        scope,
    )

    # Check if user is logged in
    user = get_session_user(request)
    if not user:
        # Build the full authorize URL so login can redirect back here
        # We URL-encode the return_to value to preserve the nested query params
        from urllib.parse import urlencode, quote

        authorize_params = {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
        }
        if state:
            authorize_params["state"] = state
        if code_challenge:
            authorize_params["code_challenge"] = code_challenge
        if code_challenge_method:
            authorize_params["code_challenge_method"] = code_challenge_method

        # Build: /authorize?response_type=code&client_id=...
        return_to = f"/authorize?{urlencode(authorize_params)}"

        # Redirect to login with return_to as a single encoded param
        login_url = f"/login?return_to={quote(return_to, safe='')}"

        return RedirectResponse(login_url, status_code=302)

    # User is logged in - show consent page
    return templates.TemplateResponse(
        request,
        "pages/oauth_consent.html",
        context={
            "user": user,
            "client": client,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
    )


# ---------------------------------------------------------------------------
# POST /oauth/consent - Handle consent decision
# ---------------------------------------------------------------------------


@router.post("/consent", response_class=HTMLResponse)
async def consent(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form("read write"),
    state: str | None = Form(None),
    code_challenge: str | None = Form(None),
    code_challenge_method: str | None = Form(None),
    allow: str | None = Form(None),  # Present if user clicked "Allow"
    deny: str | None = Form(None),  # Present if user clicked "Deny"
):
    """Handle user's consent decision."""
    user = get_session_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    client = await _get_client_by_id(client_id)
    if not client:
        raise HTTPException(400, "Unknown client")

    # User denied access
    if deny is not None:
        logger.info("oauth.consent_denied user=%s client=%s", user.get("email"), client_id)
        error_url = f"{redirect_uri}?error=access_denied"
        if state:
            error_url += f"&state={state}"
        return RedirectResponse(error_url, status_code=302)

    # User allowed - generate authorization code
    if allow is not None:
        user_id = user.get("user_id", "")
        if ":" in user_id:
            user_id = user_id.split(":")[-1]

        code = await _create_authorization_code(
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            scope=scope,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )

        logger.info(
            "oauth.consent_allowed user=%s client=%s code_created=true",
            user.get("email"),
            client_id,
        )

        # Redirect back to client with authorization code
        callback_url = f"{redirect_uri}?code={code}"
        if state:
            callback_url += f"&state={state}"

        return RedirectResponse(callback_url, status_code=302)

    # Neither allow nor deny - shouldn't happen
    raise HTTPException(400, "Invalid consent submission")


# ---------------------------------------------------------------------------
# POST /oauth/token - Exchange code for access token
# ---------------------------------------------------------------------------


@router.post("/token")
async def token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    client_id: str = Form(...),
    client_secret: str | None = Form(None),  # Optional — PKCE can replace it
    code_verifier: str | None = Form(None),
):
    """
    OAuth 2.0 Token Endpoint.

    Exchanges an authorization code for an access token.
    The access token is a standard qm_ak_xxx token that works
    with the existing auth middleware.

    Supports two auth modes:
    - client_secret: traditional confidential client auth
    - PKCE (code_verifier): public client auth (used by Claude.ai)
    """
    logger.info(
        "oauth.token_request grant_type=%s client_id=%s has_code=%s has_secret=%s has_verifier=%s redirect_uri=%s",
        grant_type, client_id, bool(code), bool(client_secret), bool(code_verifier), redirect_uri,
    )

    # Validate grant_type
    if grant_type != "authorization_code":
        logger.warning("oauth.token_failed reason=unsupported_grant_type got=%s", grant_type)
        return {"error": "unsupported_grant_type", "error_description": "Only 'authorization_code' is supported"}

    # Validate client exists
    client = await _get_client_by_id(client_id)
    if not client:
        logger.warning("oauth.token_failed reason=unknown_client client_id=%s", client_id)
        return {"error": "invalid_client", "error_description": "Unknown client"}

    # Verify client secret IF provided (optional when PKCE is used)
    if client_secret:
        secret_hash = hashlib.sha256(client_secret.encode()).hexdigest()
        if secret_hash != client.get("secret_hash"):
            logger.warning("oauth.token_failed reason=invalid_secret client_id=%s", client_id)
            return {"error": "invalid_client", "error_description": "Invalid client secret"}
    elif not code_verifier:
        # Must have at least one: client_secret or PKCE code_verifier
        logger.warning("oauth.token_failed reason=no_auth_method client_id=%s", client_id)
        return {"error": "invalid_request", "error_description": "Either client_secret or code_verifier required"}

    # Validate authorization code
    if not code:
        logger.warning("oauth.token_failed reason=missing_code")
        return {"error": "invalid_request", "error_description": "Missing authorization code"}

    auth_code = await _get_and_consume_auth_code(code)
    if not auth_code:
        logger.warning("oauth.token_failed reason=invalid_or_expired_code")
        return {"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}

    # Validate client_id matches
    auth_client_id = auth_code.get("client_id", "")
    if isinstance(auth_client_id, dict):
        auth_client_id = auth_client_id.get("id", "")
    if auth_client_id != client_id and not auth_client_id.endswith(f":{client_id}"):
        logger.warning("oauth.token_failed reason=client_mismatch expected=%s got=%s", auth_client_id, client_id)
        return {"error": "invalid_grant", "error_description": "Authorization code was issued to a different client"}

    # Validate redirect_uri matches
    if redirect_uri and redirect_uri != auth_code.get("redirect_uri"):
        logger.warning(
            "oauth.token_failed reason=redirect_mismatch expected=%s got=%s",
            auth_code.get("redirect_uri"), redirect_uri,
        )
        return {"error": "invalid_grant", "error_description": "Redirect URI mismatch"}

    # Validate PKCE if the auth code had a challenge
    if auth_code.get("code_challenge"):
        if not code_verifier:
            logger.warning("oauth.token_failed reason=missing_pkce_verifier")
            return {"error": "invalid_grant", "error_description": "PKCE code_verifier required"}
        if not _verify_pkce(
            code_verifier,
            auth_code["code_challenge"],
            auth_code.get("code_challenge_method", "S256"),
        ):
            logger.warning("oauth.token_failed reason=pkce_verification_failed")
            return {"error": "invalid_grant", "error_description": "PKCE verification failed"}

    # Get user ID from auth code
    user_id = auth_code.get("user_id", "")
    if isinstance(user_id, dict):
        user_id = user_id.get("id", "")
    if ":" in user_id:
        user_id = user_id.split(":")[-1]

    # Generate access token (same format as manual tokens)
    access_token = generate_api_token()

    # Calculate expiration
    expires_at = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRES_DAYS)

    # Store token in api_token table
    async with get_db() as db:
        await query(
            db,
            "CREATE api_token CONTENT {"
            "  user: type::record('user', $user_id),"
            "  token_hash: $token_hash,"
            "  prefix: $prefix,"
            "  name: $name,"
            "  expires_at: $expires_at"
            "}",
            {
                "user_id": user_id,
                "token_hash": hash_token(access_token),
                "prefix": access_token[:10],
                "name": f"OAuth: {client.get('name', client_id)}",
                "expires_at": expires_at.isoformat(),
            },
        )

    logger.info(
        "oauth.token_issued client=%s user_id=%s prefix=%s",
        client_id,
        user_id,
        access_token[:10],
    )

    # Return OAuth 2.0 token response
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": int(timedelta(days=ACCESS_TOKEN_EXPIRES_DAYS).total_seconds()),
        "scope": auth_code.get("scope", "read write"),
    }


# ---------------------------------------------------------------------------
# POST /register - Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------
# Claude.ai registers itself as a client before starting the OAuth flow.
# This is required by the MCP authorization spec (2025-03-26).
# ---------------------------------------------------------------------------


@router.post("/register")
async def register_client(request: Request):
    """
    Dynamic Client Registration (RFC 7591).

    Claude.ai sends this to register itself as an OAuth client.
    We generate a unique client_id and store the client in the database.
    No client_secret is needed — Claude.ai uses PKCE (public client).
    """
    body = await request.json()

    client_name = body.get("client_name", "Unknown Client")
    redirect_uris = body.get("redirect_uris", [])
    grant_types = body.get("grant_types", ["authorization_code"])
    token_endpoint_auth_method = body.get("token_endpoint_auth_method", "none")

    logger.info(
        "oauth.register_request client_name=%s redirect_uris=%s auth_method=%s",
        client_name, redirect_uris, token_endpoint_auth_method,
    )

    # Validate redirect_uris
    if not redirect_uris:
        return {"error": "invalid_client_metadata", "error_description": "redirect_uris required"}

    # Generate a unique client_id
    client_id = secrets.token_urlsafe(16)

    # Store in the database
    async with get_db() as db:
        await query(
            db,
            "CREATE type::record('oauth_client', $client_id) CONTENT {"
            "  name: $name,"
            "  redirect_uris: $redirect_uris,"
            "  grant_types: $grant_types,"
            "  token_endpoint_auth_method: $auth_method,"
            "  allowed_scopes: ['read', 'write'],"
            "  created_at: time::now()"
            "}",
            {
                "client_id": client_id,
                "name": client_name,
                "redirect_uris": redirect_uris,
                "grant_types": grant_types,
                "auth_method": token_endpoint_auth_method,
            },
        )

    logger.info("oauth.client_registered client_id=%s client_name=%s", client_id, client_name)

    # Return the registration response (RFC 7591 Section 3.2.1)
    return {
        "client_id": client_id,
        "client_name": client_name,
        "redirect_uris": redirect_uris,
        "grant_types": grant_types,
        "token_endpoint_auth_method": token_endpoint_auth_method,
    }
