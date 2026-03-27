# Database-Per-User Isolation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor Qmemory from owner-field multi-tenancy to database-per-user isolation, so each user's memories live in a physically separate SurrealDB database.

**Architecture:** Shared auth database (`qmemory/main`) holds `user` and `api_token` tables. Each user gets their own database (`qmemory/user_{id}`) with the full memory schema. The MCP auth middleware resolves the user from the Bearer token and routes `get_db()` calls to the user's database. Local stdio MCP continues using `qmemory/main` unchanged.

**Tech Stack:** SurrealDB 3.0, Python AsyncSurreal SDK, FastAPI, FastMCP

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `qmemory/db/client.py` | Modify | Add `_user_db` context var for per-request database routing |
| `qmemory/db/provision.py` | Create | `provision_user_db(user_id)` — creates database + applies schema |
| `qmemory/app/main.py` | Modify | MCPAuthMiddleware sets `_user_db` context var from token |
| `qmemory/app/routes/auth.py` | Modify | signup_submit() calls `provision_user_db()` after creating user |
| `qmemory/db/schema_cloud_permissions.surql` | Modify | Remove owner-field permissions (no longer needed per-user) |
| `tests/test_provision.py` | Create | Tests for database provisioning |
| `tests/test_db_routing.py` | Create | Tests for per-request database routing |

---

### Task 1: Add database routing context variable to `client.py` ✅ DONE

**Files:**
- Modify: `qmemory/db/client.py`
- Test: `tests/test_db_routing.py`

The core idea: a `contextvars.ContextVar` named `_user_db` holds the database name for the current request. `get_db()` reads it automatically. When not set (local MCP / tests), falls back to settings as before.

- [x] **Step 1: Write the failing test**

Create `tests/test_db_routing.py`:

```python
"""Tests for per-request database routing via _user_db context var."""

import pytest

from qmemory.db.client import _user_db, get_db, query


@pytest.fixture
async def user_db():
    """Create a temporary user database for testing."""
    async with get_db() as conn:
        await conn.query("DEFINE DATABASE IF NOT EXISTS user_test_routing")

    async with get_db(database="user_test_routing") as conn:
        await conn.query("""
            DEFINE TABLE IF NOT EXISTS memory SCHEMAFULL;
            DEFINE FIELD IF NOT EXISTS content ON memory TYPE string;
        """)
        yield "user_test_routing"

    async with get_db() as conn:
        await query(conn, "REMOVE DATABASE IF EXISTS user_test_routing")


async def test_user_db_context_var_routes_get_db(user_db):
    """When _user_db is set, get_db() connects to that database."""
    # Set the context var
    token = _user_db.set(user_db)
    try:
        async with get_db() as conn:
            await conn.query(
                "CREATE memory:routing_test SET content = 'routed'"
            )
            result = await query(conn, "SELECT content FROM memory:routing_test")
        assert result is not None
        assert result[0]["content"] == "routed"
    finally:
        _user_db.set(token)


async def test_default_db_without_context_var():
    """Without _user_db set, get_db() uses the default from settings."""
    # _user_db should be unset (default)
    async with get_db() as conn:
        result = await query(conn, "RETURN 1")
    assert result == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_db_routing.py -v`
Expected: FAIL with `ImportError: cannot import name '_user_db'`

- [ ] **Step 3: Add context var and modify get_db()**

Edit `qmemory/db/client.py`. Add the import and context var near the top (after the existing imports):

```python
from contextvars import ContextVar

# Per-request database override. Set by MCPAuthMiddleware to route each
# authenticated request to the user's private database (qmemory/user_{id}).
# When not set, get_db() falls back to settings.surreal_db (usually "main").
_user_db: ContextVar[str | None] = ContextVar("_user_db", default=None)
```

Then modify the `get_db()` function — change the line that sets `db_name`:

```python
    # Use provided overrides, or context var, or fall back to settings
    ns = namespace or settings.surreal_ns
    db_name = database or _user_db.get() or settings.surreal_db
```

This is a ONE-LINE change inside `get_db()`. The rest of the function stays identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_db_routing.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Run full test suite to verify nothing broke**

Run: `uv run pytest tests/ -x -q`
Expected: Same pass/fail count as before (130 pass, 9 known failures)

- [ ] **Step 6: Commit**

```bash
git add qmemory/db/client.py tests/test_db_routing.py
git commit -m "feat: add _user_db context var for per-request database routing"
```

---

### Task 2: Create database provisioning module ✅ DONE

**Files:**
- Create: `qmemory/db/provision.py`
- Test: `tests/test_provision.py`

This module creates a new SurrealDB database for a user and applies the memory schema to it.

- [x] **Step 1: Write the failing test**

Create `tests/test_provision.py`:

