"""
Shared test fixtures for Qmemory tests.

The key fixture here is `db` — it gives each test a fresh SurrealDB
connection in a SEPARATE test namespace ("qmemory_test") so we never
touch the real production data in "qmemory/main".

After each test, the entire test namespace is removed (cleaned up).
"""

import pytest

from qmemory.db.client import apply_schema, get_db, query


@pytest.fixture
async def db():
    """
    Fresh test database connection — cleaned up after each test.

    What this does step by step:
    1. Opens a connection to SurrealDB using the "qmemory_test" namespace
    2. Applies the full schema (tables, indexes, etc.) so tests can use them
    3. Yields the connection to the test function
    4. After the test finishes, removes the entire test namespace (cleanup)

    This means every test starts with a clean, empty database.
    """
    # Step 1+2: Connect to test namespace and set up schema
    async with get_db(namespace="qmemory_test") as conn:
        await apply_schema(conn)
        yield conn

    # Step 3: Cleanup — remove the entire test namespace
    # We need a fresh connection for this because the previous one
    # was closed by the context manager above
    async with get_db() as cleanup_conn:
        await query(cleanup_conn, "REMOVE NAMESPACE IF EXISTS qmemory_test")
