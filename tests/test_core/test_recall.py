"""
Tests for qmemory.core.recall

Tests the 4-tier recall pipeline and the assemble_context() function.
All tests use the `db` fixture from conftest.py, which provides a fresh
SurrealDB connection in the "qmemory_test" namespace.

These tests require SurrealDB to be running locally (ws://localhost:8000).

What we're testing:
  1. recall() finds memories saved to the database
  2. recall() respects salience ordering (most important first)
  3. recall() filters out soft-deleted (inactive) memories
  4. recall() filters by scope when provided
  5. recall() filters by category when provided
  6. assemble_context() produces a formatted string with self-model + memories
  7. parse_session_key() correctly extracts channel, topic, group, scope
"""

from qmemory.core.recall import (
    assemble_context,
    parse_session_key,
    recall,
    _format_age,
    _deduplicate_by_id,
    _estimate_tokens,
    _fit_to_token_budget,
)
from qmemory.core.save import save_memory
from qmemory.core.correct import correct_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# parse_session_key tests (pure function — no DB needed)
# ---------------------------------------------------------------------------


async def test_parse_session_key_telegram_topic():
    """
    A full Telegram topic session key should extract all components.

    Input:  "telegram:group:-123:topic:7"
    Expect: channel=telegram, group_id=-123, topic_id=7, scope=topic:7
    """
    result = parse_session_key("telegram:group:-123:topic:7")
    assert result["channel"] == "telegram"
    assert result["group_id"] == "-123"
    assert result["topic_id"] == "7"
    assert result["scope"] == "topic:7"
    assert result["chat_type"] == "group"


async def test_parse_session_key_with_agent_prefix():
    """
    OpenClaw session keys have an agent prefix: agent:main:telegram:...
    The channel should be extracted from the third position.
    """
    result = parse_session_key("agent:main:telegram:group:456:topic:12")
    assert result["channel"] == "telegram"
    assert result["topic_id"] == "12"
    assert result["scope"] == "topic:12"


async def test_parse_session_key_simple():
    """
    A simple test session key without known segments.
    Should return defaults.
    """
    result = parse_session_key("test:session:1")
    assert result["scope"] == "global"
    assert result["chat_type"] == "direct"


async def test_parse_session_key_empty():
    """
    An empty session key should return all defaults.
    """
    result = parse_session_key("")
    assert result["channel"] is None
    assert result["topic_id"] is None
    assert result["scope"] == "global"


async def test_parse_session_key_whatsapp():
    """
    WhatsApp channel should be recognized.
    """
    result = parse_session_key("agent:main:whatsapp:group:123")
    assert result["channel"] == "whatsapp"
    assert result["group_id"] == "123"
    assert result["chat_type"] == "group"


async def test_parse_session_key_subagent():
    """
    Subagent sessions should be detected from the key.
    """
    result = parse_session_key("agent:main:telegram:subagent:task1")
    assert result["chat_type"] == "subagent"


async def test_parse_session_key_cron():
    """
    Cron sessions should be detected from the key.
    """
    result = parse_session_key("agent:main:cron:heartbeat")
    assert result["chat_type"] == "cron"


# ---------------------------------------------------------------------------
# recall() tests — require SurrealDB
# ---------------------------------------------------------------------------


async def test_recall_finds_recent(db):
    """
    After saving two memories, recall() should find at least one of them.

    This tests Tier 4 (recent fallback) — when no query is provided,
    recall falls through to the recent tier and returns the latest memories.
    """
    await save_memory(content="Budget is 500K", category="context", salience=0.9, db=db)
    await save_memory(content="Team has 5 members", category="context", salience=0.7, db=db)

    results = await recall(scope="global", limit=5, db=db)

    # Should find at least one memory
    assert len(results) >= 1
    # Results should be dicts with the expected keys
    assert "content" in results[0]
    assert "salience" in results[0]


async def test_recall_respects_salience_order(db):
    """
    Recall should return memories sorted by salience DESC.

    The memory with salience=0.95 should come before salience=0.1,
    regardless of the order they were saved.
    """
    await save_memory(content="Low priority fact", category="context", salience=0.1, db=db)
    await save_memory(content="High priority fact", category="context", salience=0.95, db=db)

    results = await recall(scope="global", limit=5, db=db)

    # Should have both memories
    assert len(results) >= 2

    # First result should have higher salience than the last
    assert results[0]["salience"] >= results[-1]["salience"]

    # Specifically, the high-priority fact should be first
    assert results[0]["content"] == "High priority fact"


async def test_recall_filters_inactive(db):
    """
    Soft-deleted memories (is_active=false) should NOT appear in recall results.

    We save a memory, then delete it (soft-delete), and verify it's gone
    from recall results.
    """
    saved = await save_memory(content="Deleted fact", category="context", db=db)
    await correct_memory(memory_id=saved["memory_id"], action="delete", db=db)

    results = await recall(scope="global", limit=10, db=db)

    # The deleted memory should NOT appear
    assert all(r.get("content") != "Deleted fact" for r in results)


