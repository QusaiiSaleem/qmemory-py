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

## Architecture

```
qmemory/
  config.py          # Pydantic Settings — env vars with QMEMORY_ prefix
  constants.py       # 8 memory categories, extraction presets, salience decay
  types.py           # Pydantic models for all graph nodes + edges
  db/client.py       # SurrealDB: get_db(), query(), normalize_ids(), generate_id()
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
```

## Key Patterns

- **Every DB call creates a fresh connection** — `async with get_db() as db:`. No connection pooling. This avoids SurrealDB Python SDK "No iterator" bugs with reused async connections.
- **Parameterized queries only** — use `query(db, "SELECT ... WHERE x = $x", {"x": val})`. Never string-interpolate into SurrealQL.
- **RecordID normalization** — SurrealDB SDK returns `RecordID` objects. `normalize_ids()` in `db/client.py` converts them to `"table:id"` strings. All core modules receive normalized data.
- **ID format** — `generate_id("mem")` → `"mem1710864000000abc"` (timestamp + 3 random chars, no dashes).
- **Soft-delete only** — memories are never hard-deleted. `is_active = false` for deleted items.
- **Config via env** — all settings in `qmemory/config.py` via Pydantic Settings. `get_settings()` is cached (call `.cache_clear()` in tests).

## Testing

- Tests use a **separate namespace** (`qmemory_test`) — never touches production data.
- The `db` fixture in `tests/conftest.py` applies schema, yields connection, then `REMOVE NAMESPACE` on cleanup.
- 9 known failing tests — all the same issue: SurrealDB edge queries with `WHERE in = type::record(...)` return empty in test sequences. Core logic works; it's a test query pattern issue.

## MCP Tools (7 total)

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
