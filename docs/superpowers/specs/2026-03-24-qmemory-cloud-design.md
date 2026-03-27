# Qmemory Cloud — Multi-User Graph Memory Service

**Date**: 2026-03-24
**Status**: Design approved, pending implementation plan

## Problem

Qmemory runs locally on one Mac. It requires SurrealDB LaunchAgent, NanoBot watchdog, and Python processes — all of which have leaked memory or crashed. Only one user (Qusai) can use it. Friends can't connect.

## Solution

Deploy Qmemory to Railway as a multi-user cloud service. One URL, any channel, any user.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Railway Project: qmemory                                     │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  CORE BUSINESS LOGIC (Python)                          │  │
│  │  core/recall.py, save.py, search.py, correct.py,       │  │
│  │  link.py, person.py, dedup.py, embeddings.py           │  │
│  │  core/linker.py, core/reflector.py, core/decay.py (NEW)│  │
│  └────────────────────┬───────────────────────────────────┘  │
│                       │                                       │
│    ┌──────────┬───────┼──────────┬──────────────┐            │
│    ▼          ▼       ▼          ▼              ▼            │
│  BROWSER   CLAUDE   CLAUDE.AI  NANOBOT       WORKER          │
│  (HTMX)    CODE     (HTTP MCP) (HTTP MCP)   (Background)    │
│  /dash     (HTTP    /mcp/      /mcp/        linker +         │
│  /connect   MCP)                             reflector +     │
│                                              decay           │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  SurrealDB v3 (Railway Volume)                         │  │
│  │  Built-in auth + Row-level permissions                 │  │
│  │  Graph + Vector + BM25 in one                          │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

## Railway Services (3)

| Service | Source | Start Command | Volume | RAM |
|---------|--------|---------------|--------|-----|
| **surrealdb** | Docker: `surrealdb/surrealdb:v3` | `surreal start --user root --pass $SURREAL_PASS --bind 0.0.0.0:8000 surrealkv:/data/qmemory` | `/data` 1GB | 512MB |
| **qmemory-api** | GitHub repo | `uvicorn app.main:api --host 0.0.0.0 --port $PORT` | None | 256MB |
| **qmemory-worker** | Same repo | `python -m qmemory.worker` | None | 256MB |

Estimated cost: ~$13/month for all users.

## Multi-User: SurrealDB Auth + Permissions

### User Signup/Signin

```sql
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
```

### Row-Level Permissions (User A never sees User B)

```sql
DEFINE FIELD owner ON memory TYPE record<user> DEFAULT $auth.id;

DEFINE TABLE memory SCHEMAFULL
  PERMISSIONS
    FOR select, update, delete WHERE owner = $auth.id
    FOR create WHERE $auth.id != NONE;

DEFINE TABLE entity SCHEMAFULL
  PERMISSIONS
    FOR select, update, delete WHERE owner = $auth.id
    FOR create WHERE $auth.id != NONE;

DEFINE TABLE relates SCHEMAFULL TYPE RELATION
  PERMISSIONS
    FOR select, update, delete WHERE in.owner = $auth.id
    FOR create WHERE $auth.id != NONE;
```

### API Tokens (Long-Lived, for MCP)

```sql
DEFINE TABLE api_token SCHEMAFULL
  PERMISSIONS
    FOR select, delete WHERE user = $auth.id
    FOR create WHERE $auth.id != NONE;

DEFINE FIELD user ON api_token TYPE record<user>;
DEFINE FIELD token_hash ON api_token TYPE string;
DEFINE FIELD prefix ON api_token TYPE string;  -- "qm_ak_7f3b" (shown to user)
DEFINE FIELD name ON api_token TYPE string DEFAULT "Default";
DEFINE FIELD created_at ON api_token TYPE datetime DEFAULT time::now();
DEFINE FIELD expires_at ON api_token TYPE datetime;
DEFINE FIELD last_used ON api_token TYPE option<datetime>;
```

Token format: `qm_ak_{random_32_chars}` — prefix `qm_ak_` identifies it as a Qmemory API key.

## Project Structure

