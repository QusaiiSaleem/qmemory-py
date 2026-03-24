"""
MCP Auth Middleware — validates API tokens from the Authorization header.

This module handles the "who is calling?" question for every MCP request.
It extracts the API token from the HTTP header, validates it against the
database, and returns the user's information.

Flow:
1. Extract token from "Authorization: Bearer qm_ak_..." header
2. Verify format (starts with qm_ak_, correct length)
3. Hash the token (SHA-256) — we never store or compare plaintext tokens
4. Look up the hash in the api_token table
5. Check expiry (tokens have a limited lifetime)
6. Update last_used timestamp (fire-and-forget — non-critical)
7. Return the user dict

Access modes:
- No Authorization header → return None (allows unauthenticated local access)
- Invalid or expired token → raise HTTPException 401
- Valid token → return user data dict
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from qmemory.auth import (
    generate_api_token,
    get_token_prefix,
    hash_token,
    verify_token_format,
)
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token validation (called on every authenticated request)
# ---------------------------------------------------------------------------


async def resolve_api_token(
    request: Request,
    *,
    namespace: str | None = None,
    database: str | None = None,
) -> dict | None:
    """
    Extract and validate an API token from the request's Authorization header.

    This is the main auth function — called for every MCP request to determine
    who the caller is.

    Args:
        request:   The incoming FastAPI request.
        namespace: Override the SurrealDB namespace (used by tests).
        database:  Override the SurrealDB database (used by tests).

    Returns:
        dict: The user's data (email, name, etc.) if a valid token was provided.
        None: If no Authorization header was present (local/unauthenticated mode).

    Raises:
        HTTPException(401): If a token was provided but is invalid or expired.
    """
    # Step 1: Extract the Authorization header
    auth_header = request.headers.get("Authorization", "")

    # No header at all → unauthenticated access (local mode is OK)
    if not auth_header.startswith("Bearer "):
        return None

    # Strip the "Bearer " prefix to get the raw token
    token = auth_header[7:]

    # Start timing for the log message at the end
    start = time.monotonic()

    # Get the client IP for logging (never log the token itself!)
    client_host = request.client.host if request.client else "unknown"

    # Step 2: Quick format check before hitting the database
    if not verify_token_format(token):
        logger.warning(
            "auth.invalid_format client=%s",
            client_host,
        )
        raise HTTPException(status_code=401, detail="Invalid token format")

    # Step 3: Hash the token — we compare hashes, never plaintext
    token_hash_value = hash_token(token)

    # Step 4 + 5: Look up the hash in the database and check expiry
    async with get_db(namespace=namespace, database=database) as db:
        result = await query(
            db,
            "SELECT *, user.* AS user_data FROM api_token "
            "WHERE token_hash = $hash AND expires_at > time::now() "
            "LIMIT 1",
            {"hash": token_hash_value},
        )

        # query() returns None on DB error, or an empty list if no match
        if not result or (isinstance(result, list) and len(result) == 0):
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "auth.token_failed reason=expired_or_not_found elapsed_ms=%.1f client=%s",
                elapsed,
                client_host,
            )
            raise HTTPException(
                status_code=401,
                detail="Token expired or invalid",
            )

        # Grab the first (and only) matching record
        token_record = result[0] if isinstance(result, list) else result

        # Step 6: Fire-and-forget — update the last_used timestamp
        # This is non-critical, so we catch and ignore any errors
        try:
            token_id = token_record.get("id")
            if token_id:
                # token_id is already normalized to "api_token:xyz" string
                await query(
                    db,
                    "UPDATE $token_id SET last_used = time::now()",
                    {"token_id": token_id},
                )
        except Exception:
            # Don't fail auth just because we couldn't update last_used
            logger.debug("auth.last_used_update_failed (non-critical)")

        # Step 7: Extract user data and return it
        elapsed = (time.monotonic() - start) * 1000
        user_data = token_record.get("user_data", {})
        user_email = user_data.get("email", "unknown") if isinstance(user_data, dict) else "unknown"

        logger.info(
            "auth.token_validated user=%s elapsed_ms=%.1f",
            user_email,
            elapsed,
        )

        return user_data


# ---------------------------------------------------------------------------
# Token creation (used by token management page)
# ---------------------------------------------------------------------------


async def create_api_token_for_user(
    user_id: str,
    name: str = "Default",
    days: int = 30,
    *,
    namespace: str | None = None,
    database: str | None = None,
) -> dict:
    """
    Create a new API token for a user. Returns the full token ONCE.

    IMPORTANT: The plaintext token is returned in this response and NEVER
    stored. Only the SHA-256 hash is saved in the database. If the user
    loses the token, they need to generate a new one.

    Args:
        user_id:   The SurrealDB user record ID (just the id part, e.g. "abc123").
        name:      A human-readable name for this token (e.g. "Claude Desktop").
        days:      How many days until the token expires (default: 30).
        namespace: Override the SurrealDB namespace (used by tests).
        database:  Override the SurrealDB database (used by tests).

    Returns:
        dict with keys:
          - token:      The full plaintext token (show to user ONCE)
          - prefix:     Display prefix like "qm_ak_abcd" (for the token list UI)
          - name:       The token name
          - expires_at: ISO format expiry datetime
    """
    # Generate a fresh random token
    token = generate_api_token()

    # Hash it for storage — the plaintext is never saved
    token_hash_value = hash_token(token)

    # Get the display prefix (first 10 chars) for the token management UI
    prefix = get_token_prefix(token)

    # Calculate when this token should expire
    expires_at = datetime.now(timezone.utc) + timedelta(days=days)

    # Store the hashed token in the database
    async with get_db(namespace=namespace, database=database) as db:
        await query(
            db,
            "CREATE api_token CONTENT {"
            "  user: type::record('user', $user_id),"
            "  token_hash: $hash,"
            "  prefix: $prefix,"
            "  name: $name,"
            "  expires_at: $expires_at"
            "}",
            {
                "user_id": user_id,
                "hash": token_hash_value,
                "prefix": prefix,
                "name": name,
                "expires_at": expires_at.isoformat(),
            },
        )

    logger.info(
        "auth.token_created user=%s prefix=%s expires=%s",
        user_id,
        prefix,
        expires_at.date(),
    )

    # Return the full token — this is the ONLY time it's visible
    return {
        "token": token,
        "prefix": prefix,
        "name": name,
        "expires_at": expires_at.isoformat(),
    }
