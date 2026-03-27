# Qmemory

Graph-based memory system for AI agents. Python rebuild (from TypeScript).
Uses SurrealDB as a graph database, exposed via MCP (Claude Code) and NanoBot tools.

## Commands

```bash
# Tests (requires running SurrealDB)
uv run pytest tests/                          # all tests (139 passing)
uv run pytest tests/test_core/test_recall.py -v  # one file, verbose

# CLI
qmemory status                  # check SurrealDB connection + record counts
qmemory serve                   # MCP server (stdio for Claude Code)
qmemory serve-http --port 3777  # MCP server (HTTP for Claude.ai)
qmemory schema                  # apply DB schema (safe to re-run)
```

## Prerequisites

- Python 3.11+
- SurrealDB running locally: `surreal start --user root --pass root`
- `.env` file with keys (copy from `.env.example`):
  - `QMEMORY_SURREAL_URL`, `QMEMORY_SURREAL_USER`, `QMEMORY_SURREAL_PASS`
  - `ANTHROPIC_API_KEY` (for LLM dedup)
  - `VOYAGE_API_KEY` (for embeddings)

## Railway Deployment

SurrealDB runs as a separate Railway service built from `surrealdb/Dockerfile`:
- **Image**: Custom Debian + SurrealDB v3.0.0 (pinned)
- **Engine**: RocksDB at `/data/qmemory.db` (persistent volume at `/data`)
- **URL**: `surrealdb-production-d9ea.up.railway.app`
- **Internal**: `surrealdb.railway.internal:8000` (free, for app→DB)
- **Auth**: root + `SURREAL_PASS` env var (never hardcoded)

```bash
# Deploy updated Dockerfile to Railway
cd surrealdb && railway up --service surrealdb --detach

# Import schema to Railway
surreal import -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" --namespace qmemory --database main schema.surql

# Backup (run before schema changes!)
./surrealdb/backup.sh
```

**Railway env vars for SurrealDB service**: `PORT=8000`, `SURREAL_PASS`, `SURREAL_LOG=info`
**Railway env vars for app service**: `QMEMORY_SURREAL_URL=ws://surrealdb.railway.internal:8000`

### Multi-User Auth (Cloud Schema)

Cloud schema adds user accounts, API tokens, and owner-based row isolation:
- `schema_cloud.surql` — user table, `qmemory_user` access (Argon2), api_token table, owner fields
- `schema_cloud_permissions.surql` — row-level permissions (owner = $auth isolation)
- **Import order**: `schema.surql` → `schema_cloud.surql` → `schema_cloud_permissions.surql`
- Root (MCP local mode) bypasses all permissions — sees everything
- Record-level users only see memories/entities where `owner = $auth`
- Tables with `PERMISSIONS NONE` silently return empty for non-root users — always use `OVERWRITE` when adding permissions

### MCP Endpoint (Remote)