```
app/
├── main.py                     # FastAPI + FastMCP mount
├── config.py                   # Settings (env vars)
│
├── core/                       # SHARED BUSINESS LOGIC (unchanged from current)
│   ├── recall.py               # 4-tier recall + assemble_context()
│   ├── save.py                 # Save with dedup
│   ├── search.py               # BM25 + vector search
│   ├── correct.py              # Fix/delete/update/unlink
│   ├── link.py                 # Create relationship edges
│   ├── person.py               # Person entities
│   ├── dedup.py                # LLM + rule-based dedup
│   ├── embeddings.py           # Voyage AI embeddings
│   ├── linker.py               # NEW: Background auto-linking
│   ├── reflector.py            # NEW: Pattern synthesis
│   └── decay.py                # NEW: Salience decay
│
├── routes/                     # BROWSER INTERFACE (HTMX)
│   ├── auth.py                 # Signup, login, logout
│   ├── dashboard.py            # Stats, recent memories
│   ├── connect.py              # MCP connection instructions
│   ├── memories.py             # Browse/search/view memories
│   └── tokens.py               # API token management
│
├── mcp/                        # AGENT INTERFACE (FastMCP)
│   ├── server.py               # FastMCP server + 7 tools
│   └── auth.py                 # Token validation middleware
│
├── worker/                     # BACKGROUND SERVICES
│   ├── __init__.py             # Worker entry point
│   ├── linker.py               # Self-scheduling linker loop
│   ├── reflector.py            # Self-scheduling reflector loop
│   └── decay.py                # Salience decay (pure DB)
│
├── database/
│   ├── connection.py           # SurrealDB connection (fresh per request)
│   └── schema.surql            # Full schema with permissions
│
├── templates/
│   ├── base.html               # Layout (RTL, Arabic-first)
│   ├── pages/
│   │   ├── login.html
│   │   ├── signup.html
│   │   ├── dashboard.html
│   │   ├── connect.html        # MCP setup instructions
│   │   ├── memories.html       # Browse memories
│   │   └── tokens.html         # API token management
│   └── partials/
│       ├── memory_card.html
│       ├── search_results.html
│       └── flash.html
│
└── tests/
```

## MCP Server (FastMCP mounted in FastAPI)

```python
# main.py
from fastapi import FastAPI
from fastmcp import FastMCP

api = FastAPI()
mcp = FastMCP("qmemory", instructions="Graph memory for AI agents...")

# Mount MCP into FastAPI
mcp_app = mcp.http_app(path="/")
api = FastAPI(lifespan=mcp_app.lifespan)
api.mount("/mcp", mcp_app)

# Browser routes on /
# MCP tools on /mcp/
```

### 7 MCP Tools (same as current)

| Tool | Purpose |
|------|---------|
| `qmemory_bootstrap` | Load full memory context for session |
| `qmemory_search` | BM25 + vector search with graph hints |
| `qmemory_save` | Save fact with dedup |
| `qmemory_correct` | Fix/delete/update/unlink |
| `qmemory_link` | Create relationship edges |
| `qmemory_person` | Create/find person entities |
| `qmemory_import` | Import markdown (to be implemented) |

All tools authenticate via API token in the `Authorization` header. The token resolves to a user → SurrealDB enforces row-level permissions.

## Channels: How Each Connects

### Claude Code

```bash
claude mcp add qmemory \
  --transport http \
  --url https://qmemory.up.railway.app/mcp/ \
  --header "Authorization: Bearer qm_ak_..."
```

### Claude.ai

Settings → Integrations → Add MCP Server:
- URL: `https://qmemory.up.railway.app/mcp/`
- Header: `Authorization: Bearer qm_ak_...`

### NanoBot (Donna)

Two connection paths:

**Path 1 — MCP tools** (Donna calls them):
```json
// ~/.nanobot/config.json
"tools": {
  "mcpServers": {
    "qmemory": {
      "type": "streamableHttp",
      "url": "https://qmemory.up.railway.app/mcp/",
      "headers": { "Authorization": "Bearer qm_ak_..." },
      "enabledTools": ["*"]
    }
  }
}
```

