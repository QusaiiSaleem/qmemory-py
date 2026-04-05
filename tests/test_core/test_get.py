"""
Tests for qmemory.core.get — fetch by ID + neighbor traversal.

Tests require SurrealDB running locally.
"""
import pytest

from qmemory.core.get import get_memories
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes


async def test_get_by_id_returns_memory(db):
    """Fetching a saved memory by ID should return its full content."""
    saved = await save_memory(content="Test fact for get_by_id", category="context", db=db)
    mem_id = saved["memory_id"]

    result = await get_memories(ids=[mem_id], db=db)

    assert len(result["memories"]) == 1
    assert result["memories"][0]["content"] == "Test fact for get_by_id"
    assert result["not_found"] == []
    assert "actions" in result
    assert "meta" in result
    assert result["meta"]["found"] == 1
    assert result["meta"]["requested"] == 1


async def test_get_not_found(db):
    """Fetching a non-existent ID should list it in not_found."""
    result = await get_memories(ids=["memory:nonexistent999"], db=db)

    assert result["memories"] == []
    assert "memory:nonexistent999" in result["not_found"]
    assert result["meta"]["found"] == 0
    assert result["meta"]["requested"] == 1


async def test_get_mixed_found_and_not_found(db):
    """Mix of existing and non-existing IDs returns both found and not_found."""
    saved = await save_memory(content="Existing memory", category="context", db=db)
    mem_id = saved["memory_id"]

    result = await get_memories(ids=[mem_id, "memory:fake123"], db=db)

    assert len(result["memories"]) == 1
    assert result["memories"][0]["content"] == "Existing memory"
    assert "memory:fake123" in result["not_found"]
    assert result["meta"]["found"] == 1
    assert result["meta"]["requested"] == 2


async def test_get_with_neighbors(db):
    """include_neighbors=True should return connected nodes."""
    saved_a = await save_memory(content="Memory A", category="context", db=db)
    saved_b = await save_memory(content="Memory B", category="context", db=db)
    id_a = saved_a["memory_id"]
    id_b = saved_b["memory_id"]

    # Link A → B
    await link_nodes(from_id=id_a, to_id=id_b, relationship_type="supports", db=db)

    result = await get_memories(ids=[id_a], include_neighbors=True, db=db)

    assert len(result["memories"]) == 1
    mem = result["memories"][0]
    assert mem["neighbors"]["count"] >= 1
    neighbor_ids = [n["id"] for n in mem["neighbors"]["items"]]
    assert id_b in neighbor_ids


async def test_get_max_ids_limit(db):
    """Requesting more than 20 IDs should raise ValueError."""
    ids = [f"memory:mem{i}" for i in range(25)]
    with pytest.raises(ValueError, match="Maximum 20 IDs"):
        await get_memories(ids=ids, db=db)


async def test_get_multiple_ids(db):
    """Fetching multiple valid IDs should return all of them."""
    saved1 = await save_memory(content="First memory", category="context", db=db)
    saved2 = await save_memory(content="Second memory", category="context", db=db)

    result = await get_memories(ids=[saved1["memory_id"], saved2["memory_id"]], db=db)

    assert result["meta"]["found"] == 2
    assert result["meta"]["requested"] == 2
    assert len(result["memories"]) == 2
    contents = {m["content"] for m in result["memories"]}
    assert "First memory" in contents
    assert "Second memory" in contents
