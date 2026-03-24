"""
Integration tests for the MCP auth middleware (qmemory/app/auth.py).

These tests require a running SurrealDB instance. They use the "qmemory_test"
namespace so they never touch production data.

Tests cover:
1. No Authorization header → returns None (local mode)
2. Invalid token format → raises 401
3. Valid token → returns user data
4. Expired token → raises 401
5. create_api_token_for_user → creates and validates round-trip
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from qmemory.auth import generate_api_token, hash_token, get_token_prefix
from qmemory.db.client import get_db, query


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def cloud_db():
    """
    Fresh test database with BOTH base schema and cloud schema applied.

    The cloud schema adds the user and api_token tables needed for auth tests.
    """
    # Step 1: Connect to the test namespace
    async with get_db(namespace="qmemory_test") as conn:
        # Step 2: Apply the base schema
        base_schema = Path(__file__).parent.parent / "qmemory" / "db" / "schema.surql"
        if base_schema.exists():
            await conn.query(base_schema.read_text(encoding="utf-8"))

        # Step 3: Apply the cloud schema (user + api_token tables)
        cloud_schema = Path(__file__).parent.parent / "qmemory" / "db" / "schema_cloud.surql"
        if cloud_schema.exists():
            await conn.query(cloud_schema.read_text(encoding="utf-8"))

        yield conn

    # Step 4: Cleanup — remove the test namespace entirely
    async with get_db() as cleanup_conn:
        await query(cleanup_conn, "REMOVE NAMESPACE IF EXISTS qmemory_test")


def _make_request(auth_header: str | None = None, client_host: str = "127.0.0.1") -> MagicMock:
    """Create a mock FastAPI Request with optional Authorization header."""
    request = MagicMock()

    # Build a headers dict — only include Authorization if provided
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header

    # Mock the .get() method on headers
    request.headers.get = lambda key, default="": headers.get(key, default)

    # Mock the client IP address
    request.client = MagicMock()
    request.client.host = client_host

    return request


async def _create_test_user(db, email: str = "test@example.com") -> str:
    """Create a test user directly in SurrealDB. Returns the user ID."""
    result = await query(
        db,
        "CREATE user CONTENT {"
        "  email: $email,"
        "  password: 'hashed_password_placeholder',"
        "  name: 'Test User',"
        "  created_at: time::now()"
        "}",
        {"email": email},
    )

    # The result is a list with one record — extract the ID
    if isinstance(result, list) and len(result) > 0:
        record_id = result[0].get("id", "")
        # Return just the id part after "user:"
        if isinstance(record_id, str) and ":" in record_id:
            return record_id.split(":")[1]
        return str(record_id)
    return ""


async def _create_test_token(
    db,
    user_id: str,
    expires_in_days: int = 30,
) -> str:
    """Create a test API token directly in SurrealDB. Returns the plaintext token."""
    token = generate_api_token()
    token_hash_value = hash_token(token)
    prefix = get_token_prefix(token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    await query(
        db,
        "CREATE api_token CONTENT {"
        "  user: type::record('user', $user_id),"
        "  token_hash: $hash,"
        "  prefix: $prefix,"
        "  name: 'Test Token',"
        "  expires_at: $expires_at"
        "}",
        {
            "user_id": user_id,
            "hash": token_hash_value,
            "prefix": prefix,
            "expires_at": expires_at.isoformat(),
        },
    )

    return token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_no_auth_header_returns_none():
    """When no Authorization header is present, resolve_api_token returns None."""
    from qmemory.app.auth import resolve_api_token

    request = _make_request(auth_header=None)
    result = await resolve_api_token(request)
    assert result is None


async def test_non_bearer_header_returns_none():
    """When Authorization header doesn't start with 'Bearer ', return None."""
    from qmemory.app.auth import resolve_api_token

    request = _make_request(auth_header="Basic dXNlcjpwYXNz")
    result = await resolve_api_token(request)
    assert result is None