**Path 2 — System prompt injection** (automatic, every message):
```python
# nanobot-fork/nanobot/agent/memory.py
def get_memory_context(self, session_key: str = "default") -> str:
    # 1. Try remote Qmemory (Railway)
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

    # 2. Try local Qmemory (SurrealDB on localhost)
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

NanoBot env vars:
```bash
QMEMORY_URL=https://qmemory.up.railway.app
QMEMORY_TOKEN=qm_ak_...
```

## Background Worker

Three self-scheduling loops sharing a token budget:

### Linker (every 5-30 min)

1. Query 10 memories where `linked = false`
2. Fetch 20 recent OTHER memories as candidates
3. One cheap LLM call: "find relationships between these"
4. Validate every LLM-suggested ID against working set (hallucination defense)
5. Create edges via `link_nodes()` with `created_by: "linker"`
6. Mark all 10 as `linked = true` (prevents re-checking)
7. Found work → next run in 5 min. No work → back off to 30 min.

### Reflector (every 10-30 min, staggered from linker)

5 cognitive jobs in ONE LLM call:
- **Patterns**: Find recurring behaviors across memories
- **Contradictions**: Flag conflicting memories → create `contradicts` edge
- **Compressions**: Merge 3+ similar old memories into one principle
- **Ghost entities**: Names mentioned 3+ times without entity → create them
- **Self learnings**: Meta-observations about agent performance

Feedback loop prevention: excludes `source_type = "reflect"` memories.

### Salience Decay (piggybacks on linker, zero LLM cost)

| Tier | Condition | Decay | Floor |
|------|-----------|-------|-------|
| Never recalled + >7 days | `recall_count = 0` | x 0.90 | 0.1 |
| Recalled but stale >14 days | `last_recalled > 14d` | x 0.98 | 0.1 |
| Recalled 5+ times | Cemented | No decay | 0.5 |

Recall boost: `salience += 0.05` (capped at 1.0) on every recall.

### Token Budget (shared)

Sliding window, 1-hour rolling period. Three priority tiers:
- **Critical** (compaction): always allowed
- **Normal** (linker, reflector): until budget full
- **Low** (optional enrichment): 80% of budget

Presets: economy (30K tokens/hr), balanced (80K/hr), aggressive (unlimited).

When idle (no new memories): only DB queries run → zero tokens.

### Worker per User

The worker processes ALL users' memories. It queries:
```sql
SELECT * FROM memory WHERE linked = false AND owner != NONE LIMIT 10;
```
SurrealDB permissions don't apply to root-level worker queries, so the worker uses a root connection (not user tokens). Each user's memories are processed independently.

## Web Dashboard (HTMX)

### Pages

| Route | Page | Purpose |
|-------|------|---------|
| `/login` | Login form | Email + password |
| `/signup` | Signup form | Create account |
| `/dashboard` | Stats overview | Memory count, entity count, recent activity |
| `/connect` | Connection instructions | Copy-paste MCP configs for Claude Code, Claude.ai, NanoBot |
| `/memories` | Browse memories | Search, filter by category, view details |
| `/memories/:id` | Memory detail | Content, linked nodes, evidence chain |
| `/tokens` | API token management | Generate, revoke, view usage |
| `/settings` | Account settings | Change password, export data, delete account |

### Connect Page Flow

1. User logs in → dashboard
2. Clicks "Connect" tab
3. Sees MCP URL (same for everyone) + personal API token
4. Picks their tool (Claude Code / Claude.ai / NanoBot)
5. Sees copy-paste ready config with their token pre-filled
6. Copies one command, pastes in terminal
7. Done — 30 seconds

### Arabic-First

- RTL layout with Cairo font
- Arabic labels with English technical terms
- Uses `base-rtl.html` template from Hotwire skill

## Migration Path (Local → Cloud)

1. Deploy to Railway (SurrealDB + API + Worker)
2. Import existing backup: `surreal import --endpoint ... qmemory-backup.surql`
3. Create user account on web dashboard
4. Assign existing memories to user: `UPDATE memory SET owner = user:USERID WHERE owner = NONE`
5. Generate API token on dashboard
6. Update NanoBot config to use remote MCP
7. Update Claude Code MCP config
8. Remove local SurrealDB LaunchAgent
9. Remove local NanoBot watchdog (or keep NanoBot local, just pointing to remote DB)

## Schema Changes (from current)

New fields added to existing tables:

```sql
-- Every record gets an owner
DEFINE FIELD owner ON memory TYPE record<user> DEFAULT $auth.id;
DEFINE FIELD owner ON entity TYPE record<user> DEFAULT $auth.id;

-- Linker tracking
DEFINE FIELD linked ON memory TYPE bool DEFAULT false;
DEFINE INDEX linked_idx ON memory FIELDS linked;

-- Recall tracking (for decay)
DEFINE FIELD recall_count ON memory TYPE int DEFAULT 0;
DEFINE FIELD last_recalled ON memory TYPE option<datetime>;

-- New tables
DEFINE TABLE user SCHEMAFULL;
DEFINE TABLE api_token SCHEMAFULL;
```

## What Stays the Same

- All 6 core functions (`save_memory`, `search_memories`, `assemble_context`, `correct_memory`, `link_nodes`, `create_person`)
- 4-tier recall pipeline
- Dedup engine (LLM + rule-based fallback)
- Vector embeddings (Voyage AI)
- Memory formatters
- 8 memory categories
- Soft-delete only policy
- Evidence tracking

## What's New

- Multi-user auth (SurrealDB built-in)
- Row-level permissions
- API token management
- HTMX web dashboard
- Background worker (linker, reflector, decay)
- Remote MCP endpoint (Streamable HTTP)
- Railway deployment
- NanoBot memory.py HTTP fallback
