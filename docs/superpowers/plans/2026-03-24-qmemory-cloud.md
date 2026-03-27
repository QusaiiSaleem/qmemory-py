# Qmemory Cloud Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform Qmemory from a local single-user tool into a multi-user cloud service on Railway with HTMX dashboard, remote MCP, and background workers.

**Architecture:** FastAPI monolith with FastMCP mounted at `/mcp/`. SurrealDB handles auth + row-level permissions. Three Railway services: SurrealDB, API, Worker. HTMX dashboard for humans, MCP for AI agents.

**Tech Stack:** FastAPI, FastMCP, SurrealDB v3, HTMX, Jinja2, Tailwind (CDN), Railway, httpx

**Spec:** `docs/superpowers/specs/2026-03-24-qmemory-cloud-design.md`

---

## Phases Overview

| Phase | What | Produces | Independent? |
|-------|------|----------|-------------|
| 1 | Schema + Auth + Multi-user DB | Updated schema, user table, API tokens, owner field | Yes |
| 2 | HTTP MCP Server | FastAPI + FastMCP on `/mcp/`, auth middleware | Needs Phase 1 |
| 3 | HTMX Dashboard | Login, signup, connect page, memory browser | Needs Phase 2 |
| 4 | Background Worker | Linker, reflector, salience decay | Needs Phase 1 |
| 5 | Railway Deployment | Dockerfile, railway.json, data migration | Needs Phase 2 |
| 6 | NanoBot Remote | memory.py HTTP fallback in nanobot-fork | Needs Phase 5 |

---

## Phase 1: Schema + Auth Foundation

### Task 1: Multi-User Schema

**Files:**
- Modify: `qmemory/db/schema.surql`
- Create: `qmemory/db/schema_cloud.surql` (cloud-specific additions)
- Test: Manual via `surreal sql`

- [ ] **Step 1: Create cloud schema file**

Create `qmemory/db/schema_cloud.surql` with user table, API token table, access definition, and owner fields:

```sql
-- User table
DEFINE TABLE IF NOT EXISTS user SCHEMAFULL;
DEFINE FIELD email ON user TYPE string;
DEFINE FIELD password ON user TYPE string;
DEFINE FIELD name ON user TYPE string;
DEFINE FIELD created_at ON user TYPE datetime DEFAULT time::now();
DEFINE INDEX email_unique ON user FIELDS email UNIQUE;

-- Access definition (signup/signin)
DEFINE ACCESS qmemory_user ON DATABASE TYPE RECORD
  SIGNUP (
    CREATE user CONTENT {
      email: $email,
      password: crypto::argon2::generate($password),
      name: $name,
      created_at: time::now()
    }
  )
  SIGNIN (
    SELECT * FROM user
    WHERE email = $email
    AND crypto::argon2::compare(password, $password)
  )
  DURATION FOR TOKEN 24h, FOR SESSION 7d;

-- API token table
DEFINE TABLE IF NOT EXISTS api_token SCHEMAFULL;
DEFINE FIELD user ON api_token TYPE record<user>;
DEFINE FIELD token_hash ON api_token TYPE string;
DEFINE FIELD prefix ON api_token TYPE string;
DEFINE FIELD name ON api_token TYPE string DEFAULT 'Default';
DEFINE FIELD created_at ON api_token TYPE datetime DEFAULT time::now();
DEFINE FIELD expires_at ON api_token TYPE datetime;
DEFINE FIELD last_used ON api_token TYPE option<datetime>;
DEFINE INDEX token_hash_idx ON api_token FIELDS token_hash UNIQUE;

-- Owner field on existing tables
DEFINE FIELD owner ON memory TYPE option<record<user>>;
DEFINE FIELD owner ON entity TYPE option<record<user>>;

-- Linker tracking
DEFINE FIELD linked ON memory TYPE bool DEFAULT false;
DEFINE INDEX linked_idx ON memory FIELDS linked;

-- Recall tracking (for decay)
DEFINE FIELD recall_count ON memory TYPE int DEFAULT 0;
DEFINE FIELD last_recalled ON memory TYPE option<datetime>;

-- Row-level permissions
DEFINE TABLE memory SCHEMAFULL
  PERMISSIONS
    FOR select, update, delete WHERE owner = $auth.id OR owner = NONE
    FOR create WHERE $auth.id != NONE OR $auth = NONE;

DEFINE TABLE entity SCHEMAFULL
  PERMISSIONS
    FOR select, update, delete WHERE owner = $auth.id OR owner = NONE
    FOR create WHERE $auth.id != NONE OR $auth = NONE;

DEFINE TABLE relates SCHEMAFULL TYPE RELATION
  PERMISSIONS
    FOR select, update, delete WHERE in.owner = $auth.id OR in.owner = NONE
    FOR create WHERE $auth.id != NONE OR $auth = NONE;
```

