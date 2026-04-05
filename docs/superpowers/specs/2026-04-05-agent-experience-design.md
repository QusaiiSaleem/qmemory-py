# Agent Experience Redesign — qmemory Access Layer

**Date**: 2026-04-05
**Status**: Approved
**Scope**: Incremental improvement to existing tools + 1 new tool

## Problem

qmemory has a strong graph backend (entities, edges, memories) but the access layer
(search + navigation) doesn't expose it. Real-agent testing revealed 8 pain points:

1. **P0** — Search ranking broken: salience dominates, relevance ignored
2. **P0** — No get_by_id: can't fetch memories by ID
3. **P1** — Category filter additive, not restrictive
4. **P1** — No time-based filtering (after/before)
5. **P1** — No graph traversal (neighbors visible but not followable)
6. **P2** — Person entities invisible to search
7. **P2** — No save verification (indexed? embedding generated?)
8. **P3** — No pagination (offset)

## Design Decisions

- **Incremental**: evolve existing 8 tools, add 1 new tool (`qmemory_get`)
- **Approach B**: shared `actions` + `meta` footer on every response, natural top-level keys per tool
- **Bootstrap**: convert from formatted text to structured JSON
- **No schema changes**: same DB tables, same graph model

---

## 1. Response Envelope — Universal Contract

Every qmemory tool response includes:

- **`actions`** (always present): list of structured next-step tool calls
  - Each: `{"tool": "...", "args": {...}, "reason": "..."}`
  - Empty array `[]` if nothing to suggest
- **`meta`** (always present): pagination, timing, counts — varies per tool

Top-level content keys are tool-specific (no wrapper `data` object).

---

## 2. Search Ranking Fix

### Composite Score

```
final_score = (relevance_weight * relevance) + (salience_weight * salience) + recency_bonus
```

**Relevance per source tier:**
- `vector` → `vec_score` (cosine similarity, 0-1)
- `bm25` → `1.0` (flat — search::score() broken in SurrealDB v3)
- `graph` → `0.85` (graph traversal = good signal)
- `recent` → `0.3` (fallback — low confidence)

**Weights (query present):** relevance=0.6, salience=0.3, recency=up to 0.1
**Weights (no query):** relevance=0.0, salience=0.7, recency=up to 0.3

**Recency bonus:** `max(0, 0.1 - (age_in_days * 0.002))`

### Pinned Separation

Memories with `salience >= 0.9` extracted into separate `pinned` array (max 3).
They do NOT compete with relevance-ranked results.

---

## 3. Hard Category Filter

When `category` param is set, ALL tiers add `AND category IN $cats` to WHERE clause:
- Tier 0 (source_type): add clause
- Tier 1 (graph): add clause
- Tier 2 (BM25 + vector): add clause
- Tier 3 (category): already has it
- Tier 4 (recent): add clause

---

## 4. New Search Parameters

```python
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,       # existing — becomes HARD filter
    scope: str | None = None,          # existing
    limit: int = 10,                   # existing
    offset: int = 0,                   # NEW — pagination
    after: str | None = None,          # NEW — "2026-04-05" or ISO datetime
    before: str | None = None,         # NEW — "2026-04-06" or ISO datetime
    source_type: str | None = None,    # existing
    include_tool_calls: bool = False,  # existing
) -> str:
```

- `offset` → SurrealQL `START $offset`
- `after`/`before` → `AND created_at >= $after` / `AND created_at <= $before` on ALL tiers

---

## 5. Entity Inclusion in Search

Parallel entity query when `query` is provided:

```sql
SELECT id, name, type,
  (SELECT count() FROM relates WHERE out = $parent.id GROUP ALL).count AS memory_count
FROM entity
WHERE name @@ $query
   OR string::contains(string::lowercase(name), string::lowercase($query))
LIMIT 5;
```

Results go in `entities` array. Each shows `memory_count` so agent knows if worth exploring.

---

## 6. qmemory_search Response Format

