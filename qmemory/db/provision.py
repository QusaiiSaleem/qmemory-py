"""
User Database Provisioning

Creates a private SurrealDB database for a new user and applies the
memory schema. Each user gets their own database: qmemory/user_{id}.

Usage:
    await provision_user_db("abc123")
    # Creates database "user_abc123" in namespace "qmemory"
    # Applies schema.surql so all memory tables are ready
"""

from __future__ import annotations

import logging

from qmemory.db.client import apply_schema, get_db

logger = logging.getLogger(__name__)


async def provision_user_db(user_id: str) -> str:
    """
    Create a private database for a user and apply the memory schema.

    Args:
        user_id: The user's ID (just the ID part, e.g. "abc123").
                 The database will be named "user_{user_id}".

    Returns:
        The database name (e.g. "user_abc123").
    """
    # Sanitize: remove "user:" prefix if present (from RecordID)
    if ":" in user_id:
        user_id = user_id.split(":")[-1]

    db_name = f"user_{user_id}"

    logger.info("Provisioning database %s for user %s", db_name, user_id)

    # Step 1: Create the database (idempotent via IF NOT EXISTS)
    async with get_db() as conn:
        await conn.query(f"DEFINE DATABASE IF NOT EXISTS {db_name}")

    # Step 2: Connect to the new database and apply schema
    async with get_db(database=db_name) as conn:
        await apply_schema(conn)

    logger.info("Database %s provisioned successfully", db_name)
    return db_name
