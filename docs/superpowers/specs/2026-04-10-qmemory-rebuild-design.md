# Qmemory Rebuild Mission — MCP Hardening + Multi-User + Workers + Legacy Migration

**Date:** 2026-04-10
**Status:** Design — pending user review
**Scope:** Unified rebuild of the MCP layer, activation of per-user isolation, Railway worker deployment, legacy data migration, and targeted search polish.

**Prior specs this builds on (do not duplicate):**
- `2026-03-24-qmemory-cloud-design.md` — original multi-user cloud vision. This spec activates what that one only planned.
- `2026-04-08-search-rebuild-design.md` — multi-leg BM25 + RRF fusion + category grouping. **Already approved and implemented.** This spec does NOT rewrite search again; it adds one small polish (type diversity cap) and corrects pagination metadata if missing.
- `2026-04-08-worker-health-design.md` — worker code is correct and exists. This spec only deploys it to Railway and teaches it to iterate per-user databases.

---

## 1. Motivation

Qmemory ran smoothly for one user (Qusai) using a single SurrealDB `main` database. Two compounding problems now demand a rebuild:

1. **Friends can't use it.** The MCP endpoint at `mem0.qusai.org/mcp/` is open access with zero isolation; anyone using it would see and write over Qusai's memories. Multi-user infrastructure was built in March (`user` table, `api_token`, `owner` field, `_user_db` ContextVar) then **deliberately disconnected** on commit `0b7fe1a` because OAuth added friction without solving the core problem.

2. **The MCP layer has structural drift.** `qmemory/mcp/server.py` (stdio, for Claude Code) and `qmemory/app/main.py` (HTTP, for Claude.ai) are two separate copies of the same 10 tool definitions built on **two different FastMCP packages** — the official `mcp.server.fastmcp` (1.x) and jlowin's `fastmcp` (2.x+). They diverge on annotations, error handling, and lifecycle. Every tool change must be made twice. Bugs hide in the gaps.

3. **Background workers aren't running.** The `linter`, `dedup_worker`, `decay`, and `reflector` jobs exist and are correct, but Railway's `Dockerfile` + `railway.json` only start `uvicorn`. The worker has never run in production. The memory graph is decaying without maintenance.

4. **Legacy data migration.** The current `main` database holds ~8,635 memories + extracted insights from 71 books. It must move to `user_qusai` before multi-user can ship, and it must move **reversibly**.

These four problems are coupled. Multi-user requires the operations table so tools receive user context cleanly. The worker fix requires multi-user so it can iterate per-user DBs. Legacy migration is a precondition for multi-user. Search polish piggybacks because we're already in the code.

**One spec. Atomic rebuild. Each phase leaves the system working.**

---

## 2. Goals

- ✅ Per-user SurrealDB database isolation via URL path (`/mcp/u/{user_code}/`).
- ✅ Single source of truth for MCP tool definitions (operations table) mounted by both stdio and HTTP.
- ✅ Unified FastMCP package — one set of schemas, one lifecycle, one annotation API.
- ✅ Pydantic input models on every tool with enforced constraints.
- ✅ Background worker running on Railway, iterating all active users.
- ✅ Qusai's legacy `main` database migrated to `user_qusai` reversibly.
- ✅ Type diversity cap on search results (DHH-approved gbrain polish).
- ✅ CORS credentials bug fixed.
- ✅ `qmemory_import` stub removed.

## 3. Non-goals

- ❌ OAuth 2.0 / dynamic client registration (deleted in `0b7fe1a`; not coming back).
- ❌ Separate bearer tokens or PINs (URL IS the auth — see decision D1).
- ❌ "Dream cycle" nightly LLM synthesis worker job (deferred — existing workers must prove themselves first).
- ❌ Compiled-truth + timeline split on entities (deferred — not a felt pain yet).
- ❌ Multi-query expansion in search (deferred — no measured recall problem).
- ❌ Alias arrays on entity records (deferred — fix when dedup actually fails).
- ❌ Full refactor of `core/*` modules. They stay intact; only the MCP layer and DB-connection layer are touched.
- ❌ Subdomain-per-user routing (path-scoped is simpler; no wildcard DNS/TLS).
- ❌ Self-serve admin dashboard for user management. Admin ops via CLI only.

## 4. Key decisions (locked)

