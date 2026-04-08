# Search Engine Rebuild — Multi-Leg BM25 + Dynamic Category-Grouped Results

**Date:** 2026-04-08
**Status:** Approved
**Scope:** Replace `qmemory/core/search.py` with new search pipeline

## Motivation

Lessons learned from Al-Warraq's large-scale book search (21 commits in 3 days):

1. Multi-leg BM25 beats single-leg — searching entity names alongside memory content finds more
2. Split results by type — entities separated from memories, never mixed
3. Pre-fusion extraction — entity/pinned results extracted before RRF so they aren't buried
4. MCP response size caps — prevent Claude.ai truncation
5. Vector search is redundant when BM25 + graph already covers 95% of use cases

Current `search.py` limitations:
- Entity search uses `string::contains` (slow full-table scan, no BM25)
- Vector search runs on every query (Voyage API cost, ~200ms)
- Flat `results[]` list — agent must scan to understand what categories exist
- No `entity_id` filter for scoped search
- Missing database indexes for entity name fulltext and reverse edge lookup

## Architecture

### Pipeline Overview

```
Agent calls qmemory_search(query="Ahmed project")
          |
          +--- Content Leg (BM25 @@ memory.content)
          |       returns: memories
          |
          +--- Entity Leg (BM25 @@ entity.name)
          |       returns: entities
          |
          +--- Graph Leg (entity name match -> relates edges -> memories)
                  returns: memories
          |
          v
    Extract & Separate (dynamic routing)
          |
          v
    RRF Fusion (memories only, from Content + Graph legs)
          |
          v
    Optional Vector Reranker (only if < 5 BM25 results)
          |
          v
    Graph Enrichment (top 5 get neighbors)
          |
          v
    Build Response JSON (dynamic sections)
```

### Three Search Legs

| Leg | What it searches | Index used | Returns |
|-----|-----------------|------------|---------|
| Content | `memory.content @@ $query` | `idx_memory_content` (existing BM25) | memories |
| Entity | `entity.name @@ $query` | `idx_entity_name_ft` (NEW BM25) | entities |
| Graph | entity name match -> traverse `relates` edges | `idx_relates_type_out` (NEW) | memories |

All three legs run in parallel (asyncio.gather).

### RRF Fusion

Reciprocal Rank Fusion combines memory results from Content + Graph legs:

```
score = sum(1 / (k + rank)) for each leg where the memory appears
k = 60 (standard RRF constant)
```

Entity Leg results are extracted BEFORE fusion — they go directly to `entities_matched[]`.

### Vector Reranker (Optional)

Vector search is demoted from always-on to optional:
- Only fires when BM25 returns < 5 memory results
- Uses existing Voyage AI embeddings + HNSW index
- Reranks BM25 candidates by cosine similarity (does not fetch new results)
- Saves ~90% of Voyage API calls

No changes to `embeddings.py` — it still generates embeddings on save. The change is that search stops calling `generate_query_embedding()` by default.

### Extract & Separate (Dynamic Routing)

Each result is inspected and routed to the appropriate section:

| Check | Route to | Condition |
|-------|----------|-----------|
| Entity Leg result | `entities_matched[]` | Always — entities never mixed with memories |
| Memory `salience >= 0.9` | `pinned[]` | Extracted before RRF, max 3 |
| Memory `confidence < 0.5` | `hypotheses[]` | Low-confidence = needs verification |
| Memory has `from_book` edge | `book_insights[]` | Checked via relates edge lookup |
| All other memories | `memories.{category}` | Grouped by category, ranked within |

Rules:
- Empty sections are omitted entirely (no `"decision": []`)
- `self` category always comes first in the `memories` object
- Within each category, sorted by relevance DESC
- `meta.sections` lists which sections are present in the response

## Response Contract

