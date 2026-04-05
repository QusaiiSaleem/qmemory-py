"""
Core Person & Contact Management

Creates person entities with multiple linked contact identities.
A person can have Telegram, WhatsApp, email, Smartsheet IDs — all linked
via `has_identity` edges in the `relates` table.

Pattern:
  entity:ahmed (type: "person", name: "Ahmed")
    → relates → entity:ahmed_tg (type: "contact", external_source: "telegram")
        relationship_type: "has_identity"
    → relates → entity:ahmed_email (type: "contact", external_source: "gmail")
        relationship_type: "has_identity"

Query "everything about Ahmed":
  SELECT * FROM relates WHERE in = type::record('entity', $id) AND relationship_type = 'has_identity'

Flow for create_person():
  1. Check if a person with this name already exists
  2. If exists, return it with action="found"
  3. If not, CREATE a new entity node (type="person")
  4. For each contact in the contacts list:
     a. Check if that contact (external_source + handle) already exists
     b. If not, CREATE a contact entity node
     c. Check if the has_identity edge already exists
     d. If not, RELATE person → contact with relationship_type="has_identity"
  5. Return result with entity_id, contact_ids, links_created, action

Design decisions:
  - Accepts optional `db` for test injection (same pattern as save/link/correct).
  - RELATE uses backtick syntax (not type::record) — SurrealDB 3.0 requirement.
  - Optional fields omitted when None — SurrealDB 3.0 rejects NULL for option<> fields.
  - Contacts use a dict with "system" + "handle" keys (matches the OpenClaw tool interface).
    Internally mapped to external_source / external_id on the entity record.
"""

from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import generate_id, get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Main exports
# ---------------------------------------------------------------------------


async def create_person(
    name: str,
    aliases: list[str] | None = None,
    contacts: list[dict] | None = None,  # [{"system": "telegram", "handle": "@user"}]
    db: Any = None,
) -> dict:
    """
    Create a person entity with linked contact identities.

    If a person with this name already exists, returns the existing record
    with action="found". Otherwise creates a new entity with action="created".

    For each contact dict, creates a contact entity (type="contact") and
    links it to the person via a "has_identity" edge in the `relates` table.
    Duplicate contacts (same system + handle) are detected and skipped.

    Args:
        name:     The person's display name (e.g. "Ahmed Al-Rashid").
        aliases:  Optional list of alternative names/nicknames.
        contacts: Optional list of contact dicts:
                    {"system": "telegram", "handle": "@username"}
                  "system" maps to external_source, "handle" to external_id.
        db:       Optional SurrealDB connection. If None, creates one via get_db().
                  Always pass the test fixture here in tests.

    Returns:
        dict with:
          - "entity_id":     Full record ID of the person entity (e.g. "entity:pXXX")
          - "contact_ids":   List of full record IDs for each contact entity
          - "links_created": How many new has_identity edges were created
          - "action":        "created" (new person) or "found" (already existed)
    """
    if db is not None:
        # Test mode: use the provided connection directly
        return await _create_person_impl(name, aliases, contacts, db)
    else:
        # Production mode: create a fresh connection
        async with get_db() as conn:
            return await _create_person_impl(name, aliases, contacts, conn)


async def find_person(
    name_or_handle: str,
    db: Any = None,
) -> dict | None:
    """
    Search for a person by name or contact handle.

    Searches:
      1. By name (exact match against the entity name field)
      2. By alias (the name appears in the aliases list)
      3. By contact handle (exact match against external_id on linked contacts)

    Args:
        name_or_handle: The name or handle to search for.
        db:             Optional SurrealDB connection for test injection.

    Returns:
        dict with "entity_id", "name", "aliases", "contact_ids" if found.
        Returns None if no matching person is found.
    """
    if db is not None:
        return await _find_person_impl(name_or_handle, db)
    else:
        async with get_db() as conn:
            return await _find_person_impl(name_or_handle, conn)


# ---------------------------------------------------------------------------
# Internal implementations (called with an active DB connection)
# ---------------------------------------------------------------------------


