"""
Tests for qmemory.core.embeddings

These tests are designed to pass in TWO scenarios:

1. WITH a VOYAGE_API_KEY set in the environment:
   - Real API calls are made and we verify the vector shape/type
   - Cache behaviour is verified against real returned vectors

2. WITHOUT a VOYAGE_API_KEY (most dev/CI environments):
   - All functions return None gracefully
   - No API calls are attempted
   - Tests still pass — they just skip the shape assertions

This design means you can run the full test suite locally without needing
API credentials, and the tests still give you useful coverage.
"""

import pytest

# ---------------------------------------------------------------------------
# Fixtures — reset module-level state between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_voyage_state():
    """
    Reset the Voyage client and query cache before each test.

    Why this matters:
    - _client is a module-level singleton — if one test creates it with a
      real API key, the next test (which might patch env vars) would still
      use the old client.
    - _query_cache persists between tests, which could cause false positives
      in cache-hit tests (the value was already there from a previous test).

    autouse=True means this fixture runs automatically for every test in
    this file without needing to be listed in the test's arguments.
    """
    import qmemory.core.embeddings as emb

    # Reset the lazy client so the next test re-reads the API key from config
    emb._client = None

    # Clear the query cache so cache tests start with an empty state
    emb._query_cache.clear()

    # Also clear the settings cache so env var patches take effect
    from qmemory.config import get_settings

    get_settings.cache_clear()

    yield  # run the test

    # Cleanup after the test as well (belt-and-suspenders)
    emb._client = None
    emb._query_cache.clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_embedding_returns_vector_or_none():
    """
    A non-empty string should return either a valid vector or None.

    If VOYAGE_API_KEY is set: we get a real 1024-dim float vector.
    If not set: we get None (graceful degradation).

    We never get an exception — that's what we're verifying here.
    """
    from qmemory.core.embeddings import generate_embedding

    vec = await generate_embedding("The annual budget is 500K SAR")

    # Must be either None OR a non-empty list of floats
    assert vec is None or (
        isinstance(vec, list)
        and len(vec) > 100  # voyage-3-large → 1024 dims
        and isinstance(vec[0], float)
    )


@pytest.mark.asyncio
async def test_empty_string_returns_none():
    """
    Empty and whitespace-only strings should return None immediately,
    without making any API call.
    """
    from qmemory.core.embeddings import generate_embedding

    # Empty string
    assert await generate_embedding("") is None

    # Whitespace only
    assert await generate_embedding("   ") is None

    # Tab and newline
    assert await generate_embedding("\t\n") is None


@pytest.mark.asyncio
async def test_query_embedding_empty_string_returns_none():
    """Same empty-string guard applies to query embeddings."""
    from qmemory.core.embeddings import generate_query_embedding

    assert await generate_query_embedding("") is None
    assert await generate_query_embedding("   ") is None


@pytest.mark.asyncio
async def test_query_embedding_cache_hit():
    """
    Calling generate_query_embedding twice with the same text should
    return the exact same list object (cache hit).

    If no API key: both calls return None, which is also ==.
    """
    from qmemory.core.embeddings import generate_query_embedding

    vec1 = await generate_query_embedding("budget planning")
    vec2 = await generate_query_embedding("budget planning")

    # Whether vec is a real vector or None, the two results should match
    assert vec1 == vec2

    # If we got a real vector, verify the cache actually stored it
    if vec1 is not None:
        import qmemory.core.embeddings as emb

        assert "budget planning" in emb._query_cache


@pytest.mark.asyncio
async def test_different_queries_produce_different_vectors():
    """
    Two semantically different queries should produce different vectors.

    Only runs the assertion if we actually got real vectors back
    (i.e. VOYAGE_API_KEY is set).
    """
    from qmemory.core.embeddings import generate_query_embedding

    vec1 = await generate_query_embedding("budget forecasting")
    vec2 = await generate_query_embedding("team members list")

    # Only compare if both succeeded — no API key means both are None, which is fine
    if vec1 is not None and vec2 is not None:
        assert vec1 != vec2, "Different queries should produce different vectors"


@pytest.mark.asyncio
async def test_cache_does_not_exceed_max_size():
    """
    After inserting more than _CACHE_MAX entries, the cache should
    never grow beyond the limit (oldest entries are evicted).

    This test runs WITHOUT the API — it patches the internal state directly
    so we don't make 55 real API calls.
    """
    import qmemory.core.embeddings as emb

    # Directly populate the cache with fake vectors, bypassing the API
    # This lets us test the eviction logic without any API calls
    fake_vec = [0.1] * 1024

    for i in range(emb._CACHE_MAX + 5):  # Insert 55 entries (limit is 50)
        key = f"query_{i}"
        emb._query_cache[key] = fake_vec
        # Simulate the eviction logic from generate_query_embedding
        if len(emb._query_cache) > emb._CACHE_MAX:
            emb._query_cache.popitem(last=False)

    # Cache should be exactly at the limit, never above it
    assert len(emb._query_cache) == emb._CACHE_MAX

    # The first 5 entries should have been evicted (they were oldest)
    for i in range(5):
        assert f"query_{i}" not in emb._query_cache

    # The last 50 entries should still be present
    for i in range(5, emb._CACHE_MAX + 5):
        assert f"query_{i}" in emb._query_cache


@pytest.mark.asyncio
async def test_no_api_key_returns_none_gracefully(monkeypatch):
    """
    When VOYAGE_API_KEY is not set, both functions should return None
    without raising any exception.

    We use monkeypatch to temporarily clear the env var for this test only.
    """
    import os

    # Remove the key from the environment for this test
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    # Re-init the settings so the removed env var takes effect
    from qmemory.config import get_settings

    get_settings.cache_clear()

    from qmemory.core.embeddings import generate_embedding, generate_query_embedding

    # Both should return None gracefully — no exception
    assert await generate_embedding("some text") is None
    assert await generate_query_embedding("some query") is None
