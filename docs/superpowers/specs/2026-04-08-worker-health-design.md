# Worker + Health Tool Design

**Date:** 2026-04-08
**Status:** Approved
**Scope:** Background worker (linker, dedup, linter) + `qmemory_health` MCP tool

## Problem

Qmemory's graph grows over time but nobody maintains it. Orphan nodes accumulate, duplicates slip through, related memories stay unlinked, and stale facts linger. Today, all of this requires manual effort.

## Solution

Two connected pieces:

1. **Background worker** (`qmemory worker`) — runs 3 jobs on a loop to continuously maintain the graph
2. **`qmemory_health` MCP tool** — reads the worker's latest report so agents can see graph health

The worker is the brain. The health tool just reads what the worker found.

## Architecture

```
+---------------------------------------------+
|              qmemory worker                  |
|                                              |
|  +---------+  +---------+  +------------+    |
|  | Linker  |  |  Dedup  |  |  Linter    |    |
|  | (find & |  | (merge  |  | (orphans,  |    |
|  |  link)  |  |  dupes) |  |  stale,    |    |
|  |         |  |         |  |  quality)  |    |
|  +----+----+  +----+----+  +-----+------+    |
|       |            |             |           |
|       v            v             v           |
|  +-------------------------------------+     |
|  |   health_report table (SurrealDB)   |     |
|  |  stores findings + actions taken    |     |
|  +-------------------------------------+     |
|                                              |
+---------------------------------------------+
         ^                          |
         |                          v
   qmemory worker          qmemory_health tool
   (CLI - runs loop)       (reads latest report)
```

## Worker Jobs

### Job 1: Linker

Finds memories with high vector similarity but no existing edge between them. Asks Claude Haiku whether they should be linked. Creates `relates` edges for confirmed pairs.

**Algorithm:**
1. Select active memories with embeddings
2. For each memory, run vector similarity search (top 10 neighbors)
3. Filter out pairs that already have a `relates` edge
4. Send candidate pairs to Haiku: "Should these be linked? What relationship type?"
5. Create `relates` edges for confirmed pairs
6. Record actions in report

**LLM:** Claude Haiku (same pattern as `dedup.py`)
**Cost control:** Uses existing `token_budget.py` rate limiter

### Job 2: Dedup Worker

Finds near-duplicate memories that slipped past the save-time dedup check (e.g., saved from different sessions, or slight wording variations).

**Algorithm:**
1. Select active memories, grouped by category + scope
2. Within each group, run vector similarity to find high-similarity pairs (threshold: 0.92+)
3. Send candidate pairs to Haiku: "Are these duplicates?"
4. For confirmed dupes, soft-delete the weaker one (lower salience), keep the stronger
5. Record actions in report

**LLM:** Claude Haiku
**Reuses:** `core/dedup.py` comparison logic

### Job 3: Linter (6 Checks)

#### A) Orphans (top priority)
- **Find:** Memories and entities with zero relates edges (no incoming OR outgoing)
- **Query:** Active nodes whose ID appears in neither `in` nor `out` of any `relates` row
- **Auto-fix:** No — flag only. Agent decides whether to link or delete.
- **Severity:** warning

#### B) Contradictions
- **Find:** Pairs of active memories in same category+scope with opposing content
- **Method:** For each category, send batches to Haiku: "Do any of these contradict each other?"
- **Auto-fix:** No — flag the pair. Agent decides which to keep.
- **Severity:** error
- **LLM:** Claude Haiku

#### C) Stale Facts
- **Find:** Memories past `valid_until` date, or salience decayed below 0.1
- **Query:** `WHERE is_active = true AND (valid_until < time::now() OR salience < 0.1)`
- **Auto-fix:** Yes — soft-delete (is_active = false). They're expired by their own rules.
- **Severity:** info

#### D) Missing Links (handled by Linker job)
- Not a separate linter check. The Linker job handles this.
- Linter just reports: "Linker created X links since last run."

#### E) Gaps in Coverage
- **Find:** Categories with surprisingly few memories
- **Query:** `SELECT category, count() FROM memory WHERE is_active = true GROUP BY category`
- **Flag:** Any category with < 3 memories, or zero entities of type 'person'
- **Auto-fix:** No — informational only.
- **Severity:** info

