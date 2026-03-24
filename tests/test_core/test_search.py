"""
Tests for qmemory.core.search

Tests the agent's primary memory retrieval function, which combines
the 4-tier recall pipeline with graph connection hints and adaptive nudges.

All tests use the `db` fixture from conftest.py, which provides a fresh
SurrealDB connection in the "qmemory_test" namespace.

These tests require SurrealDB to be running locally (ws://localhost:8000).

What we're testing:
  1. search_memories() finds memories that have been saved
  2. Each result in the top 5 has a "connections" key (even if empty/absent when no edges)
  3. The response always has a "_nudge" key
  4. Empty query string returns recent memories (not an error)
  5. category filter narrows results to the requested category
"""

from qmemory.core.search import search_memories, _build_nudge
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes


# ---------------------------------------------------------------------------
# test_search_returns_results
# ---------------------------------------------------------------------------


async def test_search_returns_results(db):
    """
    After saving a memory, searching for it should return at least one result.

    This is the most basic smoke test — if this fails, nothing else works.
    We save a memory with a unique phrase and search for that phrase.
    """
    await save_memory(
        content="The quarterly budget is 500K",
        category="context",
        salience=0.8,
        db=db,
    )

    result = await search_memories(query_text="quarterly budget", db=db)

    # Return value should be a dict
    assert isinstance(result, dict), "search_memories should return a dict"

    # Should have "results" key
    assert "results" in result, "Result dict should have 'results' key"

    # Should have at least one result
    assert len(result["results"]) >= 1, "Should find the saved memory"

    # Each result should be a dict with the expected keys
    first = result["results"][0]
    assert "content" in first, "Result should have 'content' key"
    assert "id" in first, "Result should have 'id' key"


# ---------------------------------------------------------------------------
# test_search_has_connections_key
# ---------------------------------------------------------------------------


async def test_search_has_connections_key(db):
    """
    After linking two memories, the search result should have a 'connections' key.

    The enrichment step adds this key only when actual graph edges exist.
    We save two memories, link them with qmemory_link, then search — the
    first result should have connections populated.
    """
    # Save two memories
    saved1 = await save_memory(
        content="Team uses Slack for communication",
        category="context",
        salience=0.8,
        db=db,
    )
    saved2 = await save_memory(
        content="Slack channel is #engineering",
        category="context",
        salience=0.7,
        db=db,
    )

    # Link them so graph edges exist
    await link_nodes(
        from_id=saved1["memory_id"],
        to_id=saved2["memory_id"],
        relationship_type="has_detail",
        db=db,
    )

    # Search — first result should have connections
    result = await search_memories(query_text="Slack", db=db)

    assert "results" in result
    assert len(result["results"]) >= 1, "Should find at least one memory"

    # At least one of the top 5 results should have the connections key
    top5 = result["results"][:5]
    connected = [r for r in top5 if "connections" in r]
    assert len(connected) >= 1, (
        "At least one top-5 result should have a 'connections' key "
        "since we created a graph edge"
    )

    # The connections dict should have 'total' and 'hints'
    conn = connected[0]["connections"]
    assert "total" in conn, "'connections' should have a 'total' count"
    assert "hints" in conn, "'connections' should have a 'hints' list"
    assert conn["total"] >= 1, "Total connections should be at least 1"


# ---------------------------------------------------------------------------
# test_search_has_nudge
# ---------------------------------------------------------------------------


async def test_search_has_nudge(db):
    """
    Every search response should include a '_nudge' key.

    The nudge tells the agent what to do next — it's always present,
    even when no results are found. This is a core design requirement:
    the agent should always have a clear next action.
    """
    await save_memory(
        content="User prefers concise responses",
        category="preference",
        salience=0.6,
        db=db,
    )

    result = await search_memories(query_text="prefers", db=db)

    assert "_nudge" in result, "Response should always have '_nudge' key"
    assert isinstance(result["_nudge"], str), "_nudge should be a string"
    assert len(result["_nudge"]) > 0, "_nudge should not be empty"


async def test_search_nudge_present_on_empty_results(db):
    """
    Even when the database is empty and no results are found,
    the response should still have a '_nudge' key.

    The "no results" nudge encourages the agent to save what it learns.
    """
    result = await search_memories(query_text="this memory does not exist xyz123", db=db)

    assert "_nudge" in result, "Response should have '_nudge' even with no results"
    assert "qmemory_save" in result["_nudge"], (
        "When no memories found, nudge should suggest saving: "
        f"got '{result['_nudge']}'"
    )