| # | Decision | Rationale |
|---|---|---|
| **D1** | **URL = auth.** `user_code` in `/mcp/u/{code}/` is the only credential. No bearer tokens. | Simplest setup in Claude.ai. Acceptable risk for personal tool (not production multi-tenant SaaS). User explicitly chose this. |
| **D2** | **Per-user SurrealDB database**, same `qmemory` namespace. Database name: `user_{code}`. | Physical isolation, per-user backup/export, smaller HNSW indexes. User explicitly chose "different database." |
| **D3** | **URL shape:** `https://mem0.qusai.org/mcp/u/{user_code}/`. The `/u/` segment reserves space for future non-user paths. | Clean, extensible, disambiguates from `/admin`, `/health`, etc. |
| **D4** | **Unify on official `mcp.server.fastmcp`** (FastMCP 1.x from the Anthropic `mcp` SDK). Delete `fastmcp>=2.0` from dependencies. | Production-stable. DHH principle: pick the boring, shipped-with-the-platform option. |
| **D5** | **Single operations table** in `qmemory/mcp/operations.py`. Both stdio and HTTP mount from this one source. | Kills the "update both files" gotcha from CLAUDE.md permanently. |
| **D6** | **Rename `main` → `user_qusai` via export/import**, not in-place rename. | SurrealDB has no atomic rename at database level; export/import is the canonical migration path. |
| **D7** | **Dedicated Railway worker service** (not in-process asyncio). | Isolates worker LLM spend from HTTP responsiveness. User explicitly chose this. |
| **D8** | **Admin database** `qmemory.admin` holds only the `user` table. Every other table lives in per-user DBs. | Clean separation; one query per request to resolve user → DB name; cheap. |
| **D9** | **user_code format:** `{word}-{5 lowercase base32 chars}`. Words sourced from the [EFF long word list](https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt) (7776 entries, public domain, Diceware-quality). Pre-filtered to remove obviously-negative tone words (`abrasive`, `abrupt`, `absurd`, etc. — curated exclusion list committed as `qmemory/app/excluded_words.txt`). Example: `abacus-k7m3p`, `zoology-k7m3p`. | User choice: scale + public-domain provenance beat branded aesthetics. 7000+ post-filter words = collision-rate ~10⁻⁷ even with thousands of users. |
| **D10** | **stdio transport is for local dev/testing only.** Qusai (and all other users) use the HTTP remote URL via Claude Code's `claude mcp add --transport http https://mem0.qusai.org/mcp/u/{code}/`. Stdio still reads DB name from `QMEMORY_SURREAL_DB` env var for integration tests and local development. | Unifies to one transport for daily use. Same user, same data, whether talking to Claude Code or Claude.ai. |

---

## 5. Architecture

### 5.1 Request flow (HTTP, multi-user)

```
Claude.ai
  │
  │ POST https://mem0.qusai.org/mcp/u/calm-k7m3p/tools/call
  │   body: { "name": "qmemory_search", "arguments": {...} }
  ▼
FastAPI (api)
  │
  ├── MCPUserMiddleware
  │     ├── extract user_code="calm-k7m3p" from path
  │     ├── connect to qmemory.admin, SELECT user WHERE user_code=$code AND is_active
  │     ├── on miss → 404 (no info leak — just "not found")
  │     ├── on hit → _user_db.set("user_calm-k7m3p")
  │     └── fire-and-forget UPDATE user SET last_active_at=time::now()
  │
  ├── strip "/mcp/u/{code}" prefix → forward to mounted MCP sub-app
  │
  ▼
FastMCP (mcp_app, mounted at /mcp)
  │
  ├── dispatch tool by name
  │     ├── validate input via Pydantic model (rejects bad args with tool error)
  │     └── call handler (qmemory.core.search.search_memories, etc.)
  │
  ▼
core/search.py (unchanged)
  │
  ├── get_db()                     ← reads _user_db ContextVar
  │     └── connects to SurrealDB, uses qmemory.user_calm-k7m3p
  │
  └── runs 3-leg BM25 + RRF fusion
       + NEW: type diversity cap (≤60% per category)
       → returns JSON

  ▼
Response flows back up, no new copies
```

### 5.2 Request flow (stdio, single-user, Claude Code)

```
Claude Code
  │
  │ spawn subprocess: qmemory serve
  │ stdin/stdout JSON-RPC
  ▼
qmemory/mcp/server.py
  │
  ├── mcp = FastMCP("qmemory_mcp")
  ├── mount_operations(mcp, OPERATIONS)
  │
  ▼
Same operations table, same handlers.
No middleware. _user_db never set.
get_db() falls back to QMEMORY_SURREAL_DB env var (default: "user_qusai").
```

### 5.3 Database layout (post-migration)

```
SurrealDB instance @ surrealdb.railway.internal:8000
  │
  └── namespace: qmemory
        │
        ├── database: admin                  ← new, tiny
        │     └── user                       (user_code, display_name, db_name,
        │                                     created_at, last_active_at, is_active)
        │
        ├── database: user_qusai             ← renamed from "main"
        │     ├── memory                     (8,635 rows + books)
        │     ├── entity                     (books + people + concepts)
        │     ├── relates                    (graph edges)
        │     ├── session, message, tool_call, scratchpad, metrics
        │     └── health_report
        │
        ├── database: user_calm-k7m3p        ← new user, empty schema
        │     └── (full schema, all empty)
        │
        └── database: main                   ← kept as read-only safety net for 14 days
              └── (identical to pre-migration state)
```

### 5.4 Module layout (after rebuild)

```
qmemory/
├── mcp/
│   ├── __init__.py
│   ├── operations.py           ← NEW — single source of truth for all 9 tools
│   ├── schemas.py              ← NEW — Pydantic input models
│   ├── registry.py             ← NEW — mount_operations(mcp_server)
│   ├── errors.py               ← NEW — safe_tool() wrapper
│   └── server.py               ← shrunk to ~20 lines — stdio entry point
├── app/
│   ├── main.py                 ← shrunk — HTTP entry point, mounts middleware + operations
│   ├── middleware/
│   │   └── user_context.py     ← NEW — MCPUserMiddleware
│   ├── auth.py                 ← simplified — admin DB user lookup only
│   ├── routes/
│   │   ├── signup.py           ← simplified — generate code, provision DB, show URL
│   │   ├── connect.py          ← unchanged
│   │   └── dashboard.py        ← unchanged
│   └── config.py
├── db/
│   ├── client.py               ← get_db() reads _user_db ContextVar
│   ├── provision.py            ← provision_user_db(code) CREATE DB + apply schema
│   ├── schema.surql            ← base (unchanged)
│   ├── schema_cloud.surql      ← trimmed — drop api_token, user goes to admin DB
│   └── admin_schema.surql      ← NEW — only the user table, lives in admin DB
├── core/                       ← UNCHANGED
├── worker/                     ← UNCHANGED internals; new --all-users flag
└── cli.py                      ← worker command gains --all-users
```