Note: `OR owner = NONE` allows backward compatibility with existing unowned records.

- [ ] **Step 2: Apply schema to local SurrealDB and verify**

```bash
surreal sql --endpoint http://localhost:8000 --username root --password root \
  --namespace qmemory --database main < qmemory/db/schema_cloud.surql
```

Verify: `INFO FOR DB;` should show `user`, `api_token` tables and `qmemory_user` access.

- [ ] **Step 3: Commit**

```bash
git add qmemory/db/schema_cloud.surql
git commit -m "feat: add multi-user schema with SurrealDB auth and row-level permissions"
```

### Task 2: Auth Helper Functions

**Files:**
- Create: `qmemory/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests for auth helpers**

```python
# tests/test_auth.py
import pytest
from qmemory.auth import generate_api_token, hash_token, verify_token_format

def test_generate_api_token_format():
    token = generate_api_token()
    assert token.startswith("qm_ak_")
    assert len(token) == 38  # "qm_ak_" (6) + 32 random chars

def test_hash_token_deterministic():
    token = "qm_ak_abcdef1234567890abcdef1234567890ab"
    h1 = hash_token(token)
    h2 = hash_token(token)
    assert h1 == h2
    assert h1 != token  # not plaintext

def test_verify_token_format_valid():
    assert verify_token_format("qm_ak_abcdef1234567890abcdef1234567890ab") is True

def test_verify_token_format_invalid():
    assert verify_token_format("invalid_token") is False
    assert verify_token_format("") is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_auth.py -v
```

- [ ] **Step 3: Implement auth helpers**

```python
# qmemory/auth.py
"""Auth helpers for Qmemory Cloud — token generation, hashing, validation."""
from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "qm_ak_"
TOKEN_RANDOM_LENGTH = 32

def generate_api_token() -> str:
    """Generate a new API token: qm_ak_{32 random hex chars}."""
    return f"{TOKEN_PREFIX}{secrets.token_hex(TOKEN_RANDOM_LENGTH // 2)}"

