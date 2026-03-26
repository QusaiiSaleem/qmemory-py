"""Tests for user database provisioning."""

import pytest

from qmemory.db.client import get_db, query
from qmemory.db.provision import provision_user_db


@pytest.fixture
async def cleanup_test_db():
    """Cleanup: remove the test user database after the test."""
    yield "test_provision_user"
    async with get_db() as conn:
        await query(conn, "REMOVE DATABASE IF EXISTS user_test_provision_user")


async def test_provision_creates_database_with_schema(cleanup_test_db):
    """provision_user_db creates a database and applies the memory schema."""
    user_id = cleanup_test_db

    await provision_user_db(user_id)

    # Connect to the new database and verify tables exist
    async with get_db(database=f"user_{user_id}") as conn:
        # Query a table to verify schema was applied
        result = await conn.query("SELECT count() FROM memory GROUP ALL LIMIT 1")
        # Empty result means the table exists (no error)
        assert result is not None


async def test_provision_is_idempotent(cleanup_test_db):
    """Running provision_user_db twice doesn't error."""
    user_id = cleanup_test_db
    await provision_user_db(user_id)
    await provision_user_db(user_id)  # Should not raise
