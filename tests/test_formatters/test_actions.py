"""Tests for per-result and per-entity action builders."""

from qmemory.formatters.actions import (
    build_actions,
    build_memory_actions,
    build_entity_actions,
    build_category_drill_down,
)


def test_build_memory_actions_basic():
    """Memory actions should include correct, link, get_neighbors."""
    actions = build_memory_actions("memory:mem123")
    assert "correct" in actions
    assert actions["correct"]["tool"] == "qmemory_correct"
    assert actions["correct"]["args"]["memory_id"] == "memory:mem123"
    assert "link" in actions
    assert "get_neighbors" in actions


def test_build_entity_actions_basic():
    """Entity actions should include get and search_within."""
    actions = build_entity_actions("entity:ent456")
    assert "get" in actions
    assert actions["get"]["args"]["ids"] == ["entity:ent456"]
    assert actions["get"]["args"]["include_neighbors"] is True
    assert "search_within" in actions
    assert actions["search_within"]["args"]["entity_id"] == "entity:ent456"


def test_build_category_drill_down():
    """Drill-down actions should only include categories with > 1 result."""
    by_category = {"context": 5, "preference": 1, "decision": 3}
    actions = build_category_drill_down("Ahmed", by_category)
    tools = [a["args"]["category"] for a in actions]
    assert "context" in tools
    assert "decision" in tools
    assert "preference" not in tools


def test_build_category_drill_down_empty():
    """Empty category map should return empty list."""
    actions = build_category_drill_down("test", {})
    assert actions == []
