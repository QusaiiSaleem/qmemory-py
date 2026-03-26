"""Tests for per-request database routing via _user_db context var."""

import pytest

from qmemory.db.client import _user_db, get_db, query


@pytest.fixture
async def user_db():
    """Create a temporary user database for testing."""
    async with get_db() as conn:
        await conn.query("DEFINE DATABASE IF NOT EXISTS user_test_routing")

    async with get_db(database="user_test_routing") as conn:
        await conn.query("""
            DEFINE TABLE IF NOT EXISTS memory SCHEMAFULL;
            DEFINE FIELD IF NOT EXISTS content ON memory TYPE string;
        """)
        yield "user_test_routing"

    async with get_db() as conn:
        await query(conn, "REMOVE DATABASE IF EXISTS user_test_routing")


async def test_user_db_context_var_routes_get_db(user_db):
    """When _user_db is set, get_db() connects to that database."""
    # Set the context var
    token = _user_db.set(user_db)
    try:
        async with get_db() as conn:
            await conn.query(
                "CREATE memory:routing_test SET content = 'routed'"
            )
            result = await query(conn, "SELECT content FROM memory:routing_test")
        assert result is not None
        assert result[0]["content"] == "routed"
    finally:
        _user_db.reset(token)


async def test_default_db_without_context_var():
    """Without _user_db set, get_db() uses the default from settings."""
    # _user_db should be unset (default)
    async with get_db() as conn:
        result = await query(conn, "RETURN 1")
    assert result == 1