---

## 6. Phase-by-phase plan

Each phase is self-contained and ends with a working system. If any phase fails, rollback is defined and we stop.

### Phase 0 — Safety net (30 minutes)

**Pre-flight only. No code changes.**

- [ ] Create feature branch `rebuild-2026-04-10`.
- [ ] Tag current commit: `git tag pre-rebuild-2026-04-10`.
- [ ] Run `./surrealdb/backup.sh` against production SurrealDB. Verify gzip integrity: `gunzip -t qmemory-*.surql.gz`.
- [ ] Spot-check backup: `zgrep -c "UPDATE memory:" backup.surql.gz` — expect ~8,635.
- [ ] Download backup to local machine; do not rely solely on Railway volume.
- [ ] Export current env vars (`railway variables`) — save to local file in case of rollback.

**Exit criteria:** verifiable backup exists on two machines, branch + tag created, env vars saved.

### Phase 1 — MCP layer rebuild (foundation)

**Goal:** One FastMCP package, one operations table, Pydantic models, annotations. No multi-user logic yet. System stays single-user on `main` database. Tests pass.

#### 1.1 Unify FastMCP dependency

- [ ] Remove `fastmcp>=2.0` from `pyproject.toml`.
- [ ] Run `uv lock` — confirm only `mcp[cli]` remains.
- [ ] Grep for `import fastmcp` — should hit only `qmemory/app/main.py` (which we rewrite next).
- [ ] Run existing tests — they use `from mcp.server.fastmcp import FastMCP` in fixtures, should be unaffected.

#### 1.2 Create Pydantic input schemas

New file: `qmemory/mcp/schemas.py`

One `BaseModel` per tool. All use:
```python
model_config = ConfigDict(
    str_strip_whitespace=True,
    extra="forbid",
)
```

