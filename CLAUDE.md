# Qmemory

Graph-based memory system for AI agents. Python rebuild (from TypeScript).
Uses SurrealDB as a graph database, exposed via MCP for Claude Code (stdio)
and Claude.ai (HTTP). Multi-user isolation via per-user SurrealDB databases
routed by URL path: `/mcp/u/{user_code}/`.

## Commands

**Always target the remote Railway SurrealDB.** Local SurrealDB is only for
one-off debugging sessions; the canonical development and test target is the
production instance at `wss://surrealdb-production-d9ea.up.railway.app`. Tests
use the `qmemory_test` namespace (separate from `qmemory`), so they never touch
production data.

Export production credentials once per shell (or put them in a gitignored
`.env` — pydantic-settings auto-loads it):

```bash
export QMEMORY_SURREAL_URL="wss://surrealdb-production-d9ea.up.railway.app"
export QMEMORY_SURREAL_USER="root"
export QMEMORY_SURREAL_PASS="$(railway variables --service surrealdb --json | jq -r .SURREAL_PASS)"
```

```bash
# Tests — run against the remote DB in the qmemory_test namespace.
.venv/bin/pytest tests/                       # full suite
.venv/bin/pytest tests/test_core/test_recall.py -v

# CLI
qmemory status                  # check SurrealDB connection + record counts
qmemory serve                   # MCP server (stdio, dev/testing only)
qmemory serve-http --port 3777  # MCP server (HTTP, local)
qmemory schema                  # apply DB schema (safe to re-run)
qmemory worker --once           # run one maintenance cycle and exit
qmemory worker --all-users --interval 3600  # hourly, iterate every active user

# Admin (multi-user deployment)
qmemory admin status                                       # admin DB + user count
qmemory admin list-users                                   # show user routing table
qmemory admin create-db --name <code>                      # provision user_{code}
qmemory admin create-user --user-code <c> --display-name <n> --db-name <db>
```

## Prerequisites

- Python 3.11+ (tests and code use 3.14 via `uv` but 3.11 is the minimum)
- Access to the remote Railway SurrealDB (credentials via `railway variables --service surrealdb`).
  Local SurrealDB is **not** the default target; it's only for offline debugging.
- `.env` file in the project root (gitignored) with keys:
  - `QMEMORY_SURREAL_URL=wss://surrealdb-production-d9ea.up.railway.app`
  - `QMEMORY_SURREAL_USER=root`
  - `QMEMORY_SURREAL_PASS=...` (from Railway)
  - `ANTHROPIC_API_KEY` (for LLM dedup)
  - `VOYAGE_API_KEY` (for embeddings)

## Railway Deployment

SurrealDB runs as a separate Railway service built from `surrealdb/Dockerfile`:
- **Image**: Custom Debian + SurrealDB v3.0.0 (pinned)
- **Engine**: RocksDB at `/data/qmemory.db` (persistent volume at `/data`)
- **URL**: `surrealdb-production-d9ea.up.railway.app`
- **Internal**: `surrealdb.railway.internal:8000` (free, for app→DB)
- **Auth**: root + `SURREAL_PASS` env var (never hardcoded)

**⚠️ Don't use `railway redeploy --yes` on the surrealdb service.** There's a stale `RAILWAY_DOCKERFILE_PATH=Dockerfile.surreal` env var on this service that points to a nonexistent file. Normal builds work because `surrealdb/railway.json` overrides it, but `railway redeploy` bypasses the railway.json and rebuilds against the bogus path, producing a container where `surreal` launches with no subcommand (crash-loop with `error: 'surreal' requires a subcommand`). Always redeploy by pushing a fresh build from the surrealdb directory: `cd surrealdb && railway up --service surrealdb --detach`. This uses the local `railway.json` + `Dockerfile` explicitly and works every time. (Fix the root cause someday by unsetting the stale env var in the Railway dashboard.)

```bash
# Deploy updated Dockerfile to Railway
cd surrealdb && railway up --service surrealdb --detach

# Import schema to Railway
surreal import -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" --namespace qmemory --database main schema.surql

# Backup (run before schema changes!)
./surrealdb/backup.sh
```

**Railway env vars for SurrealDB service**: `PORT=8000`, `SURREAL_PASS`, `SURREAL_LOG=info`, `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE=536870912` (512 MB — see "RAM tuning" below)
**Railway env vars for app service**: `QMEMORY_SURREAL_URL=ws://surrealdb.railway.internal:8000`

