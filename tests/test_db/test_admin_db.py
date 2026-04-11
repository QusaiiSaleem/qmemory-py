"""Tests for the admin database connection helper."""

from __future__ import annotations

import pytest

from qmemory.db.client import apply_admin_schema, get_admin_db, query


@pytest.fixture
async def clean_admin():
    yield
    try:
        async with get_admin_db(database="admin_test") as conn:
            await query(conn, "REMOVE TABLE IF EXISTS user")
    except Exception:
        pass


async def test_get_admin_db_connects_to_admin_database(clean_admin):
    async with get_admin_db(database="admin_test") as conn:
        result = await query(conn, "RETURN 1")
    assert result == 1


async def test_apply_admin_schema_creates_user_table(clean_admin):
    async with get_admin_db(database="admin_test") as conn:
        await apply_admin_schema(conn)
        await query(
            conn,
            """CREATE user SET
                user_code = 'test-abc12',
                display_name = 'Test User',
                db_name = 'user_test-abc12'""",
        )
        result = await query(
            conn,
            "SELECT user_code, display_name, db_name, is_active FROM user WHERE user_code = 'test-abc12'",
        )
    assert result is not None
    assert len(result) == 1
    assert result[0]["user_code"] == "test-abc12"
    assert result[0]["is_active"] is True
