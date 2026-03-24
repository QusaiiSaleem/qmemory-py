"""
Tests for qmemory.core.correct

Tests all 4 actions of correct_memory():
  - "correct"  — replaces content, creates prev_version audit chain
  - "delete"   — soft-deletes only (is_active = false)
  - "update"   — mutates metadata without versioning
  - "unlink"   — hard-deletes a relates edge

All tests use the `db` fixture from conftest.py, which gives us a fresh
SurrealDB connection in the "qmemory_test" namespace. Every test starts
with a clean, empty database so there's no cross-test interference.

These tests require SurrealDB to be running locally (ws://localhost:8000).
"""

import pytest

from qmemory.core.correct import correct_memory
from qmemory.core.save import save_memory
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# Action: correct
# ---------------------------------------------------------------------------


async def test_correct_memory(db):
    """
    The "correct" action should:
    - Return action="corrected"
    - Return a new_memory_id that is DIFFERENT from the original
    - The new memory should be active with the new content
    - The old memory should be soft-deleted (is_active = false)
    - A prev_version edge should link the new memory to the old one
    """
    # Create a memory with the wrong budget figure
    saved = await save_memory(content="Budget is 400K", category="context", db=db)
    original_id = saved["memory_id"]  # e.g. "memory:mem1234abc"

    # Correct the budget figure
    result = await correct_memory(
        memory_id=original_id,
        action="correct",
        new_content="Budget is 500K",
        db=db,
    )

    # --- Check the return value ---
    assert result["action"] == "corrected"
    # The new memory should have a different ID
    assert result["new_memory_id"] != original_id
    # Both memory IDs should be present in the result
    assert result["memory_id"] == original_id
    assert "_nudge" in result

    # --- Verify old memory is now soft-deleted ---
    old_id_suffix = original_id.split(":")[1]
    old_mem = await query(
        db,
        "SELECT is_active, content FROM type::record('memory', $id)",
        {"id": old_id_suffix},
    )
    assert old_mem is not None and len(old_mem) == 1
    assert old_mem[0]["is_active"] is False  # Soft-deleted

    # --- Verify new memory is active with corrected content ---
    new_id_suffix = result["new_memory_id"].split(":")[1]
    new_mem = await query(
        db,
        "SELECT is_active, content FROM type::record('memory', $id)",
        {"id": new_id_suffix},
    )
    assert new_mem is not None and len(new_mem) == 1
    assert new_mem[0]["is_active"] is True
    assert new_mem[0]["content"] == "Budget is 500K"

    # --- Verify the prev_version edge exists ---
    # The new memory should point to the old memory via prev_version
    edges = await query(
        db,
        "SELECT * FROM prev_version WHERE in = <record>$from_id",
        {"from_id": result["new_memory_id"]},
    )
    assert edges is not None and len(edges) >= 1


async def test_correct_preserves_metadata(db):
    """
    When correcting a memory, all metadata (category, salience, scope,
    confidence) should be copied from the old memory to the new one.

    The agent's correction only changes the content — metadata carries over.
    """
    saved = await save_memory(
        content="Old fact",
        category="decision",
        salience=0.9,
        scope="project:beta",
        confidence=0.95,
        db=db,
    )

    result = await correct_memory(
        memory_id=saved["memory_id"],
        action="correct",
        new_content="New corrected fact",
        db=db,
    )

    new_id_suffix = result["new_memory_id"].split(":")[1]
    new_mem = await query(
        db,
        "SELECT category, salience, scope, confidence FROM type::record('memory', $id)",
        {"id": new_id_suffix},
    )

    assert new_mem is not None and len(new_mem) == 1
    mem = new_mem[0]
    # Metadata should be copied from the original
    assert mem["category"] == "decision"
    assert mem["salience"] == 0.9
    assert mem["scope"] == "project:beta"
    assert mem["confidence"] == 0.95


# ---------------------------------------------------------------------------
# Action: delete
# ---------------------------------------------------------------------------


async def test_delete_memory(db):
    """
    The "delete" action should:
    - Return action="deleted"
    - Set is_active = false on the memory (soft-delete)
    - NOT hard-delete the record — it should still exist in the DB
    """
    saved = await save_memory(content="Wrong fact", category="context", db=db)
    original_id = saved["memory_id"]

    result = await correct_memory(
        memory_id=original_id,
        action="delete",
        db=db,
    )

    # Check return value
    assert result["action"] == "deleted"
    assert result["memory_id"] == original_id
    assert "_nudge" in result

    # Verify the record STILL EXISTS but is now inactive
    id_suffix = original_id.split(":")[1]
    mem = await query(
        db,
        "SELECT is_active FROM type::record('memory', $id)",
        {"id": id_suffix},
    )
    assert mem is not None and len(mem) == 1
    # Soft-deleted — is_active should be False
    assert mem[0]["is_active"] is False


async def test_delete_returns_not_found_for_missing_memory(db):
    """
    Deleting a memory that doesn't exist should return a not_found response,
    not raise an exception. The agent needs a clear signal to try a different ID.
    """
    result = await correct_memory(
        memory_id="memory:nonexistent999",
        action="delete",
        db=db,
    )

    # Should return not_found, not raise an exception
    assert result.get("action") == "not_found" or "error" in result


# ---------------------------------------------------------------------------
# Action: update
# ---------------------------------------------------------------------------