def hash_token(token: str) -> str:
    """SHA-256 hash of the token for storage. Never store plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()

def get_token_prefix(token: str) -> str:
    """Extract the prefix shown to the user (first 10 chars)."""
    return token[:10]

def verify_token_format(token: str) -> bool:
    """Check if a string looks like a valid Qmemory API token."""
    return (
        isinstance(token, str)
        and token.startswith(TOKEN_PREFIX)
        and len(token) == len(TOKEN_PREFIX) + TOKEN_RANDOM_LENGTH
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_auth.py -v
```

- [ ] **Step 5: Commit**

```bash
git add qmemory/auth.py tests/test_auth.py
git commit -m "feat: add API token generation and hashing helpers"
```

### Task 3: Update Core Functions for Owner Support

**Files:**
- Modify: `qmemory/core/save.py`
- Modify: `qmemory/core/search.py`
- Modify: `qmemory/core/recall.py`

The core functions need an optional `owner_id` parameter. When provided, memories are created with that owner. When not provided (local mode), no owner is set.

- [ ] **Step 1: Add `owner_id` parameter to `save_memory()`**

In `qmemory/core/save.py`, add `owner_id: str | None = None` parameter. When building the CREATE query, include `owner = type::record('user', $owner_id)` if owner_id is set.

- [ ] **Step 2: Add `owner_id` to `search_memories()` and `assemble_context()`**

Same pattern — pass owner_id through to SurrealQL WHERE clauses. When using SurrealDB auth (cloud mode), the row-level permissions handle this automatically, but for root connections (worker), we need explicit filtering.

- [ ] **Step 3: Run existing tests to verify nothing breaks**

```bash
uv run pytest tests/ -v
```

All existing tests should still pass (owner_id defaults to None).

- [ ] **Step 4: Commit**

```bash
git add qmemory/core/save.py qmemory/core/search.py qmemory/core/recall.py
git commit -m "feat: add optional owner_id parameter to core functions"
```

---

## Phase 2: HTTP MCP Server (FastAPI + FastMCP)

### Task 4: FastAPI App with FastMCP Mount

**Files:**
- Create: `qmemory/app/__init__.py`
- Create: `qmemory/app/main.py`
- Create: `qmemory/app/config.py`
- Modify: `pyproject.toml` (add fastmcp, fastapi, jinja2 deps)

- [ ] **Step 1: Add dependencies to pyproject.toml**

Add to `[project.dependencies]`:
```
"fastapi[all]>=0.115",
"fastmcp>=2.0",
"jinja2>=3.1",
"python-multipart>=0.0.9",
```

Add optional dependency:
```
[project.optional-dependencies]
cloud = ["slowapi>=0.1"]
```

- [ ] **Step 2: Create app config**

```python
# qmemory/app/config.py
from __future__ import annotations
from pydantic_settings import BaseSettings

class AppSettings(BaseSettings):
    surreal_url: str = "ws://localhost:8000"
    surreal_user: str = "root"
    surreal_pass: str = "root"
    surreal_ns: str = "qmemory"
    surreal_db: str = "main"
    secret_key: str = "change-me-in-production"
    debug: bool = False

    model_config = {"env_prefix": "QMEMORY_", "env_file": ".env"}
```

- [ ] **Step 3: Create main.py with FastAPI + FastMCP**

```python
# qmemory/app/main.py
from __future__ import annotations

from fastapi import FastAPI
from fastmcp import FastMCP

from qmemory.app.config import AppSettings

settings = AppSettings()

# MCP server
mcp = FastMCP(
    "qmemory",
    instructions=(
        "Graph memory for AI agents. Call qmemory_bootstrap first to load "
        "your full memory context. Then use qmemory_search, qmemory_save, "
        "qmemory_correct, qmemory_link, and qmemory_person."
    ),
)

# Register tools (reuse existing core functions)
@mcp.tool
async def qmemory_bootstrap(session_key: str = "default") -> str:
    """Load full memory context at conversation start."""
    from qmemory.core.recall import assemble_context
    return await assemble_context(session_key)

@mcp.tool
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 10,
) -> str:
    """Search cross-session memory."""
    import json
    from qmemory.core.search import search_memories
    results = await search_memories(query_text=query, category=category, scope=scope, limit=limit)
    return json.dumps(results, default=str, ensure_ascii=False)

@mcp.tool
async def qmemory_save(
    content: str,
    category: str,
    salience: float = 0.5,
    scope: str = "global",
    confidence: float = 0.8,
    source_person: str | None = None,
    evidence_type: str = "observed",
    context_mood: str | None = None,
) -> str:
    """Save a fact with evidence tracking + auto-dedup."""
    import json
    from qmemory.core.save import save_memory
    result = await save_memory(
        content=content, category=category, salience=salience, scope=scope,
        confidence=confidence, source_person=source_person,
        evidence_type=evidence_type, context_mood=context_mood,
    )
    return json.dumps(result, default=str, ensure_ascii=False)

@mcp.tool
async def qmemory_correct(
    memory_id: str, action: str,
    new_content: str | None = None, updates: dict | None = None,
    edge_id: str | None = None, reason: str | None = None,
) -> str:
    """Fix, delete, update, or unlink a memory."""
    import json
    from qmemory.core.correct import correct_memory
    result = await correct_memory(
        memory_id=memory_id, action=action, new_content=new_content,
        updates=updates, edge_id=edge_id, reason=reason,
    )
    return json.dumps(result, default=str, ensure_ascii=False)

@mcp.tool
async def qmemory_link(
    from_id: str, to_id: str, relationship_type: str,
    reason: str | None = None, confidence: float | None = None,
) -> str:
    """Create a relationship edge between any two nodes."""
    import json
    from qmemory.core.link import link_nodes
    result = await link_nodes(
        from_id=from_id, to_id=to_id, relationship_type=relationship_type,
        reason=reason, confidence=confidence,
    )
    return json.dumps(result, default=str, ensure_ascii=False)

@mcp.tool
async def qmemory_person(
    name: str, aliases: list[str] | None = None,
    contacts: list[dict] | None = None,
) -> str:
    """Create or find a person entity."""
    import json
    from qmemory.core.person import create_person
    result = await create_person(name=name, aliases=aliases, contacts=contacts)
    return json.dumps(result, default=str, ensure_ascii=False)

# FastAPI app
mcp_app = mcp.http_app(path="/")
api = FastAPI(title="Qmemory", lifespan=mcp_app.lifespan)
api.mount("/mcp", mcp_app)

@api.get("/health")
async def health():
    from qmemory.db.client import is_healthy
    db_ok = await is_healthy()
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}
```

- [ ] **Step 4: Test locally**

```bash
uvicorn qmemory.app.main:api --reload --port 3777
# In another terminal:
curl http://localhost:3777/health
curl http://localhost:3777/mcp/ -X POST -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

- [ ] **Step 5: Commit**

```bash
git add qmemory/app/ pyproject.toml
git commit -m "feat: add FastAPI app with FastMCP mounted at /mcp/"
```

### Task 5: MCP Auth Middleware

**Files:**
- Create: `qmemory/app/auth.py`
- Modify: `qmemory/app/main.py`

- [ ] **Step 1: Create token validation middleware**

```python
# qmemory/app/auth.py
from __future__ import annotations

from fastapi import Request, HTTPException
from qmemory.auth import hash_token, verify_token_format
from qmemory.db.client import get_db, query

async def resolve_token(request: Request) -> dict | None:
    """Extract and validate API token from Authorization header.
    Returns user dict or None (for unauthenticated access in local mode).
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None

    token = auth[7:]
    if not verify_token_format(token):
        raise HTTPException(401, "Invalid token format")

    token_hash = hash_token(token)
    async with get_db() as db:
        result = await query(db,
            "SELECT user.* AS user, id FROM api_token "
            "WHERE token_hash = $hash AND expires_at > time::now()",
            {"hash": token_hash},
        )
        if not result or not result[0]:
            raise HTTPException(401, "Token expired or invalid")

        # Update last_used (fire-and-forget)
        token_id = result[0].get("id")
        await query(db,
            "UPDATE $id SET last_used = time::now()",
            {"id": token_id},
        )
        return result[0].get("user")
