"""
Qmemory Database Client

Provides all the low-level functions for talking to SurrealDB:
- get_db()       — async context manager that gives you a fresh connection
- query()        — run a parameterized SurrealQL query (with graceful error handling)
- normalize_ids() — convert SurrealDB RecordID objects to plain strings
- is_healthy()   — check if SurrealDB is reachable
- generate_id()  — create a timestamp-based, SurrealDB-safe ID
- apply_schema() — run the schema.surql file against the database

IMPORTANT DESIGN DECISION: Every call to get_db() creates a NEW connection.
This is intentional — reusing async SurrealDB connections causes "No iterator"
bugs. The connection is closed automatically when the context manager exits.
"""

from __future__ import annotations

import logging
import random
import string
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from surrealdb import AsyncSurreal, RecordID

from qmemory.config import get_settings

# Logger for this module — all warnings/errors go here
logger = logging.getLogger(__name__)

# Per-request database override. Set by MCPAuthMiddleware to route each
# authenticated request to the user's private database (qmemory/user_{id}).
# When not set, get_db() falls back to settings.surreal_db (usually "main").
_user_db: ContextVar[str | None] = ContextVar("_user_db", default=None)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_db(namespace: str | None = None, database: str | None = None):
    """
    Async context manager that yields a fresh SurrealDB connection.

    Creates a brand-new WebSocket connection each time (no pooling).
    Signs in with credentials from settings, selects namespace/database,
    and closes the connection when done.

    Args:
        namespace: Override the default namespace (from settings).
                   Useful for tests that use a separate "qmemory_test" namespace.
        database:  Override the default database name (from settings).

    Usage:
        async with get_db() as db:
            result = await query(db, "SELECT * FROM memory LIMIT 5")
    """
    settings = get_settings()

    # Use provided overrides, or context var, or fall back to settings
    ns = namespace or settings.surreal_ns
    db_name = database or _user_db.get() or settings.surreal_db

    # Create a fresh connection to the SurrealDB WebSocket endpoint
    db = AsyncSurreal(settings.surreal_url)

    try:
        # Step 1: Open the WebSocket connection
        await db.connect()

        # Step 2: Authenticate with username/password
        await db.signin({
            "username": settings.surreal_user,
            "password": settings.surreal_pass,
        })

        # Step 3: Select the namespace and database to work with
        await db.use(ns, db_name)

        # Hand the ready-to-use connection to the caller
        yield db
    finally:
        # Step 4: Always close the connection, even if an error occurred
        try:
            await db.close()
        except Exception:
            # If close() fails (e.g. connection already dropped), ignore it
            pass


@asynccontextmanager
async def get_admin_db(database: str = "admin"):
    """Connect to the qmemory.admin database (never uses _user_db).

    This helper exists so the signup flow, admin CLI, MCP user middleware,
    and worker loop can all reach the admin routing table without getting
    routed to a per-user DB by accident.

    Args:
        database: Usually "admin". Pass a different name in tests
                  (e.g. "admin_test") to avoid stomping production.
    """
    settings = get_settings()
    db = AsyncSurreal(settings.surreal_url)
    try:
        await db.connect()
        await db.signin({
            "username": settings.surreal_user,
            "password": settings.surreal_pass,
        })
        await db.use(settings.surreal_ns, database)
        yield db
    finally:
        try:
            await db.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def query(
    db: Any,
    surql: str,
    params: dict[str, Any] | None = None,
) -> Any | None:
    """
    Execute a parameterized SurrealQL query and return normalized results.

    This is the main function all core modules use to talk to the database.
    It wraps the raw SDK call with two important features:

    1. **Parameterized queries** — params dict is passed directly to the SDK,
       preventing SurrealQL injection. Never string-interpolate values!

    2. **Graceful degradation** — if the query fails for ANY reason (connection
       dropped, bad syntax, SurrealDB down), it logs a warning and returns None
       instead of crashing. This lets the rest of Qmemory keep working in
       degraded mode.

    Args:
        db:     An active SurrealDB connection (from get_db()).
        surql:  The SurrealQL query string with $param placeholders.
        params: Dict of parameter names → values (optional).

    Returns:
        The query result with all RecordID objects converted to strings,
        or None if an error occurred.

    Example:
        results = await query(db, "SELECT * FROM memory WHERE category = $cat", {"cat": "context"})
    """
    try:
        # Execute the query — the SDK handles parameter binding
        result = await db.query(surql, params)

        # Convert any RecordID objects in the result to plain strings
        return normalize_ids(result)

    except Exception as e:
        # Log the error but don't crash — graceful degradation
        logger.warning("SurrealDB query failed: %s | Query: %s", e, surql[:200])
        return None