```json
{
  "entities_matched": [
    {
      "id": "entity:ahmed_khalil",
      "name": "Ahmed Khalil",
      "type": "person",
      "memory_count": 12,
      "actions": {
        "get": {"tool": "qmemory_get", "args": {"ids": ["entity:ahmed_khalil"], "include_neighbors": true}},
        "search_within": {"tool": "qmemory_search", "args": {"entity_id": "entity:ahmed_khalil"}}
      }
    }
  ],

  "pinned": [
    {
      "id": "memory:mem_critical",
      "content": "Ahmed is the CTO - all architecture decisions go through him",
      "category": "context",
      "salience": 0.95,
      "age": "5d ago"
    }
  ],

  "memories": {
    "self": [
      {
        "id": "memory:mem_self_001",
        "content": "I support Ahmed's team as their AI assistant",
        "relevance": 0.72,
        "salience": 0.8,
        "found_by": "content",
        "age": "2d ago",
        "graph": {
          "entities": [{"id": "entity:ahmed_khalil", "name": "Ahmed Khalil", "edge": "about"}],
          "related": [{"id": "memory:mem789", "preview": "Ahmed manages mobile team", "edge": "supports"}],
          "from_book": null
        },
        "actions": {
          "correct": {"tool": "qmemory_correct", "args": {"memory_id": "memory:mem_self_001"}},
          "link": {"tool": "qmemory_link", "args": {"from_id": "memory:mem_self_001"}},
          "get_neighbors": {"tool": "qmemory_get", "args": {"ids": ["memory:mem_self_001"], "include_neighbors": true}}
        }
      }
    ],
    "preference": [
      {
        "id": "memory:mem456",
        "content": "Ahmed prefers async communication over meetings",
        "relevance": 0.85,
        "salience": 0.7,
        "found_by": "content",
        "age": "3d ago",
        "graph": {
          "entities": [{"id": "entity:ahmed_khalil", "name": "Ahmed Khalil", "edge": "about"}],
          "related": [],
          "from_book": null
        },
        "actions": {
          "correct": {"tool": "qmemory_correct", "args": {"memory_id": "memory:mem456"}},
          "link": {"tool": "qmemory_link", "args": {"from_id": "memory:mem456"}},
          "get_neighbors": {"tool": "qmemory_get", "args": {"ids": ["memory:mem456"], "include_neighbors": true}}
        }
      }
    ],
    "context": [
      {
        "id": "memory:mem_ctx",
        "content": "Ahmed manages the mobile team at Rakeezah",
        "relevance": 0.92,
        "salience": 0.7,
        "found_by": "graph",
        "age": "1d ago",
        "graph": {
          "entities": [
            {"id": "entity:ahmed_khalil", "name": "Ahmed Khalil", "edge": "about"},
            {"id": "entity:rakeezah", "name": "Rakeezah", "edge": "belongs_to"}
          ],
          "related": [],
          "from_book": null
        },
        "actions": {
          "correct": {"tool": "qmemory_correct", "args": {"memory_id": "memory:mem_ctx"}},
          "link": {"tool": "qmemory_link", "args": {"from_id": "memory:mem_ctx"}},
          "get_neighbors": {"tool": "qmemory_get", "args": {"ids": ["memory:mem_ctx"], "include_neighbors": true}}
        }
      }
    ]
  },

  "book_insights": [
    {
      "id": "memory:mem_book_001",
      "content": "Effective leaders delegate through trust, not control",
      "book": {"id": "entity:book_123", "title": "The Art of Leadership"},
      "section": "Chapter 3: Delegation",
      "relevance": 0.68,
      "actions": {
        "read_section": {"tool": "qmemory_books", "args": {"book_id": "entity:book_123", "section": "Chapter 3: Delegation"}},
        "browse_book": {"tool": "qmemory_books", "args": {"book_id": "entity:book_123"}}
      }
    }
  ],

  "hypotheses": [
    {
      "id": "memory:mem_hyp",
      "content": "Ahmed might be considering leaving the project",
      "confidence": 0.3,
      "evidence_type": "inferred",
      "category": "context",
      "actions": {
        "verify": {"tool": "qmemory_correct", "args": {"memory_id": "memory:mem_hyp", "action": "update"}}
      }
    }
  ],

  "actions": [
    {"tool": "qmemory_search", "args": {"query": "Ahmed", "category": "decision"}, "reason": "12 entity memories - narrow by category"}
  ],

  "meta": {
    "by_category": {"self": 1, "preference": 1, "context": 1},
    "total_found": 45,
    "returned": 6,
    "offset": 0,
    "has_more": true,
    "sections": ["entities_matched", "pinned", "memories", "book_insights", "hypotheses"],
    "search_legs": {"content": 4, "entity": 1, "graph": 3},
    "vector_rerank": false
  }
}
```