```

- [ ] **Step 2: Commit**

```bash
git add qmemory/app/auth.py
git commit -m "feat: add API token validation middleware"
```

---

## Phase 3: HTMX Dashboard

### Task 6: Base Template + Auth Routes

**Files:**
- Create: `qmemory/app/templates/base.html`
- Create: `qmemory/app/templates/pages/login.html`
- Create: `qmemory/app/templates/pages/signup.html`
- Create: `qmemory/app/routes/__init__.py`
- Create: `qmemory/app/routes/auth.py`

- [ ] **Step 1: Create RTL base template**

Use `base-rtl.html` from Hotwire skill as reference. Include hx-boost, Tailwind CDN, DaisyUI, Cairo font.

- [ ] **Step 2: Create login/signup pages**

Simple forms with HTMX POST. Login calls SurrealDB SIGNIN, signup calls SIGNUP. Session stored in signed cookie.

- [ ] **Step 3: Create auth routes**

```python
# qmemory/app/routes/auth.py
@router.get("/login")
@router.post("/login")
@router.get("/signup")
@router.post("/signup")
@router.post("/logout")
```

- [ ] **Step 4: Test signup → login → session flow**
- [ ] **Step 5: Commit**

### Task 7: Connect Page

**Files:**
- Create: `qmemory/app/templates/pages/connect.html`
- Create: `qmemory/app/routes/connect.py`

- [ ] **Step 1: Create connect page template**

Three tabs: Claude Code, Claude.ai, NanoBot. Each shows copy-paste ready config with the user's token pre-filled. JavaScript copy-to-clipboard on the code blocks.

- [ ] **Step 2: Create connect route**

```python
@router.get("/connect")
async def connect_page(request: Request, user = Depends(require_auth)):
    tokens = await get_user_tokens(user["id"])
    return templates.TemplateResponse("pages/connect.html", {
        "request": request, "user": user, "tokens": tokens,
        "mcp_url": settings.public_url + "/mcp/",
    })