```json
{
  "pinned": [
    {"id": "memory:...", "content": "...", "category": "self", "salience": 1.0}
  ],
  "entities": [
    {"id": "entity:...", "name": "...", "type": "person", "memory_count": 12}
  ],
  "results": [
    {
      "id": "memory:...",
      "content": "...",
      "category": "context",
      "salience": 0.7,
      "relevance": 0.89,
      "source_tier": "vector",
      "age": "5h ago",
      "created_at": "2026-04-05T10:30:00Z",
      "neighbors": {
        "count": 3,
        "items": [
          {"id": "entity:...", "type": "person", "name": "...",
           "edge_type": "source_person", "edge_direction": "out"},
          {"id": "memory:...", "content_preview": "...",
           "edge_type": "supports", "edge_direction": "in"}
        ]
      }
    }
  ],
  "actions": [
    {"tool": "qmemory_get", "args": {"ids": ["entity:..."]},
     "reason": "person linked to 3 results"}
  ],
  "meta": {
    "returned": 10, "offset": 0, "has_more": true, "query_ms": 120
  }
}
```

---

## 7. New Tool — qmemory_get

### Signature

```python
async def qmemory_get(
    ids: list[str],                    # required — max 20
    include_neighbors: bool = False,   # fetch graph neighbors
    neighbor_depth: int = 1,           # 1 or 2
) -> str:
```

### Basic Fetch Response

```json
{
  "memories": [
    {
      "id": "memory:...", "content": "...", "category": "...",
      "salience": 0.7, "age": "2d ago",
      "neighbors": {"count": 3, "items": []}
    }
  ],
  "not_found": ["memory:mem456"],
  "actions": [...],
  "meta": {"found": 1, "requested": 2}
}
```

### With Neighbors Response

Neighbor items include:
- `id`, `type` (person/concept/book for entities, category for memories)
- `name` or `content_preview` (first 80 chars)
- `edge_type` (supports, contradicts, source_person, from_book, etc.)
- `edge_direction` ("in" or "out")
- `edge_id` (for unlinking via qmemory_correct)

### Limits
- Max 20 IDs per call
- `neighbor_depth` max 2
- Max 10 neighbors per node (sorted by edge creation date)

### SurrealQL

```sql
-- Basic fetch
SELECT id, content, category, salience, scope, confidence,
       source_type, evidence_type, is_active, linked, recall_count,
       last_recalled, context_mood, source_person, created_at, updated_at
FROM [memory:mem123, entity:ent789]
WHERE is_active = true OR type IS NOT NONE;

-- Neighbor query
SELECT id,
    ->relates.{id, type, out} AS out_edges,
    <-relates.{id, type, in} AS in_edges,
    ->relates->memory.{id, content, category, salience} AS out_memories,
    ->relates->entity.{id, name, type} AS out_entities,
    <-relates<-memory.{id, content, category, salience} AS in_memories,
    <-relates<-entity.{id, name, type} AS in_entities
FROM [memory:mem123]
```

---

## 8. Updated Responses for Existing Tools

### qmemory_bootstrap

```json
{
  "self_model": [{"id": "...", "content": "..."}],
  "memories": {
    "preference": [{"id": "...", "content": "...", "salience": 0.8, "age": "3d ago"}],
    "context": [...],
    "decision": [...]
  },
  "actions": [
    {"tool": "qmemory_search", "args": {"query": "..."},
     "reason": "847 memories — search for specifics"}
  ],
  "meta": {
    "total_memories": 847,
    "categories": {"self": 5, "preference": 23, "context": 120},
    "oldest": "2025-11-20",
    "newest": "2026-04-05",
    "session_scope": "global"
  }
}
```

### qmemory_save

```json
{
  "memory_id": "memory:...",
  "action": "ADD",
  "content": "...",
  "actions": [
    {"tool": "qmemory_link", "args": {"from_id": "...", "to_id": "..."},
     "reason": "similar memory found during dedup"}
  ],
  "meta": {
    "dedup_checked": true,
    "dedup_candidates": 2,
    "embedding_generated": true,
    "indexed": true
  }
}
```

### qmemory_correct

```json
{
  "ok": true,
  "action": "correct",
  "old_id": "memory:...",
  "new_id": "memory:...",
  "changes": {"content": "old → new"},
  "actions": [
    {"tool": "qmemory_get", "args": {"ids": ["memory:..."], "include_neighbors": true},
     "reason": "verify corrected memory"}
  ],
  "meta": {"version_chain_length": 3}
}
```

### qmemory_link

