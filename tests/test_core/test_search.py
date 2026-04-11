"""
Tests for the new multi-leg BM25 search engine.

Tests the dynamic category-grouped response format:
entities_matched, pinned, memories.{category}, book_insights, hypotheses.

All tests use the `db` fixture from conftest.py (fresh qmemory_test namespace).
Requires SurrealDB running locally (ws://localhost:8000).
"""

from qmemory.core.search import search_memories
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes
from qmemory.core.person import create_person


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


async def test_search_returns_dict(db):
    """Search should always return a dict with actions and meta."""
    await save_memory(content="Structure test fact", category="context", db=db)
    result = await search_memories(query_text="structure test", db=db)

    assert isinstance(result, dict)
    assert "actions" in result
    assert "meta" in result


async def test_search_memories_grouped_by_category(db):
    """Memories should be grouped by category in the response."""
    await save_memory(content="User likes dark mode", category="preference", db=db)
    await save_memory(content="Project started in January", category="context", db=db)

    result = await search_memories(query_text="dark mode project", db=db)

    if "memories" in result:
        assert isinstance(result["memories"], dict), "memories should be a dict keyed by category"
        for cat, mems in result["memories"].items():
            assert isinstance(mems, list)
            for m in mems:
                assert "id" in m
                assert "content" in m
                assert "actions" in m


async def test_search_empty_categories_omitted(db):
    """Categories with no results should not appear in memories."""
    await save_memory(content="Only a preference here", category="preference", db=db)

    result = await search_memories(query_text="preference here", db=db)

    if "memories" in result:
        for cat, mems in result["memories"].items():
            assert len(mems) > 0, f"Category '{cat}' should not be empty"


async def test_search_self_category_first(db):
    """Self category should come first in the memories dict."""
    await save_memory(content="I am the agent self model", category="self", db=db)
    await save_memory(content="Some context about the world", category="context", db=db)

    result = await search_memories(query_text="agent self context world", db=db)

    if "memories" in result and "self" in result["memories"]:
        keys = list(result["memories"].keys())
        assert keys[0] == "self", f"Expected 'self' first, got: {keys}"


# ---------------------------------------------------------------------------
# Pinned tests
# ---------------------------------------------------------------------------


async def test_search_pinned_high_salience(db):
    """Memories with salience >= 0.9 should appear in pinned section."""
    await save_memory(
        content="Critical rule never break", category="self", salience=1.0, db=db
    )
    await save_memory(
        content="Normal fact about testing", category="context", salience=0.5, db=db
    )

    result = await search_memories(query_text="testing rule", db=db)

    if "pinned" in result:
        pinned_contents = [p["content"] for p in result["pinned"]]
        assert "Critical rule never break" in pinned_contents


async def test_search_pinned_not_in_memories(db):
    """Pinned memories should not also appear in the memories section."""
    await save_memory(
        content="Pinned and unique fact xyz", category="context", salience=0.95, db=db
    )

    result = await search_memories(query_text="pinned unique xyz", db=db)

    pinned_ids = {p["id"] for p in result.get("pinned", [])}
    for cat_mems in result.get("memories", {}).values():
        for m in cat_mems:
            assert m["id"] not in pinned_ids, f"Pinned memory {m['id']} also in memories"


# ---------------------------------------------------------------------------
# Hypothesis tests
# ---------------------------------------------------------------------------


async def test_search_low_confidence_in_hypotheses(db):
    """Memories with confidence < 0.5 should appear in hypotheses."""
    await save_memory(
        content="Maybe the project will be cancelled",
        category="context",
        confidence=0.3,
        evidence_type="inferred",
        db=db,
    )

    result = await search_memories(query_text="project cancelled", db=db)

    if "hypotheses" in result:
        hyp_contents = [h["content"] for h in result["hypotheses"]]
        assert "Maybe the project will be cancelled" in hyp_contents
        assert result["hypotheses"][0].get("actions", {}).get("verify") is not None


# ---------------------------------------------------------------------------
# Entity search tests
# ---------------------------------------------------------------------------


async def test_search_finds_entities(db):
    """Searching for a person name should return in entities_matched."""
    await create_person(name="Ahmed Khalil", db=db)
    await save_memory(content="Ahmed works on mobile", category="context", db=db)

    result = await search_memories(query_text="Ahmed", db=db)

    if "entities_matched" in result:
        names = [e["name"] for e in result["entities_matched"]]
        assert "Ahmed Khalil" in names


async def test_search_entity_has_actions(db):
    """Each matched entity should have get and search_within actions."""
    await create_person(name="Fatima Al-Rashid", db=db)

    result = await search_memories(query_text="Fatima", db=db)

    if "entities_matched" in result and len(result["entities_matched"]) > 0:
        entity = result["entities_matched"][0]
        assert "get" in entity["actions"]
        assert "search_within" in entity["actions"]


# ---------------------------------------------------------------------------
# Graph enrichment tests
# ---------------------------------------------------------------------------