async def test_invalid_token_format_raises_401():
    """A token that doesn't match qm_ak_ format should raise 401."""
    from qmemory.app.auth import resolve_api_token

    request = _make_request(auth_header="Bearer invalid_token_format")

    with pytest.raises(HTTPException) as exc_info:
        await resolve_api_token(request)

    assert exc_info.value.status_code == 401
    assert "Invalid token format" in str(exc_info.value.detail)


async def test_valid_token_returns_user_data(cloud_db):
    """A valid, non-expired token should return the user's data."""
    from qmemory.app.auth import resolve_api_token

    # Create a test user and token in the database
    user_id = await _create_test_user(cloud_db, email="alice@example.com")
    token = await _create_test_token(cloud_db, user_id, expires_in_days=30)

    # Build a request with the valid token
    request = _make_request(auth_header=f"Bearer {token}")

    # Validate it — should return user data
    # Pass namespace="qmemory_test" so the middleware looks in the test DB
    result = await resolve_api_token(request, namespace="qmemory_test")

    assert result is not None
    # The user_data should contain the email we created
    if isinstance(result, dict):
        assert result.get("email") == "alice@example.com"


async def test_nonexistent_token_raises_401():
    """A properly formatted but non-existent token should raise 401."""
    from qmemory.app.auth import resolve_api_token

    # Generate a valid-format token that was never stored in the DB
    fake_token = generate_api_token()
    request = _make_request(auth_header=f"Bearer {fake_token}")

    with pytest.raises(HTTPException) as exc_info:
        await resolve_api_token(request)

    assert exc_info.value.status_code == 401
    assert "expired or invalid" in str(exc_info.value.detail).lower()


async def test_expired_token_raises_401(cloud_db):
    """A token that has already expired should raise 401."""
    from qmemory.app.auth import resolve_api_token

    # Create a user and a token that expired yesterday
    user_id = await _create_test_user(cloud_db, email="expired@example.com")

    # Create an already-expired token (expires_in_days=-1 means yesterday)
    token = generate_api_token()
    token_hash_value = hash_token(token)
    prefix = get_token_prefix(token)
    expired_at = datetime.now(timezone.utc) - timedelta(days=1)

    await query(
        cloud_db,
        "CREATE api_token CONTENT {"
        "  user: type::record('user', $user_id),"
        "  token_hash: $hash,"
        "  prefix: $prefix,"
        "  name: 'Expired Token',"
        "  expires_at: $expires_at"
        "}",
        {
            "user_id": user_id,
            "hash": token_hash_value,
            "prefix": prefix,
            "expires_at": expired_at.isoformat(),
        },
    )

    # Try to validate the expired token — should fail
    request = _make_request(auth_header=f"Bearer {token}")

    with pytest.raises(HTTPException) as exc_info:
        await resolve_api_token(request, namespace="qmemory_test")

    assert exc_info.value.status_code == 401


async def test_create_api_token_for_user_roundtrip(cloud_db):
    """create_api_token_for_user should create a token that validates successfully."""
    from qmemory.app.auth import create_api_token_for_user, resolve_api_token

    # Create a test user first
    user_id = await _create_test_user(cloud_db, email="roundtrip@example.com")

    # Use the helper to create a token (pass namespace so it writes to test DB)
    token_result = await create_api_token_for_user(
        user_id=user_id,
        name="Test Key",
        days=7,
        namespace="qmemory_test",
    )

    # Verify the result structure
    assert "token" in token_result
    assert "prefix" in token_result
    assert "name" in token_result
    assert "expires_at" in token_result
    assert token_result["name"] == "Test Key"
    assert token_result["token"].startswith("qm_ak_")

    # Now validate the token — should work (pass namespace for test DB)
    request = _make_request(auth_header=f"Bearer {token_result['token']}")
    user_data = await resolve_api_token(request, namespace="qmemory_test")

    assert user_data is not None
    if isinstance(user_data, dict):
        assert user_data.get("email") == "roundtrip@example.com"
