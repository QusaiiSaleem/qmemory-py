"""
Tests for qmemory.core.search

Tests the agent's primary memory retrieval function with the new
structured response format: pinned, entities, results, actions, meta.

All tests use the `db` fixture from conftest.py, which provides a fresh
SurrealDB connection in the "qmemory_test" namespace.

These tests require SurrealDB to be running locally (ws://localhost:8000).
"""

from qmemory.core.search import search_memories
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes


# ---------------------------------------------------------------------------
# Basic response format tests
# ---------------------------------------------------------------------------


async def test_search_returns_results(db):
    """After saving a memory, searching for it should return at least one result."""
    await save_memory(
        content="The quarterly budget is 500K",
        category="context",
        salience=0.8,
        db=db,
    )

    result = await search_memories(query_text="quarterly budget", db=db)

    assert isinstance(result, dict), "search_memories should return a dict"
    assert "results" in result
    assert "actions" in result
    assert "meta" in result
    assert len(result["results"]) >= 1, "Should find the saved memory"

    first = result["results"][0]
    assert "content" in first
    assert "id" in first


async def test_search_response_has_new_format(db):
    """Search response should have pinned, entities, results, actions, meta."""
    await save_memory(content="Test the new format", category="context", salience=0.5, db=db)

    result = await search_memories(query_text="new format", db=db)

    assert "pinned" in result, "Response missing 'pinned'"
    assert "results" in result, "Response missing 'results'"
    assert "entities" in result, "Response missing 'entities'"
    assert "actions" in result, "Response missing 'actions'"
    assert "meta" in result, "Response missing 'meta'"
    assert isinstance(result["meta"], dict)
    assert "returned" in result["meta"]
    assert "has_more" in result["meta"]


# ---------------------------------------------------------------------------
# Pinned separation tests
# ---------------------------------------------------------------------------


async def test_search_pinned_separation(db):
    """Memories with salience >= 0.9 should appear in pinned, not results."""
    await save_memory(content="Critical rule always applies", category="self", salience=1.0, db=db)
    await save_memory(content="Normal fact about testing", category="context", salience=0.5, db=db)

    result = await search_memories(query_text="testing", db=db)

    pinned_contents = [m["content"] for m in result["pinned"]]
    result_contents = [m["content"] for m in result["results"]]

    assert "Critical rule always applies" in pinned_contents
    assert "Critical rule always applies" not in result_contents


# ---------------------------------------------------------------------------
# Result fields tests
# ---------------------------------------------------------------------------


async def test_search_results_have_relevance_and_tier(db):
    """Each result should have relevance score and source_tier."""
    await save_memory(content="Relevance test fact", category="context", db=db)

    result = await search_memories(query_text="relevance test", db=db)

    for r in result["results"]:
        assert "relevance" in r, f"Result missing 'relevance': {r.get('id')}"
        assert "source_tier" in r, f"Result missing 'source_tier': {r.get('id')}"


async def test_search_results_have_neighbors(db):
    """Each result should have a neighbors dict (even if empty)."""
    await save_memory(content="Neighbors test fact", category="context", db=db)

    result = await search_memories(query_text="neighbors test", db=db)

    for r in result["results"]:
        assert "neighbors" in r, f"Result missing 'neighbors': {r.get('id')}"
        assert "count" in r["neighbors"]
        assert "items" in r["neighbors"]


# ---------------------------------------------------------------------------
# Connection enrichment tests
# ---------------------------------------------------------------------------


async def test_search_enrichment_with_connections(db):
    """After linking two memories, enrichment should show connections."""
    saved1 = await save_memory(content="Team uses Slack", category="context", salience=0.8, db=db)
    saved2 = await save_memory(content="Slack channel is engineering", category="context", salience=0.7, db=db)

    await link_nodes(
        from_id=saved1["memory_id"],
        to_id=saved2["memory_id"],
        relationship_type="has_detail",
        db=db,
    )

    result = await search_memories(query_text="Slack", db=db)

    assert "results" in result
    assert len(result["results"]) >= 1

    # At least one result should have neighbors
    connected = [r for r in result["results"] if r.get("neighbors", {}).get("count", 0) > 0]
    assert len(connected) >= 1, "At least one result should have connections"


# ---------------------------------------------------------------------------
# Empty/no query tests
# ---------------------------------------------------------------------------


async def test_search_empty_query(db):
    """Passing None as query_text should return recent memories."""
    await save_memory(content="Recent fact A", category="context", salience=0.5, db=db)
    await save_memory(content="Recent fact B", category="context", salience=0.4, db=db)

    result = await search_memories(query_text=None, db=db)

    assert isinstance(result, dict)
    assert "results" in result
    assert "actions" in result
    assert len(result["results"]) >= 1


async def test_search_empty_string_query(db):
    """An empty string query should also work without errors."""
    await save_memory(content="Fallback test memory", category="context", db=db)

    result = await search_memories(query_text="", db=db)

    assert isinstance(result, dict)
    assert "results" in result
    assert "actions" in result


# ---------------------------------------------------------------------------
# Category filter tests
# ---------------------------------------------------------------------------


async def test_search_with_category_filter(db):
    """When category is specified, results should include that category."""
    await save_memory(content="User likes dark mode", category="preference", salience=0.7, db=db)
    await save_memory(content="Project deadline is Q2", category="context", salience=0.8, db=db)

    result = await search_memories(category="preference", db=db)

    assert "results" in result
    # At least the preference memory should be found
    all_mems = result["results"] + result["pinned"]
    categories_returned = [r.get("category") for r in all_mems]
    assert "preference" in categories_returned


async def test_search_category_filter_excludes_others(db):
    """Category filter should only return that category."""
    await save_memory(content="Only preference here", category="preference", db=db)
    await save_memory(content="Only context here", category="context", salience=0.9, db=db)

    result = await search_memories(category="preference", limit=5, db=db)

    all_mems = result["results"] + result["pinned"]
    categories = {r.get("category") for r in all_mems}
    assert "context" not in categories, f"Should only have preference, got: {categories}"
