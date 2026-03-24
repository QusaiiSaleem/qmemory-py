"""
Tests for qmemory.db.client — the SurrealDB database client.

These tests require a running SurrealDB instance at ws://localhost:8000
with user=root, pass=root. They use a separate "qmemory_test" namespace
so they never touch production data.

Run with:
    cd /Users/qusaiabushanap/dev/Qmemory/py
    source .venv/bin/activate
    python -m pytest tests/test_db/test_client.py -v
"""

import pytest

from qmemory.db.client import (
    generate_id,
    get_db,
    is_healthy,
    normalize_ids,
    query,
)


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


async def test_connection():
    """Verify we can connect to SurrealDB and run a basic query."""
    async with get_db() as db:
        result = await query(db, "RETURN 1")
        assert result == 1


async def test_connection_with_namespace_override():
    """Verify the namespace override parameter works."""
    async with get_db(namespace="qmemory_test") as db:
        result = await query(db, "RETURN 'hello'")
        assert result == "hello"

    # Cleanup test namespace
    async with get_db() as db:
        await query(db, "REMOVE NAMESPACE IF EXISTS qmemory_test")


# ---------------------------------------------------------------------------
# Health check tests
# ---------------------------------------------------------------------------


async def test_health():
    """is_healthy() should return True when SurrealDB is running."""
    assert await is_healthy() is True


# ---------------------------------------------------------------------------
# ID generation tests
# ---------------------------------------------------------------------------


def test_generate_id_prefix():
    """Generated IDs should start with the given prefix."""
    id1 = generate_id("mem")
    assert id1.startswith("mem")


def test_generate_id_length():
    """Generated IDs should be reasonably long (prefix + 13-digit timestamp + 3 random chars)."""
    id1 = generate_id("mem")
    # "mem" (3) + timestamp_ms (~13 digits) + random (3) = ~19 chars
    assert len(id1) > 10


def test_generate_id_uniqueness():
    """Two IDs generated in sequence should be different."""
    id1 = generate_id("mem")
    id2 = generate_id("mem")
    assert id1 != id2


def test_generate_id_no_dashes():
    """Generated IDs should not contain dashes (SurrealDB safety)."""
    for _ in range(10):
        id1 = generate_id("test")
        assert "-" not in id1


# ---------------------------------------------------------------------------
# Parameterized query tests
# ---------------------------------------------------------------------------


async def test_query_parameterized(db):
    """Parameterized queries should correctly bind parameters."""
    # Insert a memory using parameterized query
    await query(
        db,
        """
        CREATE memory SET
            content = $content,
            category = $cat,
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            recall_count = 0,
            created_at = time::now(),
            updated_at = time::now()
        """,
        {"content": "test fact", "cat": "context"},
    )

    # Query it back using a parameterized WHERE clause
    results = await query(
        db,
        "SELECT * FROM memory WHERE category = $cat",
        {"cat": "context"},
    )

    assert results is not None
    assert len(results) >= 1
    assert results[0]["content"] == "test fact"
    assert results[0]["category"] == "context"


async def test_query_returns_normalized_ids(db):
    """Query results should have RecordID objects converted to strings."""
    # Create a memory
    await query(
        db,
        """
        CREATE memory SET
            content = 'id test',
            category = 'context',
            is_active = true,
            salience = 0.5,
            scope = 'global',
            confidence = 0.8,
            evidence_type = 'observed',
            recall_count = 0,
            created_at = time::now(),
            updated_at = time::now()
        """,
    )

    # Fetch it
    results = await query(db, "SELECT * FROM memory WHERE content = 'id test'")

    assert results is not None
    assert len(results) >= 1

    # The 'id' field should be a string like "memory:abc123", NOT a RecordID object
    record_id = results[0]["id"]
    assert isinstance(record_id, str), f"Expected string ID, got {type(record_id)}"
    assert record_id.startswith("memory:")


# ---------------------------------------------------------------------------
# Graceful degradation tests
# ---------------------------------------------------------------------------


async def test_query_with_bad_surql_returns_none(db):
    """A query with invalid SurrealQL should return None, not crash."""
    result = await query(db, "THIS IS NOT VALID SURQL !!!")
    assert result is None


# ---------------------------------------------------------------------------
# normalize_ids tests
# ---------------------------------------------------------------------------


def test_normalize_ids_plain_dict():
    """Dicts without RecordID values should pass through unchanged."""
    data = {"name": "test", "count": 42}
    assert normalize_ids(data) == {"name": "test", "count": 42}


def test_normalize_ids_plain_list():
    """Lists without RecordID values should pass through unchanged."""
    assert normalize_ids([1, 2, 3]) == [1, 2, 3]


def test_normalize_ids_plain_string():
    """Strings should pass through unchanged."""
    assert normalize_ids("hello") == "hello"


def test_normalize_ids_none():
    """None should pass through unchanged."""
    assert normalize_ids(None) is None


def test_normalize_ids_record_id():
    """RecordID objects should be converted to 'table:id' strings."""
    from surrealdb import RecordID

    rid = RecordID("memory", "abc123")
    result = normalize_ids(rid)
    assert result == "memory:abc123"


def test_normalize_ids_nested_dict():
    """RecordID objects nested inside dicts should be converted."""
    from surrealdb import RecordID

    data = {
        "id": RecordID("memory", "abc123"),
        "session": RecordID("session", "ses456"),
        "content": "test fact",
    }
    result = normalize_ids(data)
    assert result["id"] == "memory:abc123"
    assert result["session"] == "session:ses456"
    assert result["content"] == "test fact"


def test_normalize_ids_nested_list():
    """RecordID objects inside lists of dicts should be converted."""
    from surrealdb import RecordID

    data = [
        {"id": RecordID("memory", "a"), "content": "first"},
        {"id": RecordID("memory", "b"), "content": "second"},
    ]
    result = normalize_ids(data)
    assert result[0]["id"] == "memory:a"
    assert result[1]["id"] == "memory:b"


# ---------------------------------------------------------------------------
# Schema application tests
# ---------------------------------------------------------------------------


async def test_schema_creates_tables(db):
    """After apply_schema(), the expected tables should exist."""
    # The db fixture already applied the schema, so just check tables exist
    # by trying to query them
    for table in ["memory", "entity", "session", "message", "tool_call", "scratchpad", "metrics"]:
        result = await query(db, f"SELECT * FROM {table} LIMIT 1")
        # Should return an empty list (not None, which would mean error)
        assert result is not None, f"Table '{table}' should exist after schema application"


async def test_schema_is_idempotent(db):
    """Applying the schema twice should not cause errors."""
    from qmemory.db.client import apply_schema

    # The db fixture already applied it once. Apply again — should not crash.
    await apply_schema(db)

    # Verify tables still work
    result = await query(db, "SELECT * FROM memory LIMIT 1")
    assert result is not None