```

- [ ] **Step 3: Test — login, go to /connect, see config blocks**
- [ ] **Step 4: Commit**

### Task 8: Token Management Page

**Files:**
- Create: `qmemory/app/templates/pages/tokens.html`
- Create: `qmemory/app/routes/tokens.py`

- [ ] **Step 1: Create token management routes**

```python
@router.get("/tokens")           # List tokens
@router.post("/tokens/generate") # Generate new token (shows it ONCE)
@router.delete("/tokens/{id}")   # Revoke a token
```

- [ ] **Step 2: Create token page template with HTMX**
- [ ] **Step 3: Test — generate token, see prefix, copy full token, revoke**
- [ ] **Step 4: Commit**

### Task 9: Dashboard + Memory Browser

**Files:**
- Create: `qmemory/app/templates/pages/dashboard.html`
- Create: `qmemory/app/templates/pages/memories.html`
- Create: `qmemory/app/templates/partials/memory_card.html`
- Create: `qmemory/app/templates/partials/search_results.html`
- Create: `qmemory/app/routes/dashboard.py`
- Create: `qmemory/app/routes/memories.py`

- [ ] **Step 1: Dashboard with stats cards**

Three cards: memory count, entity count, links count. Recent activity list.

- [ ] **Step 2: Memory browser with active search**

HTMX active search (`hx-get="/memories/search" hx-trigger="input changed delay:300ms"`), category filter, salience sort.

- [ ] **Step 3: Memory detail page**

Shows content, category, salience, evidence chain, linked nodes.

- [ ] **Step 4: Test all pages**
- [ ] **Step 5: Commit**

---

## Phase 4: Background Worker

### Task 10: Salience Decay (Zero LLM Cost)

**Files:**
- Create: `qmemory/core/decay.py`
- Create: `tests/test_core/test_decay.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_core/test_decay.py
async def test_decay_never_recalled():
    """Memories never recalled and >7 days old lose 10% salience."""

async def test_decay_stale():
    """Memories recalled but >14 days stale lose 2% salience."""

async def test_cemented_no_decay():
    """Memories with recall_count >= 5 never decay below floor."""

async def test_recall_boost():
    """Recalling a memory bumps salience by 0.05, capped at 1.0."""
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement decay**

```python
# qmemory/core/decay.py
async def run_salience_decay(db=None) -> dict:
    """Apply 3-tier biological memory decay. Pure DB — zero LLM cost."""
    # Tier 1: Never recalled, older than 7 days
    # Tier 2: Recalled but stale > 14 days
    # Tier 3: Cemented (recall_count >= 5) — skip
    # Returns: {"tier1_decayed": N, "tier2_decayed": N, "cemented_skipped": N}

async def apply_recall_boost(memory_id: str, db=None) -> None:
    """Bump salience +0.05 (capped at 1.0) and increment recall_count."""
```

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

### Task 11: Linker Service

**Files:**
- Create: `qmemory/core/linker.py`
- Create: `tests/test_core/test_linker.py`

- [ ] **Step 1: Write failing tests**

```python
async def test_linker_finds_unlinked():
    """Linker queries memories where linked=false."""

async def test_linker_marks_linked():
    """After processing, memories are marked linked=true."""

async def test_linker_validates_ids():
    """LLM-suggested IDs are validated against working set."""

async def test_linker_no_work():
    """Returns empty when no unlinked memories exist."""
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement linker**

```python
# qmemory/core/linker.py
async def run_linker_cycle(db=None) -> dict:
    """One linker cycle: find unlinked, ask LLM, create edges.

    1. Query 10 memories where linked = false
    2. Fetch 20 recent OTHER memories as candidates
    3. One cheap LLM call: find relationships
    4. Validate IDs against working set (hallucination defense)
    5. Create edges via link_nodes() with created_by="linker"
    6. Mark all processed as linked = true

    Returns: {"processed": N, "edges_created": N, "found_work": bool}
    """
```

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

### Task 12: Reflector Service

**Files:**
- Create: `qmemory/core/reflector.py`
- Create: `tests/test_core/test_reflector.py`

- [ ] **Step 1: Write failing tests**

```python
async def test_reflector_excludes_own_output():
    """Reflector never processes source_type='reflect' memories."""

async def test_reflector_no_work():
    """Returns empty when no new memories since last reflect."""