async def query_multi(
    db: Any,
    surql: str,
    params: dict[str, Any] | None = None,
) -> list[Any]:
    """
    Execute a multi-statement SurrealQL query and return ALL result sets.

    Unlike query() which returns the normalized result as-is, this function
    ensures you always get a list — one entry per statement in the query.
    Useful when a query has LET + RELATE + SELECT and you need the Nth result.

    Returns an empty list on error (graceful degradation).
    """
    result = await query(db, surql, params)
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


# ---------------------------------------------------------------------------
# RecordID normalization
# ---------------------------------------------------------------------------


def normalize_ids(data: Any) -> Any:
    """
    Recursively convert SurrealDB RecordID objects to "table:id" strings.

    The SurrealDB Python SDK returns RecordID objects for record fields
    (like `id`, `session`, `source_person`, etc.). These aren't JSON-serializable
    and are awkward to work with. This function walks through the entire result
    and converts them to simple strings like "memory:1710864000000abc".

    Works on:
    - Single RecordID → "table:id"
    - Dicts → recurse into all values
    - Lists → recurse into all items
    - Everything else → returned as-is

    Args:
        data: Any value returned from a SurrealDB query.

    Returns:
        The same data structure with RecordID objects replaced by strings.
    """
    # Check if this is a RecordID — it has table_name and id attributes
    if isinstance(data, RecordID):
        return f"{data.table_name}:{data.id}"

    # Recurse into dicts
    if isinstance(data, dict):
        return {k: normalize_ids(v) for k, v in data.items()}

    # Recurse into lists
    if isinstance(data, list):
        return [normalize_ids(item) for item in data]

    # Everything else (str, int, float, bool, None, datetime, etc.) — pass through
    return data


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


async def is_healthy() -> bool:
    """
    Check if SurrealDB is reachable and responding.

    Tries to connect, sign in, and execute a simple query.
    Returns True if everything works, False if anything fails.

    This is used by the CLI `status` command and health endpoints.
    """
    try:
        async with get_db() as db:
            result = await query(db, "RETURN 1")
            return result == 1
    except Exception:
        return False


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------


def generate_id(prefix: str) -> str:
    """
    Create a timestamp-based, SurrealDB-safe ID.

    Format: {prefix}{timestamp_ms}{random_chars}
    Example: mem1710864000000abc

    - No dashes (SurrealDB doesn't like them in unquoted IDs)
    - Timestamp ensures rough ordering
    - Random suffix prevents collisions

    Args:
        prefix: Short string like "mem", "ent", "ses", "msg"

    Returns:
        A unique ID string safe for use as a SurrealDB record ID.
    """
    # Current time in milliseconds since epoch
    timestamp_ms = int(time.time() * 1000)

    # 3 random lowercase letters for collision avoidance
    random_suffix = "".join(random.choices(string.ascii_lowercase, k=3))

    return f"{prefix}{timestamp_ms}{random_suffix}"


# ---------------------------------------------------------------------------
# Schema application
# ---------------------------------------------------------------------------


async def apply_schema(db: Any) -> None:
    """
    Execute all schema files against the connected database.

    Loads schema files in order: base memory schema first, then cloud
    and OAuth extensions. All schemas use "IF NOT EXISTS" / "OVERWRITE"
    so they're safe to run multiple times (idempotent).

    Args:
        db: An active SurrealDB connection (from get_db()).

    Raises:
        FileNotFoundError: If any schema file is missing from the package.
    """
    # Find schema files relative to THIS file (client.py is in qmemory/db/)
    schema_dir = Path(__file__).parent

    # Order matters: base schema first, then extensions that depend on it
    schema_files = [
        "schema.surql",          # Base memory graph (tables, indices)
        "schema_cloud.surql",    # Cloud: user table, api_token, owner fields
        "schema_oauth.surql",    # OAuth 2.0: oauth_client, authorization_code
    ]

    for filename in schema_files:
        schema_path = schema_dir / filename
        if not schema_path.exists():
            raise FileNotFoundError(
                f"Schema file not found at {schema_path}. "
                f"Make sure qmemory/db/{filename} exists in the package."
            )
        schema_sql = schema_path.read_text(encoding="utf-8")
        logger.info("Applying schema from %s", schema_path)
        await db.query(schema_sql)

    logger.info("Schema applied successfully")


async def apply_admin_schema(db: Any) -> None:
    """Apply the admin schema (user table only) to the connected database.

    Used by the multi-user setup to provision the tiny routing directory
    at qmemory.admin. Does NOT apply the base memory schema — admin is
    a minimal directory, not a memory graph.

    Args:
        db: An active SurrealDB connection (from get_admin_db()).
    """
    schema_path = Path(__file__).parent / "admin_schema.surql"
    if not schema_path.exists():
        raise FileNotFoundError(f"admin_schema.surql not found at {schema_path}")

    surql = schema_path.read_text(encoding="utf-8")
    logger.info("Applying admin schema from %s", schema_path.name)
    await db.query(surql)