```json
{
  "edge_id": "relates:...",
  "from": {"id": "memory:...", "content_preview": "..."},
  "to": {"id": "memory:...", "content_preview": "..."},
  "relationship_type": "supports",
  "actions": [
    {"tool": "qmemory_get", "args": {"ids": ["memory:..."], "include_neighbors": true},
     "reason": "source now has 4 connections"}
  ],
  "meta": {"edge_count_from": 4, "edge_count_to": 2}
}
```

### qmemory_person

```json
{
  "entity_id": "entity:...",
  "name": "...",
  "action": "found",
  "contacts": [{"system": "telegram", "handle": "@...", "entity_id": "entity:..."}],
  "actions": [
    {"tool": "qmemory_get", "args": {"ids": ["entity:..."], "include_neighbors": true},
     "reason": "12 memories linked to person"}
  ],
  "meta": {"memory_count": 12, "contact_count": 1}
}
```

### qmemory_books

```json
{
  "books": [{"id": "entity:...", "name": "Rework", "section_count": 12, "memory_count": 89}],
  "actions": [
    {"tool": "qmemory_books", "args": {"book_id": "entity:..."},
     "reason": "browse sections"}
  ],
  "meta": {"total_books": 71, "level": "list"}
}
```

---

## 9. File Changes

```
MODIFIED:
  qmemory/core/recall.py       — ranking, hard filters, after/before/offset, pinned, source_tier
  qmemory/core/search.py       — new response format, entity search, structured actions
  qmemory/core/save.py         — indexed/embedding_generated in response
  qmemory/core/correct.py      — changes detail in response
  qmemory/core/link.py         — both-ends preview in response
  qmemory/core/person.py       — memory_count in response
  qmemory/core/books.py        — meta in response
  qmemory/mcp/server.py        — new qmemory_get, updated search params, updated docstrings
  qmemory/app/main.py          — mirror all server.py changes (both transports in sync)

NEW FILES:
  qmemory/core/get.py          — fetch by ID + neighbor traversal
  qmemory/formatters/actions.py — build_actions() shared helper
  qmemory/formatters/response.py — attach_meta() shared helper
  tests/test_core/test_get.py  — tests for qmemory_get

UNCHANGED:
  schema.surql                 — no DB schema changes
  qmemory/core/dedup.py        — save/dedup logic untouched
  qmemory/core/embeddings.py   — embedding generation untouched
  qmemory/cli.py               — CLI commands untouched
```

---

## 10. Implementation Order

```
Step 1: Core ranking fix (recall.py)
        - source_tier tagging on each tier
        - composite score calculation
        - pinned separation (salience >= 0.9)

Step 2: Hard filters (recall.py)
        - category WHERE clause on ALL tiers
        - after/before date filtering
        - offset pagination (START $offset)

Step 3: New qmemory_get (core/get.py)
        - fetch by IDs (max 20)
        - neighbor traversal (depth 1-2, max 10 per node)
        - not_found array

Step 4: Response format (search.py + formatters/)
        - entity search in parallel
        - new JSON structure (pinned/entities/results)
        - build_actions() helper
        - attach_meta() helper

Step 5: Update all tools' responses
        - save → indexed, embedding_generated
        - correct → changes detail
        - link → both-ends preview
        - person → memory_count
        - bootstrap → structured JSON
        - books → meta + actions

Step 6: MCP layer (server.py + main.py)
        - add qmemory_get to both transports
        - add new params to qmemory_search
        - update docstrings
        - verify both transports in sync
```

Each step is independently testable. Existing 139 tests continue passing.

---

## 11. Error Handling

- DB failures → return empty results with `meta.error` field
- Invalid IDs in qmemory_get → listed in `not_found`, not an error
- Invalid after/before format → ignore filter, log warning
- `actions` always `[]` if nothing to suggest — never missing
- `meta` always present — never missing

## 12. Testing

New tests for:
- Composite ranking (vector result beats high-salience irrelevant result)
- Hard category filter (only requested category in results)
- Date filtering (after/before)
- Pagination (offset + limit)
- qmemory_get basic fetch + not_found
- qmemory_get with neighbors (depth 1 and 2)
- Entity inclusion in search
- Pinned separation (salience >= 0.9 in pinned, not in results)
- build_actions() generates valid tool calls
- attach_meta() always present on every response
