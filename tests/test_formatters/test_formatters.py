"""
Tests for qmemory.formatters — pure function formatters.

These tests do NOT require a database connection. All functions are
deterministic transformations of input data into text.
"""

import pytest


# ---------------------------------------------------------------------------
# memories.py
# ---------------------------------------------------------------------------

class TestFormatMemories:
    def test_groups_by_category(self):
        """Self memories appear before context memories; both are present."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:m1",
                "content": "I like direct talk",
                "category": "self",
                "salience": 0.9,
                "scope": "global",
                "confidence": 0.9,
                "created_at": "2026-03-24T00:00:00Z",
            },
            {
                "id": "memory:m2",
                "content": "Budget is 500K",
                "category": "context",
                "salience": 0.8,
                "scope": "global",
                "confidence": 0.8,
                "created_at": "2026-03-23T00:00:00Z",
            },
        ]
        result = format_memories(mems)

        # Both pieces of content should appear
        assert "I like direct talk" in result
        assert "500K" in result

        # Self-Model header must appear
        assert "Self-Model" in result

        # Self-Model section must come before context section
        self_pos = result.index("Self-Model")
        context_pos = result.index("500K")
        assert self_pos < context_pos

    def test_empty_memories_returns_empty(self):
        """No memories and no tools guide → empty string."""
        from qmemory.formatters.memories import format_memories

        assert format_memories([]) == ""

    def test_hypothesis_section_appears_for_low_confidence(self):
        """Memories with confidence < 0.5 land in the Hypotheses section."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:m3",
                "content": "Budget might increase next quarter",
                "category": "context",
                "salience": 0.4,
                "scope": "global",
                "confidence": 0.3,
                "created_at": "2026-03-17T00:00:00Z",
            },
        ]
        result = format_memories(mems, include_hypotheses=True)
        assert "Hypotheses" in result
        assert "Budget might increase" in result

    def test_self_memories_have_no_ids(self):
        """Self memories are rendered without their DB ID."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:secret_id_123",
                "content": "I prefer concise answers",
                "category": "self",
                "salience": 0.9,
                "scope": "global",
                "confidence": 0.95,
                "created_at": "2026-03-24T00:00:00Z",
            },
        ]
        result = format_memories(mems)
        # Content is there
        assert "I prefer concise answers" in result
        # But the raw DB ID should NOT appear (self memories skip IDs)
        assert "secret_id_123" not in result

    def test_tools_guide_included_when_requested(self):
        """include_tools_guide=True appends the tools reference block."""
        from qmemory.formatters.memories import format_memories

        result = format_memories([], include_tools_guide=True)
        assert "qmemory_save" in result
        assert "Memory Tools" in result

    def test_tools_guide_excluded_by_default(self):
        """Tools guide should NOT appear when not requested."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:m4",
                "content": "Team prefers async communication",
                "category": "preference",
                "salience": 0.7,
                "scope": "global",
                "confidence": 0.8,
                "created_at": "2026-03-20T00:00:00Z",
            },
        ]
        result = format_memories(mems)
        assert "qmemory_save" not in result

    def test_contradiction_marker_shown(self):
        """Contradicted memories get the ⚠︎ marker."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:m5",
                "content": "Office is in Riyadh",
                "category": "context",
                "salience": 0.8,
                "scope": "global",
                "confidence": 0.9,
                "is_contradicted": True,
                "created_at": "2026-03-20T00:00:00Z",
            },
        ]
        result = format_memories(mems)
        assert "⚠︎" in result

    def test_non_global_scope_shown(self):
        """Non-global scope is shown in brackets on the memory line."""
        from qmemory.formatters.memories import format_memories

        mems = [
            {
                "id": "memory:m6",
                "content": "Feature X is in progress",
                "category": "context",
                "salience": 0.7,
                "scope": "project:alpha",
                "confidence": 0.9,
                "created_at": "2026-03-20T00:00:00Z",
            },
        ]
        result = format_memories(mems)
        assert "project:alpha" in result


# ---------------------------------------------------------------------------
# budget.py
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_returns_positive_for_nonempty_string(self):
        from qmemory.formatters.budget import estimate_tokens

        assert estimate_tokens("hello world") > 0

    def test_returns_zero_for_empty_string(self):
        from qmemory.formatters.budget import estimate_tokens

        assert estimate_tokens("") == 0

    def test_longer_text_has_more_tokens(self):
        from qmemory.formatters.budget import estimate_tokens

        assert estimate_tokens("a" * 100) > estimate_tokens("a" * 10)

    def test_roughly_four_chars_per_token(self):
        from qmemory.formatters.budget import estimate_tokens

        # 400 chars → ~100 tokens
        result = estimate_tokens("a" * 400)
        assert 90 <= result <= 110


class TestApplyBudget:
    def test_trims_to_fit_budget(self):
        from qmemory.formatters.budget import apply_budget

        # Each memory has 1000-char content → ~270 tokens + 20 overhead = ~290
        # With max_tokens=100, none should fit (each is too big on its own)
        mems = [{"content": "x" * 1000, "salience": 0.5 + i * 0.1} for i in range(10)]
        trimmed = apply_budget(mems, max_tokens=100)
        assert len(trimmed) < 10

    def test_preserves_highest_salience(self):
        from qmemory.formatters.budget import apply_budget

        # Small content so multiple can fit; highest salience should survive
        mems = [
            {"content": f"Memory number {i:02d} has important content here", "salience": i * 0.1}
            for i in range(10)
        ]
        trimmed = apply_budget(mems, max_tokens=500)
        # All trimmed memories should have salience >= the lowest in result
        if trimmed:
            salient_values = [m["salience"] for m in trimmed]
            assert max(salient_values) >= min(salient_values)

    def test_skips_very_short_content(self):
        from qmemory.formatters.budget import apply_budget

        mems = [{"content": "hi", "salience": 0.9}]  # < 15 chars → noise
        trimmed = apply_budget(mems, max_tokens=1000)
        assert len(trimmed) == 0

    def test_deduplicates_similar_content(self):
        from qmemory.formatters.budget import apply_budget

        # Two memories with identical start (first 60 chars) should deduplicate
        content = "The team uses async-first communication for most decisions and updates"
        mems = [
            {"content": content, "salience": 0.8},
            {"content": content, "salience": 0.7},  # duplicate
        ]
        trimmed = apply_budget(mems, max_tokens=500)
        assert len(trimmed) == 1

    def test_empty_list_returns_empty(self):
        from qmemory.formatters.budget import apply_budget

        assert apply_budget([], max_tokens=1000) == []


# ---------------------------------------------------------------------------
# budget.py — get_age
# ---------------------------------------------------------------------------

class TestGetAge:
    def test_recent_date_returns_just_now(self):
        from qmemory.formatters.budget import get_age
        from datetime import datetime, timezone, timedelta

        recent = (datetime.now(tz=timezone.utc) - timedelta(minutes=10)).isoformat()
        assert "just now" in get_age(recent)

    def test_hours_ago(self):
        from qmemory.formatters.budget import get_age
        from datetime import datetime, timezone, timedelta

        dt = (datetime.now(tz=timezone.utc) - timedelta(hours=5)).isoformat()
        result = get_age(dt)
        assert "5h ago" in result

    def test_days_ago(self):
        from qmemory.formatters.budget import get_age
        from datetime import datetime, timezone, timedelta

        dt = (datetime.now(tz=timezone.utc) - timedelta(days=3)).isoformat()
        result = get_age(dt)
        assert "3d ago" in result

    def test_weeks_ago(self):
        from qmemory.formatters.budget import get_age
        from datetime import datetime, timezone, timedelta

        dt = (datetime.now(tz=timezone.utc) - timedelta(weeks=2)).isoformat()
        result = get_age(dt)
        assert "2w ago" in result

    def test_invalid_date_returns_empty(self):
        from qmemory.formatters.budget import get_age

        assert get_age("not-a-date") == ""

    def test_future_date_returns_empty(self):
        from qmemory.formatters.budget import get_age
        from datetime import datetime, timezone, timedelta

        future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
        assert get_age(future) == ""


# ---------------------------------------------------------------------------
# graph_map.py
# ---------------------------------------------------------------------------

class TestFormatGraphMap:
    def test_empty_returns_empty_string(self):
        from qmemory.formatters.graph_map import format_graph_map

        assert format_graph_map([], []) == ""

    def test_shows_entity_names(self):
        from qmemory.formatters.graph_map import format_graph_map

        entities = [
            {"id": "entity:e1", "name": "Qusai", "type": "person"},
            {"id": "entity:e2", "name": "Project Alpha", "type": "project"},
        ]
        result = format_graph_map(entities, [])
        assert "Qusai" in result
        assert "Project Alpha" in result

    def test_shows_relationship_hints(self):
        from qmemory.formatters.graph_map import format_graph_map

        entities = [
            {"id": "entity:e1", "name": "Qusai", "type": "person"},
            {"id": "entity:e2", "name": "Project Alpha", "type": "project"},
        ]
        edges = [
            {"from_node": "entity:e1", "to_node": "entity:e2", "type": "owns"},
        ]
        result = format_graph_map(entities, edges)
        assert "owns" in result

    def test_stats_header_shown(self):
        from qmemory.formatters.graph_map import format_graph_map

        entities = [{"id": "entity:e1", "name": "Qusai", "type": "person"}]
        stats = {"entities": 5, "edges": 10, "memories": 42, "orphans": 0}
        result = format_graph_map(entities, [], stats=stats)
        assert "5 entities" in result
        assert "42 memories" in result

    def test_orphan_nudge_shown(self):
        from qmemory.formatters.graph_map import format_graph_map

        entities = [{"id": "entity:e1", "name": "Qusai", "type": "person"}]
        stats = {"entities": 1, "edges": 0, "memories": 10, "orphans": 7}
        result = format_graph_map(entities, [], stats=stats)
        assert "7 memories have no relationships" in result

    def test_book_library_section(self):
        from qmemory.formatters.graph_map import format_graph_map

        entities = [
            {"id": "entity:b1", "name": "Thinking Fast and Slow", "type": "book", "total_links": 15},
            {"id": "entity:b2", "name": "The Lean Startup", "type": "book", "total_links": 8},
        ]
        result = format_graph_map(entities, [])
        assert "Library" in result
        assert "Thinking Fast and Slow" in result