All sections are dynamic — only present when results exist for them.

## Schema Changes

Three new indexes added to `schema.surql`:

```sql
-- BM25 fulltext on entity names (enables Entity Leg)
DEFINE INDEX IF NOT EXISTS idx_entity_name_ft ON entity
  FIELDS name FULLTEXT ANALYZER qmemory_analyzer;

-- Reverse edge lookup by type + target (enables efficient graph traversal)
DEFINE INDEX IF NOT EXISTS idx_relates_type_out ON relates FIELDS type, out;

-- Source type filter on memory (enables filtering by origin)
DEFINE INDEX IF NOT EXISTS idx_memory_source_type ON memory FIELDS source_type;
```

Note: `entity.aliases` is an array field — SurrealDB cannot BM25 index arrays. Alias matching falls back to Python-side filtering after the Entity Leg BM25 returns results.

## Files Changed

| File | Change | Risk |
|------|--------|------|
| `schema.surql` | Add 3 new indexes | Low — additive, IF NOT EXISTS |
| `qmemory/core/search.py` | Full rewrite — new pipeline | High — complete replacement |
| `qmemory/core/recall.py` | Vector search becomes optional reranker in Tier 2 | Medium — behavior change |
| `qmemory/mcp/server.py` | Add `entity_id` param to `qmemory_search` | Low — additive param |
| `qmemory/app/main.py` | Same `entity_id` param (keep transports in sync) | Low — additive param |
| `qmemory/formatters/actions.py` | Extend for per-result + per-entity actions | Medium — new action types |
| `tests/test_core/test_search.py` | Full rewrite — new tests for new pipeline | High — complete replacement |

### Files Untouched

- `qmemory/core/recall.py` `assemble_context()` — bootstrap still uses 4-tier pipeline
- `qmemory/core/get.py` — no changes
- `qmemory/core/save.py` — still generates embeddings on save (kept for reranker)
- `qmemory/core/correct.py`, `link.py`, `person.py`, `books.py` — no changes
- `qmemory/core/embeddings.py` — no changes (still used by save + reranker)
- `qmemory/formatters/response.py` — no changes

## New MCP Parameter

`entity_id` on `qmemory_search` — scopes all 3 search legs to memories linked to one entity:

- Content Leg: adds `AND id IN (SELECT VALUE in FROM relates WHERE out = <record>$entity_id)`
- Entity Leg: skipped (caller already knows the entity)
- Graph Leg: starts from this entity directly instead of name matching

Use case: "Search within Ahmed's memories" — `qmemory_search(query="budget", entity_id="entity:ahmed_khalil")`

## Key Decisions

1. **Category grouping over flat list** — `memories` is a dict keyed by category, not a flat array. Within each category, results are ranked by relevance. Empty categories omitted.
2. **`self` always first** — self-model memories define the agent's identity and always appear first in the memories object.
3. **Vector demoted, not removed** — embeddings still generated on save (for future use), but search only calls Voyage API when BM25 returns < 5 results.
4. **RRF over composite score** — replaces the current weighted composite score (0.6 relevance + 0.3 salience + recency) with standard RRF fusion (1/(60+rank)). Simpler, proven, scale-agnostic.
5. **Per-result actions** — every memory and entity gets its own `actions` dict with ready-to-use tool calls. Agents copy-paste instead of constructing calls.
6. **Graph always present** — every memory result includes `graph` with linked entities, related memories, and book source. This is the core value of a graph memory system.
7. **Parallel search legs** — all 3 legs run via `asyncio.gather` for minimal latency.
8. **`from_book` detection** — single batch query checks if any memory result has a relates edge to a book entity. Those get routed to `book_insights[]` with browse/read actions.
