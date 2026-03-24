"""
Tests for qmemory.core.person

Tests the create_person() and find_person() functions — creating person entities
with linked contact identities (has_identity edges).

All tests use the `db` fixture from conftest.py, which gives us a fresh
SurrealDB connection in the "qmemory_test" namespace. Every test starts
with a clean, empty database so there's no cross-test interference.

These tests require SurrealDB to be running locally (ws://localhost:8000).

What we're testing:
  1. Basic person creation (name only, no contacts)
  2. Creating a person with contacts — entities and edges created
  3. Duplicate creation returns the existing person (action="found")
  4. find_person() by name finds the correct record
  5. find_person() by contact handle traverses back to the person
  6. Duplicate contact skipped if already exists (idempotent)
  7. Missing system/handle in contact dict is skipped gracefully
  8. find_person() returns None when no match exists
"""

import pytest

from qmemory.core.person import create_person, find_person
from qmemory.db.client import query


# ---------------------------------------------------------------------------
# Basic creation
# ---------------------------------------------------------------------------


async def test_create_person_name_only(db):
    """
    The simplest case: create a person with just a name, no contacts.

    Verifies:
    - Returns a dict with "entity_id", "contact_ids", "links_created", "action"
    - entity_id starts with "entity:"
    - contact_ids is empty (no contacts passed)
    - links_created is 0
    - action is "created"
    """
    result = await create_person(name="Ahmed Al-Rashid", db=db)

    assert result is not None

    # Check all required keys are present
    assert "entity_id" in result
    assert "contact_ids" in result
    assert "links_created" in result
    assert "action" in result

    # entity_id should be a proper SurrealDB record ID
    assert result["entity_id"].startswith("entity:")

    # No contacts were passed, so these should be empty/zero
    assert result["contact_ids"] == []
    assert result["links_created"] == 0

    # Brand new person — action should be "created"
    assert result["action"] == "created"


async def test_create_person_entity_exists_in_db(db):
    """
    After creating a person, the entity should be queryable from SurrealDB.

    This confirms the CREATE statement ran and persisted — not just that
    the function returned a result.
    """
    result = await create_person(name="Fatima Hassan", db=db)

    # Extract the suffix from "entity:pXXX" to use in a parameterized query
    person_id = result["entity_id"]
    _, id_suffix = person_id.split(":", 1)

    # Query SurrealDB directly
    rows = await query(
        db,
        "SELECT id, name, type FROM type::record('entity', $id)",
        {"id": id_suffix},
    )

    assert rows is not None
    assert len(rows) >= 1
    assert rows[0]["name"] == "Fatima Hassan"
    assert rows[0]["type"] == "person"


# ---------------------------------------------------------------------------
# Creating with contacts
# ---------------------------------------------------------------------------


async def test_create_person_with_contacts(db):
    """
    Creating a person with contacts should:
    - Create the person entity
    - Create a contact entity for each contact dict
    - Create a has_identity edge from person → each contact
    - Return the contact_ids and links_created count
    """
    result = await create_person(
        name="Omar Khaled",
        contacts=[
            {"system": "telegram", "handle": "@omar_kh"},
            {"system": "whatsapp", "handle": "+971501234567"},
        ],
        db=db,
    )

    # Should have created 2 contacts and 2 edges
    assert len(result["contact_ids"]) == 2
    assert result["links_created"] == 2
    assert result["action"] == "created"

    # Each contact_id should be a proper entity record ID
    for cid in result["contact_ids"]:
        assert cid.startswith("entity:")


async def test_contact_entities_exist_in_db(db):
    """
    The contact entities should actually be persisted in SurrealDB with
    the correct type, external_source, and external_id fields.
    """
    result = await create_person(
        name="Sara Ibrahim",
        contacts=[
            {"system": "telegram", "handle": "@sara_ib"},
        ],
        db=db,
    )

    assert len(result["contact_ids"]) == 1
    contact_id = result["contact_ids"][0]
    _, contact_suffix = contact_id.split(":", 1)

    # Query the contact entity directly
    rows = await query(
        db,
        "SELECT id, name, type, external_source, external_id FROM type::record('entity', $id)",
        {"id": contact_suffix},
    )

    assert rows is not None and len(rows) >= 1
    contact = rows[0]
    assert contact["type"] == "contact"
    assert contact["external_source"] == "telegram"
    assert contact["external_id"] == "@sara_ib"


async def test_has_identity_edges_exist_in_db(db):
    """
    The has_identity edges should exist in the `relates` table with the
    correct relationship_type field.

    This is the most important test for person creation — it proves the
    RELATE statements ran and the graph is actually connected.
    """
    result = await create_person(
        name="Khalid Mansour",
        contacts=[
            {"system": "email", "handle": "khalid@example.com"},
        ],
        db=db,
    )

    person_id = result["entity_id"]
    _, person_suffix = person_id.split(":", 1)

    # Query for all has_identity edges FROM this person
    edges = await query(
        db,
        """
        SELECT id, relationship_type, out FROM relates
        WHERE in = type::record('entity', $id)
        AND relationship_type = 'has_identity'
        """,
        {"id": person_suffix},
    )

    assert edges is not None
    assert len(edges) == 1
    assert edges[0]["relationship_type"] == "has_identity"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