```python
"""Tests for user database provisioning."""

import pytest

from qmemory.db.client import get_db, query
from qmemory.db.provision import provision_user_db


@pytest.fixture
async def cleanup_test_db():
    """Cleanup: remove the test user database after the test."""
    yield "test_provision_user"
    async with get_db() as conn:
        await query(conn, "REMOVE DATABASE IF EXISTS user_test_provision_user")


async def test_provision_creates_database_with_schema(cleanup_test_db):
    """provision_user_db creates a database and applies the memory schema."""
    user_id = cleanup_test_db

    await provision_user_db(user_id)

    # Connect to the new database and verify tables exist
    async with get_db(database=f"user_{user_id}") as conn:
        info = await conn.query("INFO FOR DB;")
        tables = list(info["tables"].keys())

    assert "memory" in tables
    assert "entity" in tables
    assert "session" in tables
    assert "relates" in tables


async def test_provision_is_idempotent(cleanup_test_db):
    """Running provision_user_db twice doesn't error."""
    user_id = cleanup_test_db
    await provision_user_db(user_id)
    await provision_user_db(user_id)  # Should not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_provision.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'qmemory.db.provision'`

- [ ] **Step 3: Create provision.py**

Create `qmemory/db/provision.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_provision.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add qmemory/db/provision.py tests/test_provision.py
git commit -m "feat: add provision_user_db() for database-per-user creation"
```

---

### Task 3: Wire MCPAuthMiddleware to set `_user_db` context var ✅ DONE

**Files:**
- Modify: `qmemory/app/main.py`

The auth middleware already validates the Bearer token and gets the user dict. Now it also needs to:
1. Extract the user ID from the token record
2. Set `_user_db` to `user_{id}` so all `get_db()` calls in that request go to the user's database

- [x] **Step 1: Add import to main.py**

At the top of `qmemory/app/main.py`, add to the existing imports:

```python
from qmemory.db.client import _user_db
```

- [ ] **Step 2: Modify MCPAuthMiddleware to set context var**

In the `MCPAuthMiddleware.__call__` method, after the `user is None` check (after the "Token valid" comment), add the context var before calling the app:

```python
        # Token valid — route to user's private database
        user_id = user.get("id", "")
        if ":" in str(user_id):
            user_id = str(user_id).split(":")[-1]

        if user_id:
            token = _user_db.set(f"user_{user_id}")
            try:
                await self.app(scope, receive, send)
            finally:
                _user_db.set(token)
            return

        # Fallback: valid token but no user ID — pass through to default DB
        await self.app(scope, receive, send)
```

Replace the existing final line `await self.app(scope, receive, send)` in the middleware with this block.

- [ ] **Step 3: Test manually with curl**

After deploying, test that the middleware sets the database correctly:

```bash
# With valid token — should route to user's database
curl -s -X POST https://qmemory-api-production.up.railway.app/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "qmemory_bootstrap", "arguments": {"session_key": "test"}}, "id": 1}'
```

Expected: Should return empty context (new database, no memories yet) rather than seeing shared data.

- [ ] **Step 4: Commit**

```bash
git add qmemory/app/main.py
git commit -m "feat: MCPAuthMiddleware routes MCP calls to user's private database"
```

---

### Task 4: Provision user database on signup ✅ DONE

**Files:**
- Modify: `qmemory/app/routes/auth.py`

When a new user signs up, create their private database immediately.

- [x] **Step 1: Add import to auth.py**

Add at the top of `qmemory/app/routes/auth.py`:

```python
from qmemory.db.provision import provision_user_db
```

- [ ] **Step 2: Add provisioning after successful signup**

In the `signup_submit()` function, right after the `logger.info("auth.signup_success email=%s", email)` line (line 384), add:

```python
        # Create the user's private database with full memory schema
        try:
            await provision_user_db(user_info["user_id"])
            logger.info("auth.user_db_provisioned user_id=%s", user_info["user_id"])
        except Exception as exc:
            logger.error(
                "auth.user_db_provision_failed user_id=%s reason=%s",
                user_info["user_id"],
                exc,
            )
            # Don't fail signup — user can still use the web UI
            # Database can be provisioned later on first MCP call
```

This goes AFTER the JWT decoding and user_info extraction (around line 394), but BEFORE storing in session.

- [ ] **Step 3: Test by signing up a new user**

Visit `https://mem0.qusai.org/signup`, create an account, then verify the database was created:

```bash
echo "INFO FOR NS;" | surreal sql \
  -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" \
  --namespace qmemory --database main
```

Expected: Should show `user_{id}` database in the namespace info.

- [ ] **Step 4: Commit**

```bash
git add qmemory/app/routes/auth.py
git commit -m "feat: provision user database on signup"
```

---

### Task 5: Simplify permissions (remove owner-field isolation) ✅ DONE

**Files:**
- Modify: `qmemory/db/schema_cloud_permissions.surql`