```

- [ ] **Step 2: Implement reflector**

5 cognitive jobs in ONE LLM call: patterns, contradictions, compressions, ghost entities, self learnings. Each result is saved via `save_memory()` with `source_type="reflect"`.

- [ ] **Step 3: Run tests**
- [ ] **Step 4: Commit**

### Task 13: Worker Entry Point

**Files:**
- Modify: `qmemory/worker/__init__.py`
- Modify: `qmemory/cli.py`

- [ ] **Step 1: Implement worker main loop**

```python
# qmemory/worker/__init__.py
async def run_worker():
    """Main worker loop — self-scheduling, token-budgeted."""
    from qmemory.core.token_budget import init_token_budget, can_spend
    init_token_budget("balanced")

    while True:
        # Check pause file
        if Path("~/.qmemory/worker-paused").expanduser().exists():
            await asyncio.sleep(60)
            continue

        # Linker
        linker_result = await run_linker_cycle()

        # Decay (piggybacks on linker, zero cost)
        await run_salience_decay()

        # Reflector (staggered — only every other cycle)
        if cycle_count % 2 == 0:
            await run_reflector_cycle()

        # Self-schedule
        interval = 300 if linker_result["found_work"] else 1800
        await asyncio.sleep(interval)
```

- [ ] **Step 2: Update CLI**

Replace "Coming in Phase 2" stub in `cli.py` with actual worker start.

- [ ] **Step 3: Test manually**

```bash
qmemory worker  # should start and log activity
# Create ~/.qmemory/worker-paused to test pause
```

- [ ] **Step 4: Commit**

```bash
git add qmemory/worker/ qmemory/core/decay.py qmemory/core/linker.py \
  qmemory/core/reflector.py qmemory/cli.py tests/test_core/test_decay.py \
  tests/test_core/test_linker.py tests/test_core/test_reflector.py
git commit -m "feat: add background worker with linker, reflector, and salience decay"
```

---

## Phase 5: Railway Deployment

### Task 14: Dockerfile + Railway Config

**Files:**
- Create: `Dockerfile`
- Create: `railway.json`
- Create: `Procfile`

- [ ] **Step 1: Create Dockerfile**

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY . .
EXPOSE 8080
CMD ["uvicorn", "qmemory.app.main:api", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 2: Create railway.json**

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": { "builder": "DOCKERFILE" },
  "deploy": {
    "startCommand": "uvicorn qmemory.app.main:api --host 0.0.0.0 --port $PORT",
    "healthcheckPath": "/health",
    "restartPolicyType": "ON_FAILURE"
  }
}
```

- [ ] **Step 3: Create Procfile for worker service**

```
worker: python -m qmemory.worker
```

- [ ] **Step 4: Commit**

```bash
git add Dockerfile railway.json Procfile
git commit -m "feat: add Railway deployment config"
```

### Task 15: Deploy to Railway

- [ ] **Step 1: Create Railway project**

```bash
railway init --name qmemory
```

- [ ] **Step 2: Add SurrealDB service**

Use `surrealdb/surrealdb:v3` Docker image. Add volume at `/data`. Set variables:
- `SURREAL_USER=root`
- `SURREAL_PASS=<generated>`
- Start command: `surreal start --user root --pass $SURREAL_PASS --bind 0.0.0.0:8000 surrealkv:/data/qmemory`

- [ ] **Step 3: Add qmemory-api service**

Connect GitHub repo. Set variables:
- `QMEMORY_SURREAL_URL=ws://surrealdb.railway.internal:8000`
- `QMEMORY_SURREAL_USER=root`
- `QMEMORY_SURREAL_PASS=<same as SurrealDB>`
- `QMEMORY_SECRET_KEY=<generated>`
- `ANTHROPIC_API_KEY=<from env>`
- `VOYAGE_API_KEY=<from env>`

- [ ] **Step 4: Add qmemory-worker service**

Same GitHub repo, different start command: `python -m qmemory.worker`
Same variables as qmemory-api.

- [ ] **Step 5: Generate domain**

```bash
railway domain --service qmemory-api
```

- [ ] **Step 6: Apply schema**

```bash
surreal import --endpoint https://surrealdb-xxx.up.railway.app \
  --username root --password $PASS \
  --namespace qmemory --database main \
  qmemory/db/schema.surql

surreal import --endpoint https://surrealdb-xxx.up.railway.app \
  --username root --password $PASS \
  --namespace qmemory --database main \
  qmemory/db/schema_cloud.surql
```

- [ ] **Step 7: Import existing data**

```bash
surreal import --endpoint https://surrealdb-xxx.up.railway.app \
  --username root --password $PASS \
  --namespace qmemory --database main \
  /Users/qusaiabushanap/data/qmemory-backup.surql
```