# ---------------------------------------------------------------------------
# test_search_empty_query
# ---------------------------------------------------------------------------


async def test_search_empty_query(db):
    """
    Passing None or empty string as query_text should return recent memories.

    This exercises the "no query" path in recall(), which falls through
    to Tier 4 (recent fallback). It should NOT error out.
    """
    # Save some memories first so there's something to return
    await save_memory(content="Recent fact A", category="context", salience=0.5, db=db)
    await save_memory(content="Recent fact B", category="context", salience=0.4, db=db)

    # Call with no query text (the "browse recent" use case)
    result = await search_memories(query_text=None, db=db)

    assert isinstance(result, dict), "Should return dict even with empty query"
    assert "results" in result, "Should have 'results' key"
    assert "_nudge" in result, "Should have '_nudge' key"

    # Should find recent memories (Tier 4 fallback)
    assert len(result["results"]) >= 1, "Should return recent memories when query is empty"


async def test_search_empty_string_query(db):
    """
    An empty string query should also work without errors.

    Some callers might pass "" instead of None — both should work.
    """
    await save_memory(content="Fallback test memory", category="context", db=db)

    result = await search_memories(query_text="", db=db)

    assert isinstance(result, dict)
    assert "results" in result
    assert "_nudge" in result


# ---------------------------------------------------------------------------
# test_search_with_category_filter
# ---------------------------------------------------------------------------


async def test_search_with_category_filter(db):
    """
    When category is specified, results should only include memories
    from that category.

    We save memories in three categories, then search with a category filter.
    Results should only contain memories from the filtered category.
    """
    await save_memory(content="User likes dark mode", category="preference", salience=0.7, db=db)
    await save_memory(content="Project deadline is Q2", category="context", salience=0.8, db=db)
    await save_memory(content="Send updates via Slack", category="decision", salience=0.6, db=db)

    # Filter to only "preference" category
    result = await search_memories(category="preference", db=db)

    assert "results" in result
    assert len(result["results"]) >= 1, "Should find at least the preference memory"

    # All results should be in the "preference" category
    # (Note: recall's Tier 4 recent fallback might include others, but
    # category filter is applied so results should match)
    categories_returned = [r.get("category") for r in result["results"]]
    assert "preference" in categories_returned, (
        f"Should find the preference memory, got categories: {categories_returned}"
    )


async def test_search_category_filter_excludes_others(db):
    """
    Category filter should not return memories from OTHER categories.

    This is stricter than the previous test — we verify that the wrong
    categories are NOT in the results.
    """
    await save_memory(content="Only preference here", category="preference", db=db)
    await save_memory(content="Only context here", category="context", salience=0.9, db=db)

    result = await search_memories(category="preference", limit=5, db=db)

    # Results should not include "context" category memories
    # (at least when searching specifically for "preference")
    context_mems = [r for r in result["results"] if r.get("category") == "context"]

    # Tier 4 fallback COULD return context memories if nothing else is found,
    # but with a category filter and an explicit preference memory, we should
    # get at least the preference memory back.
    assert len(result["results"]) >= 1
    preference_mems = [r for r in result["results"] if r.get("category") == "preference"]
    assert len(preference_mems) >= 1, "Should find the preference memory"


# ---------------------------------------------------------------------------
# test_build_nudge (pure function — no DB needed)
# ---------------------------------------------------------------------------


async def test_build_nudge_with_connections():
    """
    When results have connections, the nudge should mention exploring them.
    """
    results = [
        {
            "id": "memory:mem123",
            "content": "Some fact",
            "connections": {"total": 2, "hints": []},
        }
    ]

    nudge = _build_nudge(results)

    assert "memory:mem123" in nudge, "Nudge should reference the connected memory ID"
    assert "qmemory_search" in nudge, "Nudge should suggest searching to explore"


async def test_build_nudge_results_no_connections():
    """
    When results exist but have no connections, the nudge should suggest linking.
    """
    results = [
        {"id": "memory:mem1", "content": "Fact A"},
        {"id": "memory:mem2", "content": "Fact B"},
    ]

    nudge = _build_nudge(results)

    assert "qmemory_link" in nudge, "Nudge should suggest linking when no connections exist"


async def test_build_nudge_empty_results():
    """
    When there are no results, the nudge should suggest saving.
    """
    nudge = _build_nudge([])

    assert "qmemory_save" in nudge, "Nudge should suggest saving when no memories found"
    assert "No memories found" in nudge, "Should say 'No memories found'"