### RAM tuning — why the block cache is capped at 512 MB

By default, SurrealDB auto-sizes its RocksDB block cache to a large fraction of the container's available memory. On Railway this came out to **~10.2 GB** of cache for ~9,000 memories of actual data — roughly 600× the working set. The block cache only accelerates *re-reads* of disk pages; sizing it past the dataset gives no benefit.

Setting `SURREAL_ROCKSDB_BLOCK_CACHE_SIZE=536870912` (512 MB) frees ~10 GB of container RAM with no measurable query-latency impact, because:
- Total qmemory data on disk is well under 100 MB at current scale
- HNSW vectors (~18 MB total at 9K memories × 2 KB I16) live in their own cache, not the block cache
- 512 MB is still ~10× the entire on-disk footprint — every hot page fits

Revert: remove the env var → next redeploy returns to the auto-sized default. Verify the active value in the SurrealDB startup logs: look for `Memory manager: block cache size: ...B`.

If the user count grows past ~50 active accounts (today: 3), revisit this number — the working set scales roughly linearly with active users, and the cache should be ~2–4× the hot working-set size, not the full data size.

### Multi-User Architecture (Per-User Database Isolation)

**URL = auth.** Every user gets a personal URL of the form
`https://mem0.qusai.org/mcp/u/{user_code}/` where `user_code` is a slug like
`abacus-k7m3p`. The user code is both the identifier and the secret — whoever
has the URL has full access. No email, no password, no token. Acceptable for
friends-and-family scale; not a production multi-tenant SaaS model.

**Database layout** (in `qmemory` namespace):
- `qmemory.admin` — tiny routing directory. Contains only the `user` table
  (fields: user_code, display_name, db_name, created_at, last_active_at, is_active).
- `qmemory.user_<code>` — one full memory graph per user (memory, entity, relates,
  session, message, scratchpad, health_report, books).
- `qmemory.main` — legacy, read-only safety net preserved for 14 days
  post-migration (2026-04-11 to 2026-04-25). **DO NOT DELETE before 2026-04-25.**

**Request routing**: `qmemory/app/middleware/user_context.py::MCPUserMiddleware`
intercepts `/mcp/u/{code}/...` requests, looks up `db_name` in
`qmemory.admin.user`, sets the `_user_db` ContextVar, rewrites the path to strip
`/u/{code}`, and forwards to the mounted FastMCP sub-app. Core modules don't
know multi-user exists — `get_db()` reads the ContextVar automatically.

**Signup** (`/signup`): zero-friction. Generates a unique user_code from a
filtered EFF word list (`qmemory/app/data/eff_large_wordlist.txt` minus
`excluded_words.txt`, ~7400 words), provisions `user_{code}` via
`provision_user_db()`, inserts the admin row, returns the personal URL inline.
No session is created.

**Legacy `/mcp/` endpoint**: returns HTTP 410 Gone with a pointer to `/signup`.
Only `/mcp/u/{code}/...` is live.

**Worker**: runs on Railway as a dedicated `qmemory-worker` service with start
command `qmemory worker --interval 3600 --all-users`. The worker queries
`admin.user WHERE is_active=true`, sets `_user_db` ContextVar per iteration,
and runs one maintenance cycle (linker → dedup → decay → reflector → linter)
per active user. See `scripts/railway-worker-setup.md` for the setup runbook.

### Transports

**stdio** (local dev/testing only): `qmemory serve`. Reads `QMEMORY_SURREAL_DB`
env var for the target database. Not intended for daily use.