async def _create_person_impl(
    name: str,
    aliases: list[str] | None,
    contacts: list[dict] | None,
    db: Any,
) -> dict:
    """
    Internal: create (or find) a person entity, then attach contacts.

    Called with an active DB connection already established.
    """

    # Normalize inputs — treat None the same as empty list
    aliases = aliases or []
    contacts = contacts or []

    # --- Step 1: Check if this person already exists by name or alias ---
    # We search both the name field and the aliases array so that
    # "Ahmed" matches both name="Ahmed" and aliases=["Ahmed Al-Rashid"].
    existing = await query(
        db,
        "SELECT * FROM entity WHERE name = $name AND type = 'person' AND is_active != false LIMIT 1",
        {"name": name},
    )

    if existing and len(existing) > 0:
        # --- Step 2: Person already exists — return the existing record ---
        # We still process contacts below, in case new contacts were passed.
        person_record = existing[0]
        person_id = str(person_record["id"])

        # Update aliases if new ones were provided
        if aliases:
            current_aliases = person_record.get("aliases") or []
            merged = list({*current_aliases, *aliases})  # deduplicate with a set
            await query(
                db,
                "UPDATE type::record($id) SET aliases = $aliases, updated_at = time::now()",
                {"id": person_id, "aliases": merged},
            )

        logger.info("create_person: found existing person %s (%s)", person_id, name)
        action = "found"
    else:
        # --- Step 3: Create a new person entity ---
        # ID format: "entity:pXXXX" (prefix "p" for person)
        person_suffix = generate_id("p")
        person_id = f"entity:{person_suffix}"

        await query(
            db,
            """
            CREATE type::record('entity', $id) SET
                name = $name,
                type = 'person',
                aliases = $aliases,
                created_at = time::now(),
                updated_at = time::now()
            """,
            {
                "id": person_suffix,
                "name": name,
                "aliases": aliases,
            },
        )

        logger.info("create_person: created new person %s (%s)", person_id, name)
        action = "created"

    # --- Step 4: Create contact entities and has_identity edges ---
    # Parse person_id into (table, suffix) for use in RELATE backtick syntax.
    # "entity:pXXX" → person_suffix = "pXXX"
    _, person_suffix = person_id.split(":", 1)

    contact_ids: list[str] = []
    links_created = 0

    for contact in contacts:
        system = contact.get("system", "")
        handle = contact.get("handle", "")

        if not system or not handle:
            # Skip malformed contact entries (missing required keys)
            logger.warning("create_person: skipping contact with missing system or handle: %s", contact)
            continue

        # --- Step 4a: Check if this contact already exists ---
        # We identify a contact by the combination of external_source + external_id.
        existing_contact = await query(
            db,
            """
            SELECT * FROM entity
            WHERE type = 'contact'
            AND external_source = $system
            AND external_id = $handle
            LIMIT 1
            """,
            {"system": system, "handle": handle},
        )

        if existing_contact and len(existing_contact) > 0:
            # Contact entity already exists — reuse it
            contact_id = str(existing_contact[0]["id"])
            logger.debug("create_person: found existing contact %s (%s:%s)", contact_id, system, handle)
        else:
            # --- Step 4b: Create a new contact entity ---
            # ID format: "entity:cXXXX" (prefix "c" for contact)
            contact_suffix = generate_id("c")
            contact_id = f"entity:{contact_suffix}"

            # The display name for this contact record
            contact_name = f"{name} ({system})"

            await query(
                db,
                """
                CREATE type::record('entity', $id) SET
                    name = $contact_name,
                    type = 'contact',
                    external_source = $system,
                    external_id = $handle,
                    created_at = time::now(),
                    updated_at = time::now()
                """,
                {
                    "id": contact_suffix,
                    "contact_name": contact_name,
                    "system": system,
                    "handle": handle,
                },
            )

            logger.info("create_person: created contact %s (%s:%s)", contact_id, system, handle)

        contact_ids.append(contact_id)

        # Parse contact_id into suffix for RELATE backtick syntax
        _, contact_suffix_for_relate = contact_id.split(":", 1)

        # --- Step 4c: Check if the has_identity edge already exists ---
        # We don't want duplicate edges between the same person and contact.
        existing_edge = await query(
            db,
            """
            SELECT id FROM relates
            WHERE in = <record>$from_id
            AND out = <record>$to_id
            AND relationship_type = 'has_identity'
            LIMIT 1
            """,
            {"from_id": person_id, "to_id": contact_id},
        )

        if existing_edge and len(existing_edge) > 0:
            # Edge already exists — skip creating it again
            logger.debug(
                "create_person: has_identity edge already exists %s → %s",
                person_id, contact_id
            )
        else:
            # --- Step 4d: RELATE person → contact with "has_identity" ---
            # CRITICAL: RELATE requires backtick syntax in SurrealDB 3.0.
            # type::record() in RELATE's FROM/TO positions causes a parse error.
            # This is safe because generate_id() only produces alphanumeric chars.
            relate_surql = (
                f"RELATE entity:`{person_suffix}`"
                f"->relates->"
                f"entity:`{contact_suffix_for_relate}` "
                f"SET relationship_type = $rel_type, created_at = time::now()"
            )

            await query(
                db,
                relate_surql,
                {"rel_type": "has_identity"},
            )

            links_created += 1
            logger.info(
                "create_person: linked %s -[has_identity]-> %s",
                person_id, contact_id
            )

    from qmemory.formatters.response import attach_meta

    # Count memories linked to this person
    mem_count_rows = await query(
        db,
        "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL",
        {"id": person_id},
    )
    mem_count = mem_count_rows[0]["c"] if mem_count_rows and isinstance(mem_count_rows, list) and len(mem_count_rows) > 0 and isinstance(mem_count_rows[0], dict) else 0

    return attach_meta(
        {
            "entity_id": person_id,
            "name": name,
            "action": action,
            "contacts": [
                {"system": c.get("system", ""), "handle": c.get("handle", ""), "entity_id": cid}
                for c, cid in zip(contacts, contact_ids)
            ] if contacts and contact_ids else [],
        },
        actions_context={"type": "person", "entity_id": person_id, "memory_count": mem_count},
        memory_count=mem_count,
        contact_count=len(contact_ids),
        links_created=links_created,
    )