With database-per-user, we no longer need `WHERE owner = $auth` permissions. Each database is already isolated. Simplify all tables to `PERMISSIONS FULL` (any authenticated connection can read/write within their own database).

- [x] **Step 1: Rewrite the permissions file**

Replace the contents of `qmemory/db/schema_cloud_permissions.surql`:

```sql
-- ============================================================
-- Qmemory Cloud — Simplified Permissions (Database-Per-User)
-- SurrealDB 3.0+
--
-- With database-per-user isolation, each user has their own
-- database. No owner-field filtering needed — all tables get
-- FULL permissions since the database itself IS the boundary.
--
-- This file is applied to qmemory/main (shared auth DB) only.
-- User databases get schema.surql (no permissions needed).
-- ============================================================

-- Auth tables in the shared database (qmemory/main):

-- USER: can only read own record
DEFINE TABLE OVERWRITE user TYPE NORMAL SCHEMAFULL
  PERMISSIONS
    FOR select WHERE id = $auth
    FOR create NONE
    FOR update WHERE id = $auth
    FOR delete NONE;

-- API_TOKEN: can only see own tokens
DEFINE TABLE OVERWRITE api_token TYPE NORMAL SCHEMAFULL
  PERMISSIONS
    FOR select WHERE user = $auth
    FOR create FULL
    FOR update WHERE user = $auth
    FOR delete WHERE user = $auth;

-- SCHEMA_VERSION: admin only
DEFINE TABLE OVERWRITE schema_version TYPE NORMAL SCHEMAFULL PERMISSIONS NONE;
```

- [x] **Step 2: Apply to Railway**

```bash
surreal import \
  -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" \
  --namespace qmemory --database main \
  qmemory/db/schema_cloud_permissions.surql
```

Expected: `Import executed with no errors`

- [x] **Step 3: Commit**

```bash
git add qmemory/db/schema_cloud_permissions.surql
git commit -m "refactor: simplify permissions for database-per-user model"
```

---

### Task 6: End-to-end test — full user lifecycle ⏳ DEPLOYED, MANUAL TESTING PENDING

**Files:** None (manual test)

This verifies the complete flow: signup → provision → token → MCP → isolated data.

- [x] **Step 1: Deploy all changes**

```bash
railway up --service qmemory-api --detach
```

- [ ] **Step 2: Create User A via signup page**

Visit `https://mem0.qusai.org/signup` and create Alice (alice@test.com).

- [ ] **Step 3: Generate API token for User A**

Visit `https://mem0.qusai.org/tokens` and generate a token. Save it.

- [ ] **Step 4: Save a memory as User A via MCP**

```bash
curl -s -X POST https://mem0.qusai.org/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $ALICE_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"qmemory_save","arguments":{"content":"Alice secret","category":"context"}},"id":1}'
```

Expected: Memory saved in Alice's private database.

- [ ] **Step 5: Create User B and verify isolation**

Repeat signup + token for Bob. Then search for Alice's memory:

```bash
curl -s -X POST https://mem0.qusai.org/mcp/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Authorization: Bearer $BOB_TOKEN" \
  -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"qmemory_search","arguments":{"query":"Alice secret"}},"id":2}'
```

Expected: `{"results": []}` — Bob cannot see Alice's data.

- [ ] **Step 6: Verify root sees all databases**

```bash
echo "INFO FOR NS;" | surreal sql \
  -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" \
  --namespace qmemory --database main
```

Expected: Shows `main`, `user_alice_id`, `user_bob_id` databases.

- [ ] **Step 7: Cleanup test users**

```bash
# As root — remove test databases and users
surreal sql -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" \
  --namespace qmemory --database main <<< "
    DELETE user WHERE email IN ['alice@test.com', 'bob@test.com'];
    DELETE api_token WHERE name = 'Test Token';
    REMOVE DATABASE IF EXISTS user_alice_id;
    REMOVE DATABASE IF EXISTS user_bob_id;
"
```

- [ ] **Step 8: Commit and update CLAUDE.md**

Update CLAUDE.md to reflect the database-per-user architecture, then commit all changes.

---

## What Stays Unchanged

- **Local stdio MCP** (`qmemory serve`) — still connects to `qmemory/main` as root. `_user_db` is never set in stdio mode, so `get_db()` falls back to settings.
- **All 19 core modules** (`core/*.py`) — they call `get_db()` which now reads `_user_db` automatically. Zero changes to core logic.
- **All existing tests** — they use `qmemory_test` namespace override, which takes priority over `_user_db`.
- **schema.surql** — applied identically to each user's database. No changes.
- **Backup per-user**: `surreal export --namespace qmemory --database user_{id} backup.surql`
- **Delete user**: `REMOVE DATABASE user_{id}` + `DELETE user WHERE id = user:{id}`