**HTTP** (everyone, including Qusai's own Claude Code): mounted at `/mcp/u/{code}/`.
Configure in Claude Code with:
```
claude mcp add --transport http qmemory https://mem0.qusai.org/mcp/u/{code}/
```
In Claude.ai: Settings → Connectors → Add custom connector → paste the URL.

The HTTP server uses the official `mcp.server.fastmcp.FastMCP` (1.x, bundled
with the Anthropic `mcp` SDK). The jlowin `fastmcp` package is NOT used — do
not re-add it; the single-package rule is structural and prevents the old
"update tools in two places" drift.

## Gotchas

- **`http_app(path="/")` NOT `path="/mcp/"`** — `api.mount("/mcp", ...)` strips the prefix before passing to FastMCP. Using `path="/mcp/"` causes 404.
- **`/mcp/{path:path}` legacy 410 vs `/_mcp` mount** — the FastMCP sub-app is mounted at `/_mcp` (internal-only), `MCPUserMiddleware` rewrites `/mcp/u/{code}/...` → `/_mcp/...`, and the catch-all `/mcp/{path:path}` returns 410 Gone for any direct hits. Mounting at `/mcp` directly would let the 410 catch-all swallow rewritten requests.
- **Schema loader file list** — `apply_schema()` in `db/client.py` loads `schema.surql` then `schema_cloud.surql`. The OAuth schema was deleted in the rebuild. Missing a file causes silent failures (tables don't exist, queries return empty).
- **`apply_admin_schema()` is separate** — `db/admin_schema.surql` (the user routing table) is applied via its own helper, NOT the base loader. Used by `get_admin_db()`.
- **Dockerfile must COPY before pip install** — `COPY . .` then `RUN pip install .`. If you split them (copy pyproject.toml first, install, then copy source), Docker caches the old package and new code changes don't deploy.
- **`SELECT *` includes embedding** — Always use `MEMORY_FIELDS` constant from `recall.py` (or explicit field lists) in queries. Never `SELECT *` from memory/entity — the 1024-float embedding array wastes agent context tokens (~10KB per record).

### SurrealDB v3 BM25 fulltext: 3 verified bugs

The qmemory rebuild discovered three serious v3 bugs in the `@@` operator. Each took hours to diagnose because the failure modes look like data problems, not query problems. Document them here so we don't re-discover them.

1. **`WHERE content @@ $param` returns WRONG rows** — parameterized fulltext binding is silently broken. Only the literal form `@@ "..."` works correctly. Workaround: inline the query as an escaped string literal in the SurrealQL. See `_content_leg` and `_escape_surql_string` in `qmemory/core/search.py`.

2. **`search::score(0)` always returns 0.0** — there's no server-side BM25 relevance score in v3. We rank in Python by term frequency (count of query token substring occurrences in content), salience as tiebreaker.

3. **`@@` is conjunctive (AND), not disjunctive (OR)** — a query with N tokens needs a single memory containing ALL N tokens. When the agent issues a 9-word mixed-language mega-query, content leg returns 0. **Don't paper over this with a Python OR-fallback** — the right fix is the `meta.search_hint` field plus the Query Craft rule in `QMEMORY_INSTRUCTIONS` teaching the agent to issue 2-3-keyword queries in ONE language.

### Other v3 query gotchas

- **`LET $x = (...); SELECT FROM $x;` fails** — LET vars don't persist across statements. Use inline subqueries or two-step Python.
- **`ORDER BY` requires the order idiom in the SELECT** — `ORDER BY in.salience` throws `Missing order idiom 'in.salience' in statement selection` unless `in.salience AS salience` (or similar) appears in the SELECT clause.
- **`ASSERT string::len($value) BETWEEN 4 AND 64` is invalid** — use `>= AND <=` instead.
- **`WHERE id IN (subquery)` is catastrophically slow** for graph traversal — forces a full memory-table scan checking each row against the edge subquery. Took 191 seconds in production for one query. Workaround: traverse FROM the relates table outward (`SELECT in.* FROM relates WHERE out = $eid`), indexed by `out`, ~60x faster.
- **FastMCP DNS rebinding auto-allowlist** — when binding to `127.0.0.1`, FastMCP auto-installs a Host-header allowlist that rejects `mem0.qusai.org` with HTTP 421. Pass `transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)` explicitly.
- **`BaseHTTPMiddleware` doesn't propagate `ContextVar`s** — Starlette runs the downstream app in a separate task context, so `_user_db.set()` in middleware never reaches `get_db()` in core. Use pure ASGI middleware instead (`__call__(scope, receive, send)`).
- **`SELECT id, ... FROM [r1, r2]` returns id=None** — when `FROM` is a list of explicit records, the planner doesn't preserve the row record id; every result row has `id: null` regardless of what's in the SELECT. Workaround: project `meta::id(id) AS _row_id` and reconstruct the full id with the table prefix you already know. Used by `_enrich_with_graph` (search.py) and `_fetch_neighbors_batch` (get.py) to stitch batched results back to inputs by id rather than by position. Don't trust positional zip — a deleted row will misalign every subsequent index.

### Search behavior — `meta.search_hint`

When `qmemory_search` returns 0 content-leg matches AND the query has 4+ tokens, `search_memories()` adds a `meta.search_hint` string to the response telling the agent that the query shape is the problem, not the data. The threshold lives in `LONG_QUERY_TOKEN_THRESHOLD` in `qmemory/core/search.py`. The hint text is fixed in the same file. The agent's `QMEMORY_INSTRUCTIONS` Query Craft section teaches it to read this field and retry with 2-3 keywords in one language.

This is the **fail-loud-instead-of-silent-fallback** approach. We deliberately did NOT add a Python OR-fallback in the content leg because:
- It hides the root cause (agents issuing bad queries) instead of fixing it
- It adds 100+ lines of stopword filtering, two-phase fusion, and dedupe complexity
- Every future agent connecting to qmemory would generate the same mega-queries, and the system would silently paper over them

The `search_hint` + Query Craft rule together cost ~30 lines and teach the agent better behavior on every call.

## Architecture

```
qmemory/
  app/               # Cloud HTTP server (FastAPI + FastMCP)
    main.py          #   FastAPI app, MCP mount (no auth), health check
    config.py        #   AppSettings — QMEMORY_ prefixed env vars
    auth.py          #   resolve_api_token(), create_api_token_for_user() (web UI only)
    routes/oauth.py  #   OAuth 2.0 endpoints (not used by MCP — kept for web UI)
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
    recall.py        #   5-tier recall pipeline (Tier 0: source_type + Tiers 1-4) + assemble_context()
    books.py         #   Hierarchical book browsing (list_books, list_sections, read_section)
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
    health.py        #   Read/save worker health reports
    linter.py        #   6 graph health checks (orphans, stale, gaps, quality)
    dedup_worker.py  #   Batch dedup via word similarity + Haiku
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
- **HNSW indexes use TYPE I16** — Both `memory` and `entity` tables have HNSW vector indexes with `TYPE I16 DIST COSINE EFC 150 M 12`. I16 uses 75% less RAM than the F64 default with <1% recall loss. SurrealDB auto-quantizes float embeddings to I16 internally — no code changes needed. Never use the default (F64) for new indexes.
- **MEMORY_FIELDS constant** — `recall.py` defines `MEMORY_FIELDS` with all memory fields except `embedding`. Use this in all SELECT queries to avoid returning 1024-float arrays. Vector search query adds `vec_score` alongside `MEMORY_FIELDS`.

## Testing

- Tests use a **separate namespace** (`qmemory_test`) — never touches production data.
- The `db` fixture in `tests/conftest.py` applies schema, yields connection, then `REMOVE NAMESPACE` on cleanup.
- 9 known failing tests — all the same issue: SurrealDB v3 edge queries with `<-.id` syntax and `WHERE in = type::record(...)` return empty. Core logic works; it's a SurrealDB v3 syntax change.

## MCP Tools (10 total)

Two transports: **stdio** (Claude Code local, `qmemory serve`) and **HTTP** (Claude.ai + remote Claude Code, `https://mem0.qusai.org/mcp/u/{user_code}/`).

**IMPORTANT**: All 9 tools are defined in a single place — `qmemory/mcp/operations.py`. Both `qmemory/mcp/server.py` (stdio) and `qmemory/app/main.py` (HTTP) mount them via `qmemory.mcp.registry.mount_operations(mcp, OPERATIONS)`. Edit `operations.py` once; both transports pick it up automatically. Pydantic input models live in `qmemory/mcp/schemas.py` and enforce every parameter's type, range, and enum constraints. Error handling goes through `qmemory/mcp/errors.py::safe_tool()` — handlers never raise through the transport layer.

### Server-level instructions (the 8 behavioral rules)

The MCP `instructions` field is sent to clients on `initialize` — once per session, not per tool call. Claude.ai and Claude Code treat it as a connector-level system prompt, so behavioral rules encoded here apply to **every user, every project, automatically** — no per-project copy-paste needed on the Claude.ai side.

**Single source of truth:** `qmemory/mcp/operations.py::QMEMORY_INSTRUCTIONS` (a module-level string constant). Both transports import it. Updates ship with `git push` → Railway redeploy → next session sees the new rules.

**The 8 non-negotiable rules currently encoded:**

1. **BOOTSTRAP FIRST** — every conversation, before any other action
2. **SEARCH BEFORE ANSWERING** — never guess what's in memory
3. **SAVE AS YOU GO** — every decision/preference/correction immediately
4. **LINK WHAT'S CONNECTED** — graph edges turn facts into a brain
5. **CORRECT, DON'T DUPLICATE** — supersede via `qmemory_correct`
6. **CREATE PERSON ENTITIES** — first mention of any named human
7. **FOLLOW THE GRAPH WHEN RESULTS ARE THIN** — two-hop traversal via `qmemory_get(include_neighbors=true, neighbor_depth=2)` rescues searches that missed direct matches
8. **ADD BOOKS PROPERLY** — use `qmemory_add_book` in two phases: create book, then add sections one at a time

Plus a Style section: silent operation, no permission-asking, language-preserving (Arabic stays Arabic), and one-fact-per-memory discipline.

**To update the rules:** edit `QMEMORY_INSTRUCTIONS` in `operations.py`, commit, push. Verify with:
```bash
curl -s -X POST https://mem0.qusai.org/mcp/u/qusai/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"1"}}}' \
  | python3 -c "import sys,json; print(json.loads(sys.stdin.read())['result']['instructions'][:500])"
```

**Do NOT also put the rules in Claude.ai project instructions or in individual tool descriptions** — that creates two sources of truth that drift. The connector-level `instructions` field is the single canonical place. Individual tool descriptions describe **what** the tool does (sent on every tool list); the connector instructions describe **when and how often** to use them (sent once on initialize).

The current instructions string is ~3,700 characters (~900 tokens) — substantial but cheap because it's sent once per session at the handshake, not per tool call. Length budget is generous; prioritize clarity over brevity.

| Tool | Read-only | Purpose |
|------|-----------|---------|
| `qmemory_bootstrap` | Yes | Load full memory context at conversation start |
| `qmemory_search` | Yes | Multi-leg BM25 + RRF + type diversity cap |
| `qmemory_get` | Yes | Fetch by ID + graph neighbor traversal |
| `qmemory_save` | No | Save fact with evidence tracking + auto-dedup |
| `qmemory_correct` | No | Fix, delete, update, or unlink a memory |
| `qmemory_link` | No | Create relationship edge between any nodes |
| `qmemory_person` | No | Create/find person with linked identities |
| `qmemory_books` | Yes | Browse books: list books → sections → content |
| `qmemory_add_book` | No | Add books: create entity, then add sections one at a time |
| `qmemory_health` | Yes | Check graph health — orphans, stale, gaps, quality |

## Book Knowledge

Memory contains insights extracted from 71 books (8,500+ linked ideas).
To access them, use the `source_type` parameter on `qmemory_search`:

- All book insights: `qmemory_search(source_type="from_book")`
- Book insights on a topic: `qmemory_search(query="leadership", source_type="from_book")`
- Link a memory to a book insight: `qmemory_link(from_id="memory:xxx", to_id="memory:yyy", type="supports")`

## Book Browsing

Agents browse books hierarchically instead of flat search:

- **List all books**: `qmemory_books()` or `qmemory_books(query="learning")`
- **See sections**: `qmemory_books(book_id="entity:xxx")`
- **Read section**: `qmemory_books(book_id="entity:xxx", section="Chapter 1")`
- **Link to memory**: `qmemory_link(from_id="memory:chunk", to_id="memory:note", type="supports")`

Memory table has a `section` field (extracted from content headers). Books are `entity` records with `type = 'book'`, linked to memories via `relates` edges with `type = 'from_book'`.

## Graph Model

- **Nodes**: memory, entity, session, message, tool_call, scratchpad, metrics
- **Edge**: `relates` — single edge table with `type` field for any relationship (supports, contradicts, caused_by, has_identity, from_book, etc.)
  - `in` = source node, `out` = target node (e.g. `from_book`: memory is `in`, book entity is `out`)
  - Indexed: `idx_relates_type`, `idx_relates_type_in` (compound for source_type queries)
- **8 memory categories**: self, style, preference, context, decision, idea, feedback, domain
  - `self` memories are always injected first in context

## Code Style

- Type hints everywhere (Python 3.11+ syntax: `str | None`, not `Optional[str]`)
- `from __future__ import annotations` at top of every module
- Pydantic BaseModel for all data types
- Async/await for all DB and LLM operations
- Click for CLI commands