Every field has:
- `Field(..., description="...")` with real text (becomes the tool's schema doc)
- Numeric constraints: `ge=0.0, le=1.0` for salience/confidence; `ge=1, le=50` for limit
- `Literal[...]` for enums (category, action, evidence_type, context_mood, check)
- Regex patterns where appropriate (e.g., memory IDs: `pattern=r"^(memory|entity|session):\w+$"`)

Models:
- `BootstrapInput(session_key)`
- `SearchInput(query, category, scope, limit, offset, after, before, include_tool_calls, source_type, entity_id)`
- `GetInput(ids, include_neighbors, neighbor_depth)` — ids has `min_length=1, max_length=20`
- `SaveInput(content, category, salience, scope, confidence, source_person, evidence_type, context_mood)`
- `CorrectInput(memory_id, action, new_content, updates, edge_id, reason)`
- `LinkInput(from_id, to_id, relationship_type, reason, confidence)`
- `PersonInput(name, aliases, contacts)`
- `BooksInput(book_id, section, query)`
- `HealthInput(check)`

**Delete:** `qmemory_import` stub. Nine tools, not ten.

#### 1.3 Create operations table

New file: `qmemory/mcp/operations.py`

```python
from dataclasses import dataclass
from typing import Callable, Awaitable
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from qmemory.mcp import schemas
from qmemory.core import recall, search, get, save, correct, link, person, books, health

@dataclass(frozen=True)
class Operation:
    name: str
    description: str            # one-line manpage entry — trimmed from current docstrings
    input_model: type[BaseModel]
    annotations: ToolAnnotations
    handler: Callable[[BaseModel], Awaitable[dict]]

OPERATIONS: list[Operation] = [
    Operation(
        name="qmemory_bootstrap",
        description=(
            "Load your full memory context at conversation start. Returns self-model, "
            "cross-session memories grouped by category, graph map, and session info. "
            "Call once at the START of every conversation."
        ),
        input_model=schemas.BootstrapInput,
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
        handler=lambda i: recall.assemble_context(i.session_key),
    ),
    # ... 8 more
]
```

Each description is **one line** — the current docstrings' multi-paragraph narratives move into `Field(..., description=...)` text on the input model. This cuts per-turn tool schema overhead from ~4-8k tokens to ~1.5k tokens.

Sibling disambiguation added to descriptions where relevant:
- `qmemory_search`: "Use `qmemory_get` if you already have memory IDs. Use `qmemory_books` to browse book knowledge hierarchically."
- `qmemory_get`: "Use `qmemory_search` if you only have a query, not IDs."
- `qmemory_books`: "Use `qmemory_search(source_type='from_book')` for keyword search across all books."

#### 1.4 Create registry (mount helper)

New file: `qmemory/mcp/registry.py`

```python
def mount_operations(mcp_server: FastMCP, ops: list[Operation]) -> None:
    for op in ops:
        mcp_server.tool(
            name=op.name,
            description=op.description,
            annotations=op.annotations,
        )(_make_wrapper(op))

def _make_wrapper(op: Operation):
    async def wrapper(**kwargs) -> str:
        # Pydantic validation
        try:
            validated = op.input_model(**kwargs)
        except ValidationError as e:
            return json.dumps({"isError": True, "message": f"Invalid arguments: {e}"})
        # Call handler with timing + error wrap
        return await safe_tool(op, validated)
    wrapper.__name__ = op.name
    return wrapper
```

#### 1.5 Create error wrapper

New file: `qmemory/mcp/errors.py`

```python
async def safe_tool(op: Operation, validated: BaseModel) -> str:
    start = time.monotonic()
    logger.info("Tool call: %s(%s)", op.name, _scrub(validated.model_dump()))
    try:
        result = await op.handler(validated)
    except DatabaseError as e:
        logger.exception("DB error in %s", op.name)
        return json.dumps({
            "isError": True,
            "content": [{"type": "text", "text": f"Database unreachable. Retry in a few seconds. ({e.__class__.__name__})"}]
        }, ensure_ascii=False)
    except Exception as e:
        logger.exception("Unexpected error in %s", op.name)
        return json.dumps({
            "isError": True,
            "content": [{"type": "text", "text": f"Internal error in {op.name}. Check logs."}]
        }, ensure_ascii=False)
    elapsed = time.monotonic() - start
    logger.info("%s completed in %.2fs", op.name, elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)
```

**Key:** every tool call returns `isError: true` on failure instead of raising through to the MCP transport layer. Claude can react and retry.

#### 1.6 Shrink stdio server

Rewrite `qmemory/mcp/server.py` to ~20 lines:

```python
from mcp.server.fastmcp import FastMCP
from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations

mcp = FastMCP(
    "qmemory_mcp",                               # lowercase, {service}_mcp convention
    instructions=(
        "Graph memory for AI agents. Call qmemory_bootstrap first. "
        "Use qmemory_search for queries, qmemory_save for new facts, "
        "qmemory_correct for fixes, qmemory_link for relationships."
    ),
)

mount_operations(mcp, OPERATIONS)
```

Entry point in `cli.py::serve` calls `mcp.run()`.

#### 1.7 Shrink HTTP entry point

Rewrite `qmemory/app/main.py`. Key changes:
- Import from `mcp.server.fastmcp` (not `fastmcp`).
- `mcp_app = mcp.http_app(path="/", json_response=True, stateless_http=True)` — unchanged pattern.
- Mount operations via the same `mount_operations(mcp, OPERATIONS)` call.
- CORS middleware: set `allow_origins=["https://claude.ai", "https://claude.com", "https://*.anthropic.com"]` and keep `allow_credentials=True`. Wildcard+credentials is broken in browsers — real fix.
- Still mounts MCP at `/mcp` (no multi-user middleware yet in Phase 1; added in Phase 2).

**Exit criteria for Phase 1:**
- `uv run pytest tests/` passes (140+ tests).
- `qmemory serve` runs stdio mode; Claude Code can connect and call `qmemory_bootstrap`.
- `uv run uvicorn qmemory.app.main:api` runs HTTP; `curl /mcp/tools` returns 9 tool schemas.
- All 9 tools have `ToolAnnotations`, Pydantic input schemas, and truncated descriptions.
- `grep -r "import fastmcp" qmemory/` returns nothing.
- Single-user behavior unchanged (still reads `main` database).

**Rollback:** `git reset --hard pre-rebuild-2026-04-10`. No data touched.

### Phase 2 — Multi-user isolation (the new feature)

**Goal:** per-user SurrealDB database, routed by URL path, with a signup flow that provisions.

#### 2.1 Admin database + schema

New file: `qmemory/db/admin_schema.surql`

```sql
DEFINE TABLE IF NOT EXISTS user SCHEMAFULL;
DEFINE FIELD user_code ON user TYPE string ASSERT string::len($value) BETWEEN 4 AND 64;
DEFINE FIELD display_name ON user TYPE string;
DEFINE FIELD db_name ON user TYPE string;                      -- e.g. "user_calm-k7m3p"
DEFINE FIELD created_at ON user TYPE datetime VALUE time::now();
DEFINE FIELD last_active_at ON user TYPE option<datetime>;
DEFINE FIELD is_active ON user TYPE bool DEFAULT true;
DEFINE INDEX idx_user_code ON user FIELDS user_code UNIQUE;
```

#### 2.2 Provisioning helper

New file: `qmemory/db/provision.py`

```python
async def provision_user_db(user_code: str, display_name: str) -> str:
    """Create a new user database and apply full schema. Returns db_name."""
    db_name = f"user_{user_code}"

    # 1. Use admin DB, create the new database
    async with get_admin_db() as admin:
        await query(admin, f"DEFINE DATABASE {db_name}")

    # 2. Switch to the new DB and apply base + cloud schema
    async with get_db_by_name(db_name) as user_db:
        await apply_schema(user_db, include_admin=False)

    # 3. Insert user row in admin
    async with get_admin_db() as admin:
        await query(admin, """
            CREATE user SET
                user_code = $code,
                display_name = $name,
                db_name = $db_name,
                is_active = true
        """, {"code": user_code, "name": display_name, "db_name": db_name})

    return db_name
```

#### 2.3 User code generator

New files:
- `qmemory/app/wordlist.py` — bundles the EFF long word list at build time
- `qmemory/app/user_code.py` — generator

```python
# qmemory/app/wordlist.py
# Source: https://www.eff.org/files/2016/07/18/eff_large_wordlist.txt
# License: Creative Commons Attribution 3.0
# 7776 dice-numbered English words. We load, strip dice numbers, then
# filter out ~200 obviously-negative words (committed exclusion list).

from pathlib import Path

_EFF_PATH = Path(__file__).parent / "data" / "eff_large_wordlist.txt"
_EXCLUDED_PATH = Path(__file__).parent / "data" / "excluded_words.txt"

def load_wordlist() -> list[str]:
    """Load the EFF long list, strip dice numbers, apply exclusion filter."""
    excluded = {w.strip() for w in _EXCLUDED_PATH.read_text().splitlines() if w.strip()}
    words = []
    for line in _EFF_PATH.read_text().splitlines():
        # Format: "11111\tabacus" — tab-separated dice number + word
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] not in excluded:
            words.append(parts[1])
    return words

WORDLIST = load_wordlist()    # ~7500 words after exclusion
```

```python
# qmemory/app/user_code.py
import secrets, string
from qmemory.app.wordlist import WORDLIST

def generate_user_code() -> str:
    word = secrets.choice(WORDLIST)
    suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(5))
    return f"{word}-{suffix}"

async def generate_unique_user_code(max_attempts: int = 10) -> str:
    for _ in range(max_attempts):
        code = generate_user_code()
        async with get_admin_db() as db:
            existing = await query(db, "SELECT id FROM user WHERE user_code = $code", {"code": code})
        if not existing:
            return code
    raise RuntimeError("Could not generate unique user code after 10 attempts — word list exhausted?")
```

**Exclusion list curation:** hand-review the EFF list once, remove words with clearly negative connotations (`abrasive`, `abrupt`, `absurd`, `abuse`, `accuse`, `ache`, `acrid`, etc.). Target: ~200 exclusions, leaving 7500+ words. Committed as `qmemory/app/data/excluded_words.txt` for auditability.

#### 2.4 DB client context routing

Modify `qmemory/db/client.py`:

```python
_user_db: ContextVar[str | None] = ContextVar("_user_db", default=None)

@asynccontextmanager
async def get_db() -> AsyncIterator[Surreal]:
    db_name = _user_db.get() or settings.surreal_db   # env var fallback for stdio
    async with _connect(
        url=settings.surreal_url,
        user=settings.surreal_user,
        pass_=settings.surreal_pass,
        namespace=settings.surreal_namespace,
        database=db_name,
    ) as db:
        yield db

@asynccontextmanager
async def get_admin_db() -> AsyncIterator[Surreal]:
    """Connect directly to the admin database. Never uses _user_db."""
    async with _connect(
        url=settings.surreal_url,
        user=settings.surreal_user,
        pass_=settings.surreal_pass,
        namespace=settings.surreal_namespace,
        database="admin",
    ) as db:
        yield db
```

Core modules (`search.py`, `save.py`, etc.) **don't change**. They already call `get_db()`; it just picks up the ContextVar automatically.

#### 2.5 MCP user middleware

New file: `qmemory/app/middleware/user_context.py`

```python
class MCPUserMiddleware(BaseHTTPMiddleware):
    """Extract user_code from /mcp/u/{code}/ path, resolve to DB name, set ContextVar."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Match /mcp/u/{code}/...
        m = re.match(r"^/mcp/u/([a-z0-9-]+)(/.*)?$", path)
        if not m:
            return await call_next(request)    # not a user-scoped MCP call, pass through

        user_code = m.group(1)

        # Lookup in admin DB
        async with get_admin_db() as admin:
            rows = await query(admin,
                "SELECT db_name, is_active FROM user WHERE user_code = $code",
                {"code": user_code})

        if not rows or not rows[0]["is_active"]:
            # No info leak — just "not found"
            return JSONResponse({"error": "not_found"}, status_code=404)

        db_name = rows[0]["db_name"]

        # Set ContextVar for the duration of the request
        token = _user_db.set(db_name)
        try:
            # Rewrite path: strip /u/{code} so the mounted MCP app sees /mcp/...
            request.scope["path"] = "/mcp" + (m.group(2) or "/")
            request.scope["raw_path"] = request.scope["path"].encode()
            response = await call_next(request)
        finally:
            _user_db.reset(token)

        # Fire-and-forget update of last_active_at
        asyncio.create_task(_touch_user(user_code))

        return response
```

**Important gotcha:** path rewriting must happen BEFORE `call_next`. Starlette's router matches on `request.scope["path"]`. The mounted MCP sub-app is at `/mcp`; we need it to see `/mcp/tools/call`, not `/mcp/u/calm-k7m3p/tools/call`.

#### 2.6 Wire middleware into main.py

```python
api.add_middleware(MCPUserMiddleware)   # must be OUTSIDE the /mcp mount
api.mount("/mcp", mcp_app)              # mounted app serves both /mcp/... and /mcp/u/.../...
```

#### 2.7 Signup route

Rewrite `qmemory/app/routes/signup.py`:

1. GET `/signup` — show form (display_name field only).
2. POST `/signup`:
   - Generate unique user_code
   - `provision_user_db(user_code, display_name)`
   - Redirect to `/connect?code={user_code}`
3. GET `/connect?code={user_code}` — show the personal URL, copy button, paste-into-Claude.ai instructions.

**No password. No email. No token.** The URL is the credential. Losing it = losing the data (documented warning on the page).

#### 2.8 Update CLAUDE.md

The current CLAUDE.md says "Tools defined in TWO places — keep in sync." After Phase 1, this is obsolete. Update to: "All 9 tools live in `qmemory/mcp/operations.py`. Edit once, mounted to both transports by `mount_operations()`."

**Exit criteria for Phase 2:**
- Admin DB exists with schema.
- `/signup` flow creates user_code + provisions empty DB.
- Accessing `/mcp/u/{code}/tools/list` returns tools.
- Accessing `/mcp/u/{unknown-code}/tools/list` returns 404.
- Two signup'd users see zero overlap when saving + searching memories (integration test).
- Legacy `/mcp/` endpoint (no `/u/`) still works for now and points at the default DB (`main`). Removed in Phase 3.

**Rollback:** feature flag `MULTI_USER_ENABLED=false` — middleware becomes a no-op, `/mcp/` behaves as before. If rollback needed after users sign up, their databases remain intact, just not routable.

### Phase 3 — Legacy migration (`main` → `user_qusai`)

**🟡 MEDIUM RISK MODIFICATION — requires backup verification before starting.**

#### 3.1 Export

```bash
surreal export --endpoint "$SURREAL_URL" --username root --password "$SURREAL_PASS" \
  --namespace qmemory --database main > main-export.surql

wc -l main-export.surql                           # expect thousands of lines
grep -c "UPDATE memory:" main-export.surql        # expect ~8635
grep -c "UPDATE entity:" main-export.surql        # expect ~72 books + N people
```

#### 3.2 Provision `user_qusai` database only

```bash
# Create the empty database + apply schema. Does NOT insert the admin user row yet —
# that happens in step 3.6 after verification passes, so a failed import can't leave
# a dangling admin pointer.
qmemory admin create-db --name user_qusai

# This creates:
#   - database qmemory.user_qusai (empty, with full schema)
#   - NO admin user row yet
```

#### 3.3 Import

```bash
surreal import --endpoint "$SURREAL_URL" --username root --password "$SURREAL_PASS" \
  --namespace qmemory --database user_qusai main-export.surql
```

Expected: 5-15 minutes as HNSW indexes rebuild over 8,635 memory embeddings.

#### 3.4 Verify counts

```bash
# Run via surreal CLI or qmemory status
SELECT count() FROM memory;           # expect 8635
SELECT count() FROM entity;           # expect 72+
SELECT count() FROM relates;          # expect N (record pre-migration)
SELECT count() FROM memory WHERE source_type = 'from_book';   # book insights
```

Automation: `scripts/verify_migration.py` that compares pre- and post-counts for every table and fails loudly on any mismatch.

#### 3.5 Sample verification (behavioral)

Run 5 known queries via `qmemory search` against both `main` (read-only) and `user_qusai`. Results must match exactly.

```bash
QMEMORY_SURREAL_DB=main qmemory search "Rakeezah"
QMEMORY_SURREAL_DB=user_qusai qmemory search "Rakeezah"
# Compare top 5 IDs
```

#### 3.6 Create admin user row pointing at `user_qusai`

Only after steps 3.4 + 3.5 have passed:

```bash
qmemory admin create-user --user-code qusai --display-name "Qusai Abushanap" --db-name user_qusai
```

```sql
-- Equivalent SurrealQL
CREATE user SET
    user_code = 'qusai',
    display_name = 'Qusai Abushanap',
    db_name = 'user_qusai',
    is_active = true;
```

This is the moment `/mcp/u/qusai/` becomes routable. If it goes wrong, the rollback is `DELETE user WHERE user_code = 'qusai'` — data in `user_qusai` stays untouched.

#### 3.7 Deprecate legacy `/mcp/` endpoint

In `main.py`, make `/mcp/` (without `/u/{code}`) redirect to `/connect` or return a 410 Gone with an explanation. All Claude.ai clients must update to the personal URL.

**Exit criteria for Phase 3:**
- `qmemory.user_qusai` exists with row counts matching `main` within ±0 (not ±%).
- Sample searches return byte-identical results.
- Qusai's personal URL `https://mem0.qusai.org/mcp/u/qusai/` works end-to-end from Claude.ai.
- `main` database untouched (read-only safety net).

**Rollback:** `UPDATE user SET db_name = 'main' WHERE user_code = 'qusai'`. Instant. Data in `user_qusai` stays as a parallel copy until we're confident.

**Permanent cleanup:** `main` database deleted after 14 days of incident-free operation. Not before.

### Phase 4 — Railway worker service

**Goal:** the linter + dedup + decay + reflector loop actually runs in production, iterating all user databases.

#### 4.1 Teach worker to iterate users

Modify `qmemory/worker/__init__.py`:

```python
async def run_worker(interval: int = 86400, once: bool = False, all_users: bool = False) -> None:
    while True:
        if all_users:
            async with get_admin_db() as admin:
                users = await query(admin, "SELECT user_code, db_name FROM user WHERE is_active = true")
            logger.info("Worker cycle: %d users", len(users))
            for u in users:
                token = _user_db.set(u["db_name"])
                try:
                    await _run_one_cycle(user_label=u["user_code"])
                except Exception:
                    logger.exception("Worker cycle failed for user %s", u["user_code"])
                finally:
                    _user_db.reset(token)
        else:
            # Single-user mode (local dev, CLI)
            await _run_one_cycle(user_label="default")

        if once:
            return
        await asyncio.sleep(interval)
```

#### 4.2 CLI flag

Add `--all-users` to `qmemory worker` command in `cli.py`:

```python
@cli.command()
@click.option("--interval", default=86400, type=int)
@click.option("--once", is_flag=True)
@click.option("--all-users", is_flag=True, help="Iterate all active users from admin DB")
def worker(interval: int, once: bool, all_users: bool):
    asyncio.run(run_worker(interval=interval, once=once, all_users=all_users))
```

#### 4.3 Per-user token budget

Modify `qmemory/core/token_budget.py` to scope budget by user_code (ContextVar key). Fair-share: total budget / N active users. Implementation is local to the file; core modules unchanged.

#### 4.4 Railway worker service

In the Railway project, create a new service called `qmemory-worker` from the same GitHub repo, same Dockerfile, but with:

- **Start command:** `qmemory worker --interval 3600 --all-users`
- **Env vars:** same as API service (copy via `railway variables --service qmemory-worker --set ...`)
- **Health check:** none (it's a loop, not a server)
- **Resource limits:** 512MB RAM, 0.5 vCPU (generous — it's not heavy)

Commit a `scripts/railway-worker-setup.md` runbook documenting the manual Railway UI steps (Railway CLI doesn't support all service creation options).

#### 4.5 Health report scoping

`health_report` table lives in each user database (not admin). `qmemory_health` tool reads from `get_db()` — automatically scoped to the calling user. No cross-user leakage.

**Exit criteria for Phase 4:**
- `qmemory-worker` Railway service is running.
- `railway logs --service qmemory-worker` shows "Worker cycle: N users" entries.
- `qmemory_health` tool returns a recent report (not "no report found").
- One cycle per user completes in under 5 minutes at current data size.

**Rollback:** delete the Railway worker service. No data impact.

### Phase 5 — Polish

Small, isolated, high-value changes. Each is its own commit.

#### 5.1 Type diversity cap (gbrain #2)

Add to `qmemory/core/search.py` post-fusion result assembly:

```python
DIVERSITY_CAP = 0.6        # no single category > 60% of results

def _apply_diversity_cap(memories: list[dict], limit: int) -> list[dict]:
    per_cat_cap = max(1, int(limit * DIVERSITY_CAP))
    counts: dict[str, int] = {}
    result = []
    overflow = []
    for m in memories:
        cat = m.get("category", "unknown")
        if counts.get(cat, 0) < per_cat_cap:
            result.append(m)
            counts[cat] = counts.get(cat, 0) + 1
        else:
            overflow.append(m)
        if len(result) >= limit:
            break
    # Backfill with overflow if we didn't hit limit (sparse result sets)
    while len(result) < limit and overflow:
        result.append(overflow.pop(0))
    return result
```

Apply **after** RRF fusion and category grouping, **before** the final `memories.{category}` dict assembly. The dynamic sections (entities_matched, pinned, book_insights, hypotheses) are untouched.

#### 5.2 Pagination metadata audit

Read `qmemory/core/search.py` — verify the `meta` object in the response contains `total_found`, `returned`, `offset`, `has_more`. If missing, add. (The April 8 spec designed this, but implementation drift is possible.)

#### 5.3 Delete `qmemory_import` stub

Already removed from operations table in Phase 1.2. This step is documentation-only: update CLAUDE.md tool count from 10 to 9.

**Exit criteria for Phase 5:**
- Search results with >10 memories never contain more than 60% from one category.
- `meta` object always includes pagination fields.

### Phase 6 — Verification and cutover

#### 6.1 Local verification

- [ ] `uv run pytest tests/` — all tests pass.
- [ ] `qmemory serve` (stdio) — Claude Code connects, all 9 tools callable.
- [ ] `uv run uvicorn qmemory.app.main:api` (HTTP) locally:
  - [ ] `GET /health` → 200
  - [ ] `POST /signup` → creates test user, returns URL
  - [ ] `GET /mcp/u/{test-code}/tools/list` → 9 tools
  - [ ] `POST /mcp/u/{test-code}/tools/call {"name":"qmemory_save", ...}` → success
  - [ ] `POST /mcp/u/{unknown-code}/tools/list` → 404
  - [ ] Two separate test users save distinct memories, searches return only own data

#### 6.2 Staging deploy

Push `rebuild-2026-04-10` branch → Railway auto-deploys a PR environment (if configured) or a separate preview service.

- [ ] Smoke test against staging URL
- [ ] Verify admin DB exists
- [ ] Verify `user_qusai` migration on staging (using a staging-only export)

#### 6.3 Production cutover

Ordered:
1. Deploy updated API service → new code runs, but still defaults to `main` (env var `QMEMORY_SURREAL_DB=main` per D10).
2. Run migration: `main` → `user_qusai`.
3. Insert `qusai` admin user row pointing to `user_qusai`.
4. Update personal Claude.ai MCP URL to `/mcp/u/qusai/`.
5. Deploy worker service.
6. Monitor logs 24 hours.

#### 6.4 Post-deploy smoke tests

- [ ] Qusai's personal URL works from Claude.ai.
- [ ] A second test user (you create via signup) has fully isolated data.
- [ ] Worker logs show "Worker cycle: 2 users" entries.
- [ ] `qmemory_health` returns fresh report for both users.
- [ ] No error spikes in Railway logs.

**Exit criteria for Phase 6 = mission complete:**
- Qusai's memories are accessible only via `/mcp/u/qusai/`.
- A friend signing up gets a fresh personal URL and isolated DB.
- Workers run on Railway, maintaining the graph without intervention.
- Nothing from the CLAUDE.md gotcha list is still true.

**Rollback:** each phase has its own rollback. Phase-6-specific rollback:
- Flip admin pointer back to `main`
- Remove MCPUserMiddleware
- Revert frontend docs to non-user URL
- `main` untouched, no data loss

---

## 7. Risks and mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| FastMCP 1.x missing features we rely on (e.g., progress reporting) | Medium | Audit `core/` for any code that calls FastMCP 2.x-specific APIs. None found in current audit (only `@mcp.tool()` and `http_app()` are used). |
| Migration export/import corrupts data | High | Phase 0 verified backup. `main` database stays live until 14-day safety window passes. Automated verify script. |
| HNSW index rebuild on import takes too long | Medium | Spike: time a test export/import on a staging DB with comparable size. If >15 min, consider `EXPLAIN ANALYZE` on index creation. |
| URL with user_code leaks in Claude.ai telemetry or browser history | High (security) | Documented warning on `/signup` and `/connect` pages. Future: optional PIN layer. For now: acceptable for friends-and-family scale. |
| Worker cycle per user too slow at scale (e.g., 50 users × 5 min = 4 hours) | Low now, Medium later | Cycle interval is 1 hour. If workload exceeds budget, add parallelism (`asyncio.gather` on user cycles, with per-user lock). Deferred until felt. |
| Middleware path rewriting breaks Starlette routing in unexpected ways | Medium | Dedicated test: `test_middleware_path_rewrite.py` — simulates `/mcp/u/test/tools/list` and asserts downstream handlers see `/mcp/tools/list`. |
| CORS allowlist excludes a legitimate Claude.ai domain | Low | Start with `https://claude.ai`, `https://claude.com`, `https://*.anthropic.com`. Expand if `OPTIONS` preflights fail in Railway logs. |
| Migration 14-day safety window gets forgotten | Low | Create a `TaskCreate` + calendar reminder when Phase 3 completes. |

---

## 8. Resolved decisions (previously open)

All five were answered on 2026-04-10 before implementation started. Baked into D9, D10, and the phase plan.

1. **Word list source → EFF long word list (7776 words), with ~200 negative-tone exclusions pre-filtered.** Scale and public-domain provenance over branded curation. See D9 and Phase 2.3 for the loader + filter. Committed exclusion list at `qmemory/app/data/excluded_words.txt`.

2. **Signup friction → zero.** Signup asks only for display name. Returns the personal URL. Lose the URL = lose the data. Documented warning on `/signup` and `/connect`. Future "send me my URL" flow deferred indefinitely — DHH-approved for friends-and-family scale. No mailer setup, no account system.

3. **Books → per-user, stay with Qusai.** The 71 books + 8,500 insights migrate only to `user_qusai`. New users start with empty book libraries. No shared DB, no seeding on signup, no cross-DB queries. Respects these as Qusai's curated research. A future "import books from source" action can be added if friends ask, but not in this spec.

4. **Admin CLI → minimal only.** Four commands ship: `admin create-db`, `admin create-user`, `admin list-users`, `admin status`. Everything else (deactivate, rotate, export, delete) is deferred until felt. YAGNI applied.

5. **Stdio default DB → doesn't matter for Qusai's daily use.** Qusai will use the HTTP remote URL (`mem0.qusai.org/mcp/u/qusai/`) from Claude Code via `claude mcp add --transport http`, not stdio with a direct SurrealDB connection. Stdio mode remains available for integration tests and local dev; it reads `QMEMORY_SURREAL_DB` env var with no default. See D10.

**Consequence of #5:** the "dual transport, dual logic" cognitive overhead of the current codebase evaporates. Every real user — Qusai included — talks to the same HTTP endpoint, goes through the same middleware, gets the same isolation. stdio is just a developer tool.

---

## 9. Success metrics (how we know it worked)

- **Isolation:** two test users save memories with identical content; each one's search returns only their own row.
- **Worker health:** `qmemory_health` returns a report with `generated_at` within the last 2 hours.
- **Drift prevention:** only one place defines tools (`qmemory/mcp/operations.py`). `grep -r "@mcp.tool" qmemory/ | wc -l` returns exactly 9 — the loop inside `mount_operations`, not 18 or 20.
- **Schema enforcement:** passing an out-of-range `salience=2.5` to `qmemory_save` returns an `isError: true` with a Pydantic validation message.
- **Token budget:** tool schemas combined with descriptions consume <2k tokens per turn (measure via MCP inspector).
- **Search diversity:** a query that would previously return 10 book insights now returns a mix (max 6 of any category).

---

## 10. Work estimate

Rough sizing for a product designer + AI pair working incrementally:

| Phase | Estimated effort |
|---|---|
| 0 — Safety net | 0.5 hour |
| 1 — MCP rebuild | 4-6 hours (schemas, ops table, registry, rewrites) |
| 2 — Multi-user | 3-4 hours (middleware, provision, signup, tests) |
| 3 — Migration | 1-2 hours (mostly waiting for export/import + verification) |
| 4 — Worker deploy | 1 hour (Railway setup + `--all-users` flag) |
| 5 — Polish | 0.5 hour (diversity cap is 30 lines) |
| 6 — Verification + cutover | 2 hours |
| **Total** | **12-16 hours** spread over 2-3 days |

Not a weekend project. Not a month-long rewrite either. The kind of focused rebuild DHH would call "a proper week's work."

---

## 11. Acceptance — what the end state looks like

When this spec is done:

1. **Qusai opens Claude.ai, pastes `https://mem0.qusai.org/mcp/u/qusai/`, and sees all his memories intact.**
2. **A friend signs up at `mem0.qusai.org/signup`, gets a personal URL, pastes it into their Claude.ai, and starts with an empty graph.** Nothing the friend does affects Qusai's data.
3. **The worker runs quietly on Railway every hour**, walking all active users and maintaining their graphs. Qusai sees fresh health reports.
4. **Editing a tool is a single-file change in `qmemory/mcp/operations.py`.** Both stdio and HTTP pick it up automatically. The CLAUDE.md "update in two places" warning is deleted.
5. **Invalid tool arguments are rejected at the MCP boundary** with a clear error, not silently accepted.
6. **`main` database still exists** (for 14 days) as a rollback safety net, untouched.