async def test_recall_with_categories(db):
    """
    When categories are specified, recall should include memories
    matching those categories (via Tier 3).
    """
    await save_memory(content="I prefer direct communication", category="self", salience=0.9, db=db)
    await save_memory(content="Budget is 500K", category="context", salience=0.8, db=db)
    await save_memory(content="Use formal tone", category="style", salience=0.7, db=db)

    # Recall only "self" and "style" categories
    results = await recall(categories=["self", "style"], limit=10, db=db)

    # Should find at least the self and style memories
    contents = [r.get("content") for r in results]
    # At least one of the filtered categories should be present
    # (Tier 4 recent fallback may also add the context memory)
    assert any("direct communication" in c for c in contents if c) or \
           any("formal tone" in c for c in contents if c)


async def test_recall_empty_database(db):
    """
    Recall on an empty database should return an empty list, not crash.
    """
    results = await recall(scope="global", limit=5, db=db)
    assert isinstance(results, list)
    assert len(results) == 0


async def test_recall_respects_limit(db):
    """
    Recall should not return more results than the requested limit.
    """
    # Save 10 memories
    for i in range(10):
        await save_memory(
            content=f"Fact number {i}",
            category="context",
            salience=0.5 + (i * 0.05),
            db=db,
        )

    # Request only 3
    results = await recall(limit=3, db=db)
    assert len(results) <= 3


async def test_recall_with_scope_filter(db):
    """
    When scope is provided, recall should prefer memories matching that scope.
    Global memories should also be included (they're always visible).
    """
    await save_memory(content="Global fact", category="context", scope="global", salience=0.8, db=db)
    await save_memory(content="Topic 7 fact", category="context", scope="topic:7", salience=0.8, db=db)
    await save_memory(content="Topic 9 fact", category="context", scope="topic:9", salience=0.8, db=db)

    # Recall for topic:7 scope
    results = await recall(scope="topic:7", limit=10, db=db)

    # Should find global + topic:7, but NOT topic:9
    contents = [r.get("content") for r in results]
    assert "Global fact" in contents
    assert "Topic 7 fact" in contents
    assert "Topic 9 fact" not in contents


# ---------------------------------------------------------------------------
# assemble_context() tests — require SurrealDB
# ---------------------------------------------------------------------------


async def test_assemble_context_basic(db):
    """assemble_context() should return a dict with self_model, memories, actions, meta."""
    await save_memory(content="I prefer direct communication", category="self", salience=0.9, db=db)
    await save_memory(content="Budget is 500K", category="context", salience=0.8, db=db)

    context = await assemble_context(session_key="test:session:1", db=db)

    assert isinstance(context, dict)
    assert "self_model" in context
    assert "memories" in context
    assert "actions" in context
    assert "meta" in context

    # Self-model should contain the self memory
    self_contents = [m["content"] for m in context["self_model"]]
    assert "I prefer direct communication" in self_contents

    # Context memories should be in the grouped dict
    assert "context" in context["memories"]


async def test_assemble_context_session_header(db):
    """The meta should reflect the session scope."""
    await save_memory(content="Some fact", category="context", salience=0.5, db=db)

    context = await assemble_context(session_key="telegram:group:123:topic:7", db=db)

    assert "meta" in context
    assert context["meta"]["session_scope"] == "topic:7"


async def test_assemble_context_empty_db(db):
    """Even with no memories, assemble_context should return a valid dict."""
    context = await assemble_context(session_key="test:session:1", db=db)

    assert isinstance(context, dict)
    assert "self_model" in context
    assert "memories" in context
    assert "meta" in context
    assert context["meta"]["total_memories"] == 0


async def test_assemble_context_self_memories_first(db):
    """Self-model should be in its own top-level key, separate from memories."""
    await save_memory(content="Agent self-knowledge fact", category="self", salience=0.9, db=db)
    await save_memory(content="Some context fact", category="context", salience=0.8, db=db)

    context = await assemble_context(session_key="test:session:1", db=db)

    # Self-model is separate from memories
    assert len(context["self_model"]) >= 1
    assert "self" not in context["memories"]  # self excluded from grouped memories


async def test_assemble_context_multiple_categories(db):
    """Memories from different categories should be grouped by category."""
    await save_memory(content="User likes dark mode", category="preference", salience=0.7, db=db)
    await save_memory(content="Project deadline is March", category="context", salience=0.8, db=db)
    await save_memory(content="Use Slack for updates", category="decision", salience=0.6, db=db)

    context = await assemble_context(session_key="test:session:1", db=db)

    assert "preference" in context["memories"]
    assert "context" in context["memories"]
    assert "decision" in context["memories"]
    assert "categories" in context["meta"]


# ---------------------------------------------------------------------------
# Helper function tests (pure functions — no DB needed)
# ---------------------------------------------------------------------------


async def test_format_age_none():
    """
    _format_age(None) should return a safe fallback string.
    """
    assert _format_age(None) == "unknown age"