#### F) Data Quality
- **Find:** Broken edges (point to deleted/missing nodes), empty content, invalid categories
- **Queries:** Multiple validation queries
- **Auto-fix:** Yes — delete broken edges, soft-delete empty memories.
- **Severity:** error

### Auto-fix Summary

| Check | Auto-fix? | Reason |
|-------|-----------|--------|
| Orphans | No | Might be valuable, needs judgment |
| Contradictions | No | Needs human/agent judgment |
| Stale facts | Yes | Already expired by their own rules |
| Missing links | Yes (Linker) | That's the Linker's whole purpose |
| Gaps | No | Just informational |
| Data quality | Yes | Objectively broken data |

## Health Report Table

```surql
DEFINE TABLE health_report SCHEMAFULL;

DEFINE FIELD created_at      ON health_report TYPE datetime DEFAULT time::now();
DEFINE FIELD orphans_found   ON health_report TYPE int     DEFAULT 0;
DEFINE FIELD contradictions_found ON health_report TYPE int DEFAULT 0;
DEFINE FIELD stale_found     ON health_report TYPE int     DEFAULT 0;
DEFINE FIELD links_created   ON health_report TYPE int     DEFAULT 0;
DEFINE FIELD dupes_merged    ON health_report TYPE int     DEFAULT 0;
DEFINE FIELD gaps            ON health_report TYPE array   DEFAULT [];
DEFINE FIELD quality_issues  ON health_report TYPE int     DEFAULT 0;
DEFINE FIELD findings        ON health_report TYPE array   DEFAULT [];
DEFINE FIELD duration_ms     ON health_report TYPE int     DEFAULT 0;
```

Each finding in the `findings` array:
```json
{
  "check": "orphan|contradiction|stale|gap|quality|linker|dedup",
  "severity": "info|warning|error",
  "node_id": "memory:mem123abc",
  "detail": "Memory has no connections to any other node",
  "action": {
    "tool": "qmemory_link",
    "params": {"from_id": "memory:mem123", "to_id": "entity:ent456", "type": "relates_to"}
  },
  "fixed": false
}
```

## qmemory_health MCP Tool

```python
qmemory_health(
    check: str = "all"
    # "all" | "orphans" | "contradictions" | "stale" |
    # "missing_links" | "gaps" | "quality"
) -> str  # JSON report
```

- Reads the latest `health_report` from SurrealDB
- If `check != "all"`, filters findings to that check type
- Returns summary counts + findings + suggested actions (same `actions` pattern as other tools)
- If no report exists: returns `"No health report found. Run 'qmemory worker --once' first."`

Added to both transport files:
- `qmemory/mcp/server.py` (stdio)
- `qmemory/app/main.py` (HTTP)

## File Structure

### New Files

```
qmemory/
  worker/
    __init__.py          # currently stub -> becomes runner
    loop.py              # main worker loop (interval, run jobs)
    jobs/
      __init__.py
      linker.py          # find similar -> Haiku -> create edges
      dedup_worker.py    # find near-dupes -> Haiku -> soft-delete
      linter.py          # 6 checks (orphans, contradictions, stale, gaps, quality)
  core/
    health.py            # read latest report, filter by check type
```

### Modified Files

```
qmemory/mcp/server.py   # add qmemory_health tool (stdio)
qmemory/app/main.py     # add qmemory_health tool (HTTP)
qmemory/cli.py           # replace worker stub with real command
qmemory/db/schema.surql  # add health_report table
tests/
  test_worker/
    test_linker.py
    test_dedup_worker.py
    test_linter.py
  test_core/
    test_health.py
```

## CLI Command

```bash
qmemory worker                    # run with defaults (once per day, 86400s)
qmemory worker --interval 3600    # every hour
qmemory worker --once             # run once and exit (for testing/cron)
```

## Dependencies

No new dependencies. Uses existing:
- SurrealDB (graph queries, vector search)
- Claude Haiku via `llm/anthropic_provider.py` (linker, dedup, contradictions)
- Voyage AI via `core/embeddings.py` (vector similarity)
- `token_budget.py` (rate limiting LLM calls)

## Testing

- All tests use `qmemory_test` namespace (existing pattern)
- Each worker job tested independently
- Linter checks tested with seeded data (create orphans, dupes, stale facts, then verify detection)
- Health tool tested by inserting a `health_report` record and reading it back