async def test_update_metadata(db):
    """
    The "update" action should mutate metadata fields directly on the memory
    WITHOUT creating a new version or soft-deleting the original.
    """
    saved = await save_memory(
        content="Some fact",
        category="context",
        salience=0.5,
        scope="global",
        db=db,
    )
    original_id = saved["memory_id"]

    result = await correct_memory(
        memory_id=original_id,
        action="update",
        updates={"salience": 0.9, "scope": "project:alpha"},
        db=db,
    )

    # Check return value
    assert result["action"] == "updated"
    assert result["memory_id"] == original_id

    # Verify the fields were actually changed in the DB
    id_suffix = original_id.split(":")[1]
    mem = await query(
        db,
        "SELECT salience, scope, is_active FROM type::record('memory', $id)",
        {"id": id_suffix},
    )
    assert mem is not None and len(mem) == 1
    # Fields should be updated
    assert mem[0]["salience"] == 0.9
    assert mem[0]["scope"] == "project:alpha"
    # Memory should still be ACTIVE (update does not soft-delete)
    assert mem[0]["is_active"] is True


async def test_update_single_field(db):
    """
    Update should work when only one field is changed.
    The others should remain at their original values.
    """
    saved = await save_memory(
        content="Fact to update",
        category="context",
        salience=0.3,
        db=db,
    )

    result = await correct_memory(
        memory_id=saved["memory_id"],
        action="update",
        updates={"salience": 0.8},
        db=db,
    )

    assert result["action"] == "updated"

    id_suffix = saved["memory_id"].split(":")[1]
    mem = await query(
        db,
        "SELECT salience FROM type::record('memory', $id)",
        {"id": id_suffix},
    )
    assert mem[0]["salience"] == 0.8


# ---------------------------------------------------------------------------
# Action: unlink
# ---------------------------------------------------------------------------


async def test_unlink_edge(db):
    """
    The "unlink" action should hard-delete a `relates` edge.
    The two memory nodes should still exist after unlinking.
    """
    # Create two memories to link together
    mem1 = await save_memory(content="Memory one", category="context", db=db)
    mem2 = await save_memory(content="Memory two", category="context", db=db)

    id1 = mem1["memory_id"].split(":")[1]
    id2 = mem2["memory_id"].split(":")[1]

    # Create a relates edge between them manually (what the linker service does)
    edge_result = await query(
        db,
        f"RELATE memory:`{id1}`->relates->memory:`{id2}` SET relationship_type = 'related_to', created_at = time::now()",
    )

    # Find the edge ID so we can unlink it
    edges = await query(
        db,
        "SELECT id FROM relates WHERE in = <record>$from_id",
        {"from_id": mem1["memory_id"]},
    )
    assert edges is not None and len(edges) >= 1

    edge_id = edges[0]["id"]  # e.g. "relates:relXXX"

    # Unlink the edge
    result = await correct_memory(
        memory_id=mem1["memory_id"],  # context — the "from" memory
        action="unlink",
        edge_id=edge_id,
        db=db,
    )

    assert result["action"] == "unlinked"
    assert "_nudge" in result

    # Verify the edge is GONE
    edges_after = await query(
        db,
        "SELECT id FROM relates WHERE in = <record>$from_id",
        {"from_id": mem1["memory_id"]},
    )
    assert edges_after is None or len(edges_after) == 0


# ---------------------------------------------------------------------------
# Validation / error cases
# ---------------------------------------------------------------------------


async def test_invalid_action_raises(db):
    """
    An unrecognized action should raise ValueError immediately.
    The agent might try action="fix" or action="replace" — catch it early.
    """
    saved = await save_memory(content="test", category="context", db=db)

    with pytest.raises(ValueError, match="Invalid action"):
        await correct_memory(
            memory_id=saved["memory_id"],
            action="invalid",
            db=db,
        )


async def test_correct_without_new_content_raises(db):
    """
    action="correct" without providing new_content should raise ValueError.
    """
    saved = await save_memory(content="test", category="context", db=db)

    with pytest.raises(ValueError, match="new_content is required"):
        await correct_memory(
            memory_id=saved["memory_id"],
            action="correct",
            # new_content intentionally omitted
            db=db,
        )


async def test_update_without_updates_raises(db):
    """
    action="update" without providing the updates dict should raise ValueError.
    """
    saved = await save_memory(content="test", category="context", db=db)

    with pytest.raises(ValueError, match="updates dict is required"):
        await correct_memory(
            memory_id=saved["memory_id"],
            action="update",
            # updates intentionally omitted
            db=db,
        )


async def test_unlink_without_edge_id_raises(db):
    """
    action="unlink" without providing edge_id should raise ValueError.
    """
    saved = await save_memory(content="test", category="context", db=db)

    with pytest.raises(ValueError, match="edge_id is required"):
        await correct_memory(
            memory_id=saved["memory_id"],
            action="unlink",
            # edge_id intentionally omitted
            db=db,
        )


async def test_correct_nonexistent_memory(db):
    """
    Correcting a memory that doesn't exist should return a not_found
    response, not raise an exception.
    """
    result = await correct_memory(
        memory_id="memory:nonexistent999",
        action="correct",
        new_content="This should not be created",
        db=db,
    )

    # Should return not_found gracefully
    assert result.get("action") == "not_found" or "error" in result