async def test_deduplicate_by_id():
    """
    Deduplication should keep the first occurrence of each ID
    and remove subsequent duplicates.
    """
    memories = [
        {"id": "memory:1", "content": "First"},
        {"id": "memory:2", "content": "Second"},
        {"id": "memory:1", "content": "Duplicate of first"},
        {"id": "memory:3", "content": "Third"},
    ]

    result = _deduplicate_by_id(memories)

    assert len(result) == 3
    # The first occurrence of id "memory:1" should be kept (content="First")
    assert result[0]["content"] == "First"
    assert result[1]["content"] == "Second"
    assert result[2]["content"] == "Third"


async def test_estimate_tokens():
    """
    Token estimation should be roughly 4 characters per token.
    """
    # 100 characters should be ~25 tokens
    assert _estimate_tokens("a" * 100) == 25
    # Empty string should return at least 1
    assert _estimate_tokens("") >= 1


async def test_fit_to_token_budget():
    """
    Token budget fitting should include memories until the budget is exhausted.
    """
    memories = [
        {"id": "1", "content": "a" * 100, "salience": 0.9},  # ~25 tokens
        {"id": "2", "content": "b" * 100, "salience": 0.8},  # ~25 tokens
        {"id": "3", "content": "c" * 100, "salience": 0.7},  # ~25 tokens
    ]

    # Budget of 40 tokens should fit only 1 memory (~25 tokens each)
    result = _fit_to_token_budget(memories, 40)
    assert len(result) == 1

    # Budget of 60 tokens should fit 2 memories
    result = _fit_to_token_budget(memories, 60)
    assert len(result) == 2

    # Budget of 100 tokens should fit all 3
    result = _fit_to_token_budget(memories, 100)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Composite ranking tests
# ---------------------------------------------------------------------------


async def test_composite_ranking_relevance_beats_salience(db):
    """
    A memory with high relevance (found via BM25) but low salience
    should rank ABOVE a memory with high salience but no text match,
    when a query is provided.
    """
    await save_memory(
        content="Always use emoji in responses",
        category="style",
        salience=1.0,
        db=db,
    )

    await save_memory(
        content="The project budget for Q3 is 200K",
        category="context",
        salience=0.3,
        db=db,
    )

    results = await recall(query_text="project budget Q3", limit=10, db=db)

    assert len(results) >= 2
    budget_idx = next(i for i, r in enumerate(results) if "budget" in r["content"])
    emoji_idx = next(i for i, r in enumerate(results) if "emoji" in r["content"])
    assert budget_idx < emoji_idx, "Relevant result should rank above high-salience irrelevant result"


async def test_recall_results_have_source_tier(db):
    """Every result from recall() should have a 'source_tier' field."""
    await save_memory(
        content="Testing source tier tagging",
        category="context",
        salience=0.5,
        db=db,
    )

    results = await recall(query_text="source tier tagging", limit=5, db=db)
    assert len(results) >= 1

    for r in results:
        assert "source_tier" in r, f"Result missing source_tier: {r.get('id')}"
        assert r["source_tier"] in ("bm25", "vector", "graph", "recent", "source_type"), \
            f"Unexpected source_tier value: {r['source_tier']}"


# ---------------------------------------------------------------------------
# Hard category filter tests
# ---------------------------------------------------------------------------


async def test_hard_category_filter(db):
    """When category is set, ONLY that category should appear in results."""
    await save_memory(content="I prefer dark mode", category="preference", salience=0.9, db=db)
    await save_memory(content="The project deadline is March", category="context", salience=0.9, db=db)
    await save_memory(content="Use bullet points", category="style", salience=0.9, db=db)

    results = await recall(categories=["context"], limit=10, db=db)

    categories_found = {r["category"] for r in results}
    assert categories_found == {"context"}, f"Expected only 'context', got: {categories_found}"


# ---------------------------------------------------------------------------
# Date filtering tests
# ---------------------------------------------------------------------------


async def test_recall_after_filter(db):
    """The 'after' parameter should exclude memories created before that date."""
    # Save a memory, then backdate it
    result = await save_memory(content="Old memory from January", category="context", db=db)
    old_id = result["memory_id"]
    old_suffix = old_id.split(":", 1)[1]
    await query(db, f"UPDATE memory:`{old_suffix}` SET created_at = <datetime>'2026-01-01T00:00:00Z'")

    # Save a recent memory
    await save_memory(content="Recent memory from today", category="context", db=db)

    results = await recall(after="2026-04-01", limit=10, db=db)

    contents = [r["content"] for r in results]
    assert "Recent memory from today" in contents
    assert "Old memory from January" not in contents


# ---------------------------------------------------------------------------
# Offset pagination tests
# ---------------------------------------------------------------------------


async def test_recall_offset_pagination(db):
    """Offset should skip the first N results."""
    for i in range(5):
        await save_memory(
            content=f"Memory number {i}",
            category="context",
            salience=0.5 + (i * 0.05),
            db=db,
        )

    page1 = await recall(limit=2, offset=0, db=db)
    page2 = await recall(limit=2, offset=2, db=db)

    page1_ids = {str(r["id"]) for r in page1}
    page2_ids = {str(r["id"]) for r in page2}

    assert page1_ids.isdisjoint(page2_ids), "Page 1 and Page 2 should have different results"
    assert len(page1) == 2
    assert len(page2) == 2