async def _find_person_impl(
    name_or_handle: str,
    db: Any,
) -> dict | None:
    """
    Internal: search for a person by name or contact handle.

    Search order:
      1. Exact name match on entity records (type="person")
      2. Name appears in aliases array
      3. Handle matches a contact entity's external_id, then traverse back to person

    Returns a dict if found, None otherwise.
    """

    # --- Search 1: Exact name match ---
    by_name = await query(
        db,
        """
        SELECT * FROM entity
        WHERE type = 'person'
        AND is_active != false
        AND (name = $name OR $name IN aliases)
        LIMIT 1
        """,
        {"name": name_or_handle},
    )

    if by_name and len(by_name) > 0:
        person = by_name[0]
        person_id = str(person["id"])
        _, person_suffix = person_id.split(":", 1)

        # Find all linked contact IDs via has_identity edges
        contact_rows = await query(
            db,
            """
            SELECT out FROM relates
            WHERE in = <record>$from_id
            AND relationship_type = 'has_identity'
            """,
            {"from_id": person_id},
        )
        contact_ids = [str(row["out"]) for row in (contact_rows or [])]

        return {
            "entity_id": person_id,
            "name": person.get("name"),
            "aliases": person.get("aliases") or [],
            "contact_ids": contact_ids,
        }

    # --- Search 2: Handle match via contact entity ---
    # Look for a contact with this external_id, then find who it belongs to.
    by_handle = await query(
        db,
        """
        SELECT * FROM entity
        WHERE type = 'contact'
        AND external_id = $handle
        LIMIT 1
        """,
        {"handle": name_or_handle},
    )

    if by_handle and len(by_handle) > 0:
        contact = by_handle[0]
        contact_id = str(contact["id"])

        # Traverse the has_identity edge backwards to find the person
        person_rows = await query(
            db,
            """
            SELECT in FROM relates
            WHERE out = <record>$to_id
            AND relationship_type = 'has_identity'
            LIMIT 1
            """,
            {"to_id": contact_id},
        )

        if person_rows and len(person_rows) > 0:
            person_id = str(person_rows[0]["in"])
            _, person_suffix = person_id.split(":", 1)

            # Fetch the full person record
            person_record = await query(
                db,
                "SELECT * FROM type::record('entity', $id) LIMIT 1",
                {"id": person_suffix},
            )

            if person_record and len(person_record) > 0:
                person = person_record[0]

                # Find all linked contact IDs
                contact_rows = await query(
                    db,
                    """
                    SELECT out FROM relates
                    WHERE in = <record>$from_id
                    AND relationship_type = 'has_identity'
                    """,
                    {"from_id": person_id},
                )
                all_contact_ids = [str(row["out"]) for row in (contact_rows or [])]

                return {
                    "entity_id": person_id,
                    "name": person.get("name"),
                    "aliases": person.get("aliases") or [],
                    "contact_ids": all_contact_ids,
                }

    # Not found by any method
    return None