- [ ] **Step 8: Verify health**

```bash
curl https://qmemory-api-xxx.up.railway.app/health
curl https://qmemory-api-xxx.up.railway.app/mcp/ -X POST \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tools/list","id":1}'
```

- [ ] **Step 9: Commit any config changes**

---

## Phase 6: NanoBot Remote Connection

### Task 16: Update NanoBot Fork — memory.py HTTP Fallback

**Files:**
- Modify: `~/dev/nanobot-fork/nanobot/agent/memory.py`

- [ ] **Step 1: Add remote Qmemory support**

Update `get_memory_context()` to try remote HTTP call first:

```python
def get_memory_context(self, session_key: str = "default") -> str:
    # 1. Try remote Qmemory (Railway)
    import os
    qmemory_url = os.environ.get("QMEMORY_URL")
    qmemory_token = os.environ.get("QMEMORY_TOKEN")
    if qmemory_url and qmemory_token:
        try:
            import httpx
            response = httpx.post(
                f"{qmemory_url}/mcp/tools/qmemory_bootstrap",
                json={"session_key": session_key},
                headers={"Authorization": f"Bearer {qmemory_token}"},
                timeout=10,
            )
            result = response.json().get("result", "")
            if result:
                return result
        except Exception as e:
            logger.debug("Remote Qmemory failed: {}", e)

    # 2. Try local Qmemory (existing code)
    try:
        import asyncio, concurrent.futures
        from qmemory.core.recall import assemble_context
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, assemble_context(session_key))
            result = future.result(timeout=10)
        if result:
            return result
    except Exception as e:
        logger.debug("Local Qmemory failed: {}", e)

    # 3. Fallback: flat MEMORY.md
    long_term = self.read_long_term()
    return f"## Long-term Memory\n{long_term}" if long_term else ""
```

- [ ] **Step 2: Test with env vars**

```bash
QMEMORY_URL=https://qmemory-api-xxx.up.railway.app \
QMEMORY_TOKEN=qm_ak_... \
nanobot agent -m "bootstrap my memory"
```

- [ ] **Step 3: Commit and push fork**

```bash
cd ~/dev/nanobot-fork
git add nanobot/agent/memory.py
git commit -m "feat: add remote Qmemory HTTP fallback in memory.py"
git push origin main
```

- [ ] **Step 4: Reinstall NanoBot from fork**

```bash
uv tool install --force --from ~/dev/nanobot-fork nanobot-ai
```

### Task 17: Configure NanoBot MCP + Env Vars

- [ ] **Step 1: Add qmemory MCP server to NanoBot config**

```json
// ~/.nanobot/config.json — add under tools.mcpServers
"qmemory": {
  "type": "streamableHttp",
  "url": "https://qmemory-api-xxx.up.railway.app/mcp/",
  "headers": { "Authorization": "Bearer qm_ak_..." },
  "enabledTools": ["*"]
}
```

- [ ] **Step 2: Set env vars for system prompt injection**

Add to NanoBot's LaunchAgent or shell profile:
```bash
export QMEMORY_URL=https://qmemory-api-xxx.up.railway.app
export QMEMORY_TOKEN=qm_ak_...
```

- [ ] **Step 3: Restart NanoBot and test**

Send Donna a Telegram message. Check:
- System prompt includes graph memory context (not just MEMORY.md)
- `qmemory_save`, `qmemory_search` tools work via MCP
- Memories are created with owner field

- [ ] **Step 4: Remove local SurrealDB LaunchAgent (optional)**

```bash
launchctl bootout gui/$(id -u)/com.surrealdb.server
rm ~/Library/LaunchAgents/com.surrealdb.server.plist
```

---

## Verification Checklist

After all phases complete:

- [ ] Sign up on web dashboard → get API token
- [ ] Connect Claude Code with token → qmemory_bootstrap works
- [ ] Connect Claude.ai with token → qmemory_save works
- [ ] NanoBot/Donna auto-injects memory in system prompt
- [ ] Second user signs up → cannot see first user's memories
- [ ] Worker runs → unlinked memories get auto-linked
- [ ] Worker paused via `touch ~/.qmemory/worker-paused`
- [ ] No tokens consumed when idle (check token_budget logs)
- [ ] Railway health endpoint returns ok
- [ ] Existing 8,804 memories accessible after migration