- **URL**: `https://mem0.qusai.org/mcp/` (direct: `qmemory-api-production.up.railway.app/mcp/`)
- **Auth (Bearer)**: `Authorization: Bearer qm_ak_xxxxx` header — for Claude Code and API clients
- **Auth (Bypass)**: When `QMEMORY_BYPASS_KEY` env var exists, all unauthenticated requests skip OAuth and route to `QMEMORY_BYPASS_USER` (default: `hi@qusai.org`). No key in URL needed — just the env var's presence enables it.
- **To re-enable multi-user OAuth**: `railway variables delete QMEMORY_BYPASS_KEY --service qmemory-api` — bypass code becomes no-op, 401 + OAuth flow takes over. All OAuth routes, DB provisioning, and per-user isolation are already implemented and ready.
- **Token format**: `qm_ak_` + 32 hex chars (38 chars total). Only SHA-256 hash stored in DB.
- **Token flow**: signup at `/signup` → generate token at `/tokens` → use in Claude Code MCP client
- Auth middleware in `MCPAuthMiddleware` class in `app/main.py` wraps the FastMCP sub-app
- `/health` endpoint does NOT require auth (for Railway health checks)
- `mcp.http_app(path="/")` + `api.mount("/mcp", ...)` = clean `/mcp/` URL (not `/mcp/mcp/`)
- **OAuth 2.0 routes**: `/authorize`, `/token`, `/register`, `/.well-known/oauth-*` — all implemented but Claude.ai has a known bug where it never starts the OAuth flow ([anthropics/claude-ai-mcp#5](https://github.com/anthropics/claude-ai-mcp/issues/5))

## Gotchas

- **`http_app(path="/")` NOT `path="/mcp/"`** — `api.mount("/mcp", ...)` strips the prefix before passing to FastMCP. Using `path="/mcp/"` causes 404.
- **Railway CDN (Fastly) caches 401 responses** — when testing OAuth/auth changes, use the direct Railway URL (`qmemory-api-production.up.railway.app`) to bypass CDN cache.
- **OAuth token errors must return HTTP 400/401** — RFC 6749 requires it. Returning 200 with error JSON body breaks Claude.ai's OAuth client.
- **OAuth register must return HTTP 201** — RFC 7591 requires 201 Created + `client_id_issued_at` field.
- **Claude.ai OAuth is a known bug** — Claude.ai receives 401 but never starts OAuth discovery. Affects all custom MCP servers. Tracked at anthropics/claude-ai-mcp#5. Don't spend time debugging our OAuth if Claude.ai never sends requests to `/.well-known/*`.
- **ASGI scope in mounted middleware** — `api.mount("/mcp", Middleware(app))` strips path prefix in scope. Middleware sees `path="/"`, not `path="/mcp/"`. But `scope.get("root_path")` = `/mcp`.
- **Schema loader must load ALL .surql files** — `apply_schema()` in `db/client.py` loads 3 files in order: `schema.surql` → `schema_cloud.surql` → `schema_oauth.surql`. Missing a file causes silent failures (tables don't exist, queries return empty).

## Architecture

```
qmemory/
  app/               # Cloud HTTP server (FastAPI + FastMCP)
    main.py          #   FastAPI app, MCPAuthMiddleware, OAuth bypass, mounts
    config.py        #   AppSettings — QMEMORY_ prefixed env vars (bypass_key, bypass_user)
    auth.py          #   resolve_api_token(), create_api_token_for_user()
    routes/oauth.py  #   OAuth 2.0 endpoints: /authorize, /token, /register, /consent
    routes/auth.py   #   Session auth: /login, /signup, /logout
    routes/tokens.py #   Token management UI: /tokens
    routes/connect.py#   /connect page for Claude.ai setup
  config.py          # Pydantic Settings — env vars with QMEMORY_ prefix
  constants.py       # 8 memory categories, extraction presets, salience decay
  types.py           # Pydantic models for all graph nodes + edges
  db/client.py       # SurrealDB: get_db(), query(), normalize_ids(), generate_id()
  db/schema_cloud.surql          # Multi-user: user table, access rules, owner fields
  db/schema_cloud_permissions.surql  # Row-level permissions (owner isolation)
  core/              # Business logic
    recall.py        #   4-tier recall pipeline + assemble_context()
    save.py          #   Save memory with auto-dedup
    search.py        #   BM25 + vector search with graph enrichment
    correct.py       #   Fix/delete/update/unlink memories (soft-delete only)
    link.py          #   Create relationship edges between any nodes
    person.py        #   Create/find person entities with multi-identity contacts
    dedup.py         #   LLM-driven + rule-based fallback dedup
    embeddings.py    #   Voyage AI embedding generation
    scratchpad.py    #   Per-session working memory
    metrics.py       #   Fire-and-forget event tracking
    token_budget.py  #   Hourly rate limiter for background LLM calls
  formatters/        # Memory -> text rendering
    memories.py      #   Evidence markers, category grouping, hypotheses
    graph_map.py     #   Entity graph as readable text
    budget.py        #   Token estimation and budget enforcement
  llm/               # LLM provider abstraction
    base.py          #   Abstract base class
    anthropic_provider.py  # Claude Haiku implementation
  mcp/server.py      # FastMCP server — 7 tools (bootstrap, search, save, correct, link, person, import)
  cli.py             # Click CLI — serve, serve-http, status, schema, worker
  nanobot/           # NanoBot tool entry points (Phase 2 — stubs only)
  worker/            # Background worker (Phase 2 — stub only)
tests/               # Mirrors qmemory/ structure, pytest-asyncio (asyncio_mode = "auto")
schema.surql         # SurrealDB schema (also at qmemory/db/schema.surql)
surrealdb/           # Railway SurrealDB service (separate container)
  Dockerfile         #   Custom Debian + RocksDB (not official image — fixes Railway volume permissions)
  railway.json       #   Railway service config (ON_FAILURE restart)
  backup.sh          #   Backup script (export → gzip)
```

## Key Patterns

- **Every DB call creates a fresh connection** — `async with get_db() as db:`. No connection pooling. This avoids SurrealDB Python SDK "No iterator" bugs with reused async connections.
- **Parameterized queries only** — use `query(db, "SELECT ... WHERE x = $x", {"x": val})`. Never string-interpolate into SurrealQL.
- **RecordID normalization** — SurrealDB SDK returns `RecordID` objects. `normalize_ids()` in `db/client.py` converts them to `"table:id"` strings. All core modules receive normalized data.
- **ID format** — `generate_id("mem")` → `"mem1710864000000abc"` (timestamp + 3 random chars, no dashes).
- **Soft-delete only** — memories are never hard-deleted. `is_active = false` for deleted items.
- **Config via env** — all settings in `qmemory/config.py` via Pydantic Settings. `get_settings()` is cached (call `.cache_clear()` in tests).
- **Railway SurrealDB uses custom Dockerfile** — NOT the official `surrealdb/surrealdb:v3` image. The official image runs as non-root, causing permission errors with Railway volumes. The custom Debian image in `surrealdb/Dockerfile` fixes this.

## Testing

- Tests use a **separate namespace** (`qmemory_test`) — never touches production data.
- The `db` fixture in `tests/conftest.py` applies schema, yields connection, then `REMOVE NAMESPACE` on cleanup.
- 9 known failing tests — all the same issue: SurrealDB edge queries with `WHERE in = type::record(...)` return empty in test sequences. Core logic works; it's a test query pattern issue.

## MCP Tools (7 total)

Two transports: **stdio** (Claude Code, local, `qmemory serve`) and **HTTP** (Claude.ai, remote, `https://mem0.qusai.org/mcp/`).
HTTP requires `Authorization: Bearer qm_ak_xxx`. Stdio has no auth (runs locally).

| Tool | Read-only | Purpose |
|------|-----------|---------|
| `qmemory_bootstrap` | Yes | Load full memory context at conversation start |
| `qmemory_search` | Yes | BM25 + vector search with graph hints |
| `qmemory_save` | No | Save fact with evidence tracking + auto-dedup |
| `qmemory_correct` | No | Fix, delete, update, or unlink a memory |
| `qmemory_link` | No | Create relationship edge between any nodes |
| `qmemory_person` | No | Create/find person with linked identities |
| `qmemory_import` | No | Import markdown file (stub — not yet implemented) |

## Graph Model

- **Nodes**: memory, entity, session, message, tool_call, scratchpad, metrics
- **Edge**: `relates` — single edge table with `type` field for any relationship (supports, contradicts, caused_by, has_identity, etc.)
- **8 memory categories**: self, style, preference, context, decision, idea, feedback, domain
  - `self` memories are always injected first in context

## Code Style

- Type hints everywhere (Python 3.11+ syntax: `str | None`, not `Optional[str]`)
- `from __future__ import annotations` at top of every module
- Pydantic BaseModel for all data types
- Async/await for all DB and LLM operations
- Click for CLI commands