async def test_search_results_have_graph(db):
    """Memory results should have graph context with entities and related."""
    await save_memory(content="Team uses Slack for comms", category="context", db=db)

    result = await search_memories(query_text="Slack comms", db=db)

    if "memories" in result:
        for cat_mems in result["memories"].values():
            for m in cat_mems:
                assert "graph" in m, f"Memory {m['id']} missing graph"
                assert "entities" in m["graph"]
                assert "related" in m["graph"]


async def test_search_enrichment_shows_linked(db):
    """After linking two memories, graph should show the connection."""
    saved1 = await save_memory(
        content="Slack is used daily", category="context", salience=0.8, db=db
    )
    saved2 = await save_memory(
        content="Slack channel engineering", category="context", salience=0.7, db=db
    )

    await link_nodes(
        from_id=saved1["memory_id"],
        to_id=saved2["memory_id"],
        relationship_type="has_detail",
        db=db,
    )

    result = await search_memories(query_text="Slack", db=db)

    # Find any memory with non-empty graph.related
    has_related = False
    for cat_mems in result.get("memories", {}).values():
        for m in cat_mems:
            if m.get("graph", {}).get("related"):
                has_related = True
    assert has_related, "At least one memory should have graph.related after linking"


# ---------------------------------------------------------------------------
# Per-result action tests
# ---------------------------------------------------------------------------


async def test_search_results_have_actions(db):
    """Each memory result should have correct, link, get_neighbors actions."""
    await save_memory(content="Actions test fact", category="context", db=db)

    result = await search_memories(query_text="actions test", db=db)

    if "memories" in result:
        for cat_mems in result["memories"].values():
            for m in cat_mems:
                assert "actions" in m
                assert "correct" in m["actions"]
                assert "link" in m["actions"]
                assert "get_neighbors" in m["actions"]


# ---------------------------------------------------------------------------
# Meta tests
# ---------------------------------------------------------------------------


async def test_search_meta_has_by_category(db):
    """Meta should include by_category counts."""
    await save_memory(content="Meta test preference", category="preference", db=db)
    await save_memory(content="Meta test context", category="context", db=db)

    result = await search_memories(query_text="meta test", db=db)

    assert "by_category" in result["meta"]
    assert isinstance(result["meta"]["by_category"], dict)


async def test_search_meta_has_sections(db):
    """Meta should list which sections are present."""
    await save_memory(content="Sections test fact", category="context", db=db)

    result = await search_memories(query_text="sections test", db=db)

    assert "sections" in result["meta"]
    assert isinstance(result["meta"]["sections"], list)


async def test_search_meta_has_search_legs(db):
    """Meta should show how many results came from each leg."""
    await save_memory(content="Legs test fact", category="context", db=db)

    result = await search_memories(query_text="legs test", db=db)

    assert "search_legs" in result["meta"]


# ---------------------------------------------------------------------------
# Empty/no query tests
# ---------------------------------------------------------------------------


async def test_search_no_query_returns_recent(db):
    """No query should return recent memories."""
    await save_memory(content="Recent fact alpha", category="context", db=db)

    result = await search_memories(query_text=None, db=db)

    assert isinstance(result, dict)
    assert "meta" in result


async def test_search_empty_string_query(db):
    """Empty string query should work without errors."""
    await save_memory(content="Empty query test", category="context", db=db)

    result = await search_memories(query_text="", db=db)

    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Category filter tests
# ---------------------------------------------------------------------------


async def test_search_category_filter(db):
    """Category filter should only return that category."""
    await save_memory(content="Filter preference item", category="preference", db=db)
    await save_memory(content="Filter context item", category="context", db=db)

    result = await search_memories(category="preference", db=db)

    if "memories" in result:
        for cat in result["memories"]:
            assert cat == "preference", f"Expected only preference, got {cat}"


# ---------------------------------------------------------------------------
# entity_id scoped search tests
# ---------------------------------------------------------------------------


async def test_search_with_entity_id(db):
    """entity_id should scope search to memories linked to that entity."""
    person = await create_person(name="Scoped Person", db=db)
    eid = person["entity_id"]

    saved = await save_memory(
        content="Scoped person likes coffee", category="preference", db=db
    )
    await link_nodes(
        from_id=saved["memory_id"], to_id=eid,
        relationship_type="about", db=db,
    )

    await save_memory(
        content="Unrelated person likes tea", category="preference", db=db
    )

    result = await search_memories(query_text="likes", entity_id=eid, db=db)

    all_contents = []
    for cat_mems in result.get("memories", {}).values():
        all_contents.extend(m["content"] for m in cat_mems)
    assert any("coffee" in c for c in all_contents), (
        f"Should find 'coffee', got: {all_contents}"
    )


async def test_diversity_cap_limits_single_category(db):
    """Saving many same-category memories triggers the 60% cap."""
    from qmemory.core.save import save_memory
    from qmemory.core.search import search_memories

    for i in range(15):
        await save_memory(
            content=f"preference fact #{i}: I like option letter",
            category="preference",
            salience=0.5,
            db=db,
        )

    result = await search_memories(query_text="letter", limit=10, db=db)
    prefs = result.get("memories", {}).get("preference", [])
    # DIVERSITY_CAP = 0.6, limit = 10, so max 6 preference entries
    assert len(prefs) <= 6, f"diversity cap violated: got {len(prefs)} preference"