async def test_create_duplicate_returns_existing(db):
    """
    Calling create_person() twice with the same name should return the
    existing person on the second call — NOT create a second entity.

    This tests the "found" path — the duplicate detection by name.
    """
    # First call — creates the person
    first = await create_person(name="Nour Al-Deen", db=db)
    assert first["action"] == "created"

    # Second call — same name
    second = await create_person(name="Nour Al-Deen", db=db)

    # Should be "found", not "created"
    assert second["action"] == "found"

    # Both calls should return the SAME entity_id
    assert first["entity_id"] == second["entity_id"]

    # There should only be ONE person entity with this name in the DB
    count_rows = await query(
        db,
        "SELECT count() FROM entity WHERE name = $name AND type = 'person' GROUP ALL",
        {"name": "Nour Al-Deen"},
    )
    # count() GROUP ALL returns [{"count": N}]
    assert count_rows is not None
    total = count_rows[0]["count"] if count_rows else 0
    assert total == 1


async def test_duplicate_contact_not_created_twice(db):
    """
    If the same contact system+handle appears in two separate create_person()
    calls, the contact entity should only be created once.

    First call creates the contact and the edge.
    Second call should detect the existing contact and skip creation.
    """
    contacts = [{"system": "telegram", "handle": "@shared_handle"}]

    first = await create_person(name="Person One", contacts=contacts, db=db)
    second = await create_person(name="Person Two", contacts=contacts, db=db)

    # Both people were created
    assert first["action"] == "created"
    assert second["action"] == "created"

    # But they should share the same contact entity_id
    assert len(first["contact_ids"]) == 1
    assert len(second["contact_ids"]) == 1
    assert first["contact_ids"][0] == second["contact_ids"][0]

    # Only ONE contact entity with this handle should exist in the DB
    count_rows = await query(
        db,
        "SELECT count() FROM entity WHERE external_id = $handle AND type = 'contact' GROUP ALL",
        {"handle": "@shared_handle"},
    )
    assert count_rows is not None
    total = count_rows[0]["count"] if count_rows else 0
    assert total == 1


# ---------------------------------------------------------------------------
# find_person
# ---------------------------------------------------------------------------


async def test_find_existing_person_by_name(db):
    """
    After creating a person, find_person() should return the correct record
    when searching by the exact name.
    """
    created = await create_person(
        name="Yasmin Othman",
        contacts=[{"system": "telegram", "handle": "@yasmin_o"}],
        db=db,
    )

    found = await find_person("Yasmin Othman", db=db)

    assert found is not None
    assert found["entity_id"] == created["entity_id"]
    assert found["name"] == "Yasmin Othman"

    # Should also list the contact IDs
    assert len(found["contact_ids"]) == 1


async def test_find_person_by_contact_handle(db):
    """
    find_person() should also work when searching by a contact handle —
    it traverses the has_identity edge backwards to find the person.
    """
    created = await create_person(
        name="Tariq Saeed",
        contacts=[{"system": "telegram", "handle": "@tariq_s"}],
        db=db,
    )

    # Search by handle, not name
    found = await find_person("@tariq_s", db=db)

    assert found is not None
    assert found["entity_id"] == created["entity_id"]
    assert found["name"] == "Tariq Saeed"


async def test_find_person_returns_none_when_not_found(db):
    """
    find_person() should return None (not raise, not return an empty dict)
    when no matching person or contact exists.
    """
    result = await find_person("NonexistentPersonXYZ", db=db)

    assert result is None


async def test_find_person_by_alias(db):
    """
    find_person() should find a person if the search term matches one of
    their aliases (not just the primary name).
    """
    await create_person(
        name="Mohammed Al-Farsi",
        aliases=["Mo", "Mohd"],
        db=db,
    )

    # Search by alias
    found = await find_person("Mo", db=db)

    assert found is not None
    assert found["name"] == "Mohammed Al-Farsi"


# ---------------------------------------------------------------------------
# Edge cases / graceful degradation
# ---------------------------------------------------------------------------


async def test_contact_missing_system_is_skipped(db):
    """
    A contact dict missing the "system" key should be silently skipped.
    The person should still be created without error.
    """
    result = await create_person(
        name="Hassan Nouri",
        contacts=[
            {"handle": "@hassan"},  # Missing "system" key
        ],
        db=db,
    )

    # Person was still created
    assert result["action"] == "created"
    assert result["entity_id"].startswith("entity:")

    # But no contacts were created (the malformed one was skipped)
    assert result["contact_ids"] == []
    assert result["links_created"] == 0


async def test_contact_missing_handle_is_skipped(db):
    """
    A contact dict missing the "handle" key should be silently skipped.
    """
    result = await create_person(
        name="Leila Karim",
        contacts=[
            {"system": "telegram"},  # Missing "handle" key
        ],
        db=db,
    )

    assert result["action"] == "created"
    assert result["contact_ids"] == []
    assert result["links_created"] == 0


async def test_create_person_with_aliases(db):
    """
    Aliases should be stored on the entity record.
    """
    result = await create_person(
        name="Rania Aziz",
        aliases=["Rani", "RaniaA"],
        db=db,
    )

    assert result["action"] == "created"
    _, id_suffix = result["entity_id"].split(":", 1)

    rows = await query(
        db,
        "SELECT aliases FROM type::record('entity', $id)",
        {"id": id_suffix},
    )

    assert rows is not None and len(rows) >= 1
    stored_aliases = rows[0].get("aliases") or []
    assert "Rani" in stored_aliases
    assert "RaniaA" in stored_aliases


async def test_create_person_no_contacts_no_aliases(db):
    """
    Calling create_person with just a name (no contacts, no aliases)
    should work cleanly with default empty lists.
    """
    result = await create_person(name="Bare Person", db=db)

    assert result["action"] == "created"
    assert result["contact_ids"] == []
    assert result["links_created"] == 0
