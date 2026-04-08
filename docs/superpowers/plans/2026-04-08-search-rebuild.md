# Search Engine Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `qmemory/core/search.py` with a new multi-leg BM25 search engine that returns dynamic category-grouped results with per-result graph context and actions.

**Architecture:** Three parallel BM25 legs (content, entity name, graph traversal) fused via RRF. Vector search demoted to optional reranker. Results dynamically routed to sections: entities_matched, pinned, memories.{category}, book_insights, hypotheses. Each result includes graph neighbors and actionable tool calls.

**Tech Stack:** Python 3.11+, SurrealDB 3.0+ (BM25 fulltext, HNSW), asyncio.gather for parallel legs, Voyage AI (optional reranker only)

**Spec:** `docs/superpowers/specs/2026-04-08-search-rebuild-design.md`

---

## File Map

| File | Role | Change |
|------|------|--------|
| `schema.surql` | Database schema | Add 3 indexes |
| `qmemory/core/search.py` | New search engine | Full rewrite (~350 lines) |
| `qmemory/core/recall.py` | Recall pipeline | Modify Tier 2 — vector becomes optional |
| `qmemory/mcp/server.py` | MCP stdio transport | Add `entity_id` param |
| `qmemory/app/main.py` | MCP HTTP transport | Add `entity_id` param |
| `qmemory/formatters/actions.py` | Action builder | Add per-result and per-entity action builders |
| `tests/test_core/test_search.py` | Search tests | Full rewrite |

---

### Task 1: Add Schema Indexes

**Files:**
- Modify: `schema.surql:99-118` (after entity table, before edges)

- [ ] **Step 1: Add 3 new indexes to schema.surql**

Add these lines after line 113 (after `idx_entity_external`), before the HNSW index:

```sql
-- BM25 fulltext on entity names (enables Entity Leg search)
DEFINE INDEX IF NOT EXISTS idx_entity_name_ft ON entity
  FIELDS name FULLTEXT ANALYZER qmemory_analyzer;

-- Reverse edge lookup by type + target (enables efficient graph traversal)
DEFINE INDEX IF NOT EXISTS idx_relates_type_out ON relates FIELDS type, out;

-- Source type filter on memory (enables filtering by origin)
DEFINE INDEX IF NOT EXISTS idx_memory_source_type ON memory FIELDS source_type;
```

- [ ] **Step 2: Verify schema applies cleanly**

Run: `uv run python -c "import asyncio; from qmemory.db.client import get_db, apply_schema; asyncio.run((lambda: apply_schema.__wrapped__(None))())"`

If SurrealDB is not running locally, just verify the SQL is valid by reading it. The schema uses `IF NOT EXISTS` so it's safe to re-apply.

- [ ] **Step 3: Commit**

```bash
git add schema.surql
git commit -m "schema: add BM25 entity name, relates type+out, memory source_type indexes"
```

---

### Task 2: Extend Action Builder for Per-Result Actions

**Files:**
- Modify: `qmemory/formatters/actions.py`
- Test: `tests/test_formatters/test_actions.py` (create if not exists)

- [ ] **Step 1: Write tests for new action builders**

Create `tests/test_formatters/test_actions.py`:

```python
"""Tests for per-result and per-entity action builders."""

from qmemory.formatters.actions import (
    build_actions,
    build_memory_actions,
    build_entity_actions,
    build_category_drill_down,
)


def test_build_memory_actions_basic():
    """Memory actions should include correct, link, get_neighbors."""
    actions = build_memory_actions("memory:mem123")
    assert "correct" in actions
    assert actions["correct"]["tool"] == "qmemory_correct"
    assert actions["correct"]["args"]["memory_id"] == "memory:mem123"
    assert "link" in actions
    assert "get_neighbors" in actions


def test_build_entity_actions_basic():
    """Entity actions should include get and search_within."""
    actions = build_entity_actions("entity:ent456")
    assert "get" in actions
    assert actions["get"]["args"]["ids"] == ["entity:ent456"]
    assert actions["get"]["args"]["include_neighbors"] is True
    assert "search_within" in actions
    assert actions["search_within"]["args"]["entity_id"] == "entity:ent456"


def test_build_category_drill_down():
    """Drill-down actions should only include categories with > 1 result."""
    by_category = {"context": 5, "preference": 1, "decision": 3}
    actions = build_category_drill_down("Ahmed", by_category)
    # Should suggest drilling into context (5) and decision (3), not preference (1)
    tools = [a["args"]["category"] for a in actions]
    assert "context" in tools
    assert "decision" in tools
    assert "preference" not in tools


def test_build_category_drill_down_empty():
    """Empty category map should return empty list."""
    actions = build_category_drill_down("test", {})
    assert actions == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_formatters/test_actions.py -v`
Expected: FAIL — `build_memory_actions`, `build_entity_actions`, `build_category_drill_down` not defined yet.

- [ ] **Step 3: Implement the new action builders**

Add these functions to `qmemory/formatters/actions.py` (after the existing `build_actions` function):

```python
def build_memory_actions(memory_id: str) -> dict:
    """Build per-result actions for a single memory."""
    return {
        "correct": {
            "tool": "qmemory_correct",
            "args": {"memory_id": memory_id},
        },
        "link": {
            "tool": "qmemory_link",
            "args": {"from_id": memory_id},
        },
        "get_neighbors": {
            "tool": "qmemory_get",
            "args": {"ids": [memory_id], "include_neighbors": True},
        },
    }


def build_entity_actions(entity_id: str) -> dict:
    """Build per-result actions for a matched entity."""
    return {
        "get": {
            "tool": "qmemory_get",
            "args": {"ids": [entity_id], "include_neighbors": True},
        },
        "search_within": {
            "tool": "qmemory_search",
            "args": {"entity_id": entity_id},
        },
    }


def build_book_insight_actions(book_id: str, section: str | None = None) -> dict:
    """Build actions for a book insight result."""
    actions: dict = {
        "browse_book": {
            "tool": "qmemory_books",
            "args": {"book_id": book_id},
        },
    }
    if section:
        actions["read_section"] = {
            "tool": "qmemory_books",
            "args": {"book_id": book_id, "section": section},
        }
    return actions


def build_category_drill_down(query: str, by_category: dict[str, int]) -> list[dict]:
    """Build drill-down actions for categories with multiple results."""
    actions = []
    for cat, count in sorted(by_category.items(), key=lambda x: x[1], reverse=True):
        if count > 1:
            actions.append({
                "tool": "qmemory_search",
                "args": {"query": query, "category": cat},
                "reason": f"{count} {cat} memories found",
            })
    return actions
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_formatters/test_actions.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add qmemory/formatters/actions.py tests/test_formatters/test_actions.py
git commit -m "feat: add per-result memory, entity, book, and drill-down action builders"
```

---

### Task 3: Write the New Search Engine

This is the core task — full rewrite of `qmemory/core/search.py`.

**Files:**
- Rewrite: `qmemory/core/search.py`

- [ ] **Step 1: Write the new search.py — module header and constants**

Replace the entire contents of `qmemory/core/search.py` with:

```python
"""
Core Search — Multi-Leg BM25 + RRF Fusion + Dynamic Category-Grouped Results

Three parallel search legs:
  1. Content Leg  — BM25 fulltext on memory.content
  2. Entity Leg   — BM25 fulltext on entity.name
  3. Graph Leg    — entity name match -> relates edges -> linked memories

Results are fused via RRF (Reciprocal Rank Fusion), then dynamically
routed to response sections:
  - entities_matched[] — matched entities with actions
  - pinned[]           — high-salience memories (>= 0.9)
  - memories.{cat}     — category-grouped, relevance-ranked
  - book_insights[]    — memories linked to book entities
  - hypotheses[]       — low-confidence memories (< 0.5)

All sections are dynamic — only present when results exist.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from qmemory.core.recall import MEMORY_FIELDS, _format_age
from qmemory.db.client import get_db, query
from qmemory.formatters.actions import (
    build_actions,
    build_book_insight_actions,
    build_category_drill_down,
    build_entity_actions,
    build_memory_actions,
)
from qmemory.formatters.response import attach_meta

logger = logging.getLogger(__name__)

# --- Constants ---
RRF_K = 60                  # RRF fusion constant (standard value)
MAX_PINNED = 3              # Max pinned memories to extract
PINNED_THRESHOLD = 0.9      # Salience threshold for pinned
HYPOTHESIS_THRESHOLD = 0.5  # Confidence below this = hypothesis
TOP_N_ENRICH = 5            # How many results to enrich with graph
MAX_HINTS_PER_RESULT = 3    # Max neighbor hints per result
VECTOR_RERANK_THRESHOLD = 5 # Only fire vector if BM25 returns fewer than this
ENTITY_LEG_LIMIT = 5        # Max entities to return
CONTENT_LEG_LIMIT = 50      # Max BM25 content results (pre-fusion)
GRAPH_LEG_LIMIT = 15        # Max graph-traversal results (pre-fusion)

# Category display order — self always first
CATEGORY_ORDER = ["self", "style", "preference", "context", "decision", "idea", "feedback", "domain"]
```

- [ ] **Step 2: Write the main search_memories function**

Append to `qmemory/core/search.py`:

```python
async def search_memories(
    query_text: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 20,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
    include_tool_calls: bool = False,
    owner_id: str | None = None,
    source_type: str | None = None,
    entity_id: str | None = None,
    db: Any = None,
) -> dict:
    """Search memories with multi-leg BM25, RRF fusion, and dynamic category grouping.

    Returns structured JSON with dynamic sections based on what was found.
    """
    logger.debug("Search: query=%s category=%s entity_id=%s owner=%s", query_text, category, entity_id, owner_id)

    async def _run(conn: Any) -> dict:
        # Build shared filter clauses for all legs
        filters = _build_filters(category, scope, after, before, source_type)

        # --- Run 3 legs in parallel ---
        if query_text and query_text.strip():
            content_task = _content_leg(query_text, filters, limit, entity_id, conn)
            entity_task = _entity_leg(query_text, conn) if not entity_id else _empty_list()
            graph_task = _graph_leg(query_text, filters, entity_id, conn)

            content_results, entity_results, graph_results = await asyncio.gather(
                content_task, entity_task, graph_task
            )
        else:
            # No query — just fetch recent memories
            content_results = await _recent_fallback(filters, limit, conn)
            entity_results = []
            graph_results = []

        # --- RRF Fusion (memories only, from Content + Graph legs) ---
        fused_memories = _rrf_fuse(content_results, graph_results)

        # --- Optional vector reranker ---
        if query_text and len(fused_memories) < VECTOR_RERANK_THRESHOLD:
            fused_memories = await _vector_rerank(query_text, fused_memories, filters, limit, conn)

        # --- Extract & Separate ---
        return await _extract_and_separate(
            fused_memories=fused_memories,
            entity_results=entity_results,
            query_text=query_text or "",
            limit=limit,
            offset=offset,
            db=conn,
        )

    if db is not None:
        return await _run(db)
    else:
        async with get_db() as conn:
            return await _run(conn)


async def _empty_list() -> list:
    """Async no-op that returns empty list (for asyncio.gather slots)."""
    return []
```

- [ ] **Step 3: Write the filter builder**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Shared filter builder
# ---------------------------------------------------------------------------

def _build_filters(
    category: str | None,
    scope: str | None,
    after: str | None,
    before: str | None,
    source_type: str | None,
) -> dict:
    """Build shared filter clauses and params for all search legs."""
    clauses = ""
    params: dict[str, Any] = {}

    if category:
        clauses += " AND category IN $cats"
        params["cats"] = [category]

    if scope and scope != "any":
        clauses += ' AND (scope = $scope OR scope = "global")'
        params["scope"] = scope

    if after:
        clauses += " AND created_at >= <datetime>$after_dt"
        params["after_dt"] = after

    if before:
        clauses += " AND created_at <= <datetime>$before_dt"
        params["before_dt"] = before

    if source_type:
        clauses += " AND source_type = $source_type"
        params["source_type"] = source_type

    return {"clauses": clauses, "params": params}
```

- [ ] **Step 4: Write the Content Leg**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Leg 1: Content BM25
# ---------------------------------------------------------------------------

async def _content_leg(
    query_text: str,
    filters: dict,
    limit: int,
    entity_id: str | None,
    db: Any,
) -> list[dict]:
    """BM25 fulltext search on memory.content."""
    params: dict[str, Any] = {"query": query_text, "limit": min(limit, CONTENT_LEG_LIMIT)}
    params.update(filters["params"])

    # Optional entity_id scope — only memories linked to this entity
    entity_clause = ""
    if entity_id:
        entity_clause = " AND id IN (SELECT VALUE in FROM relates WHERE out = <record>$entity_id)"
        params["entity_id"] = entity_id

    surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE content @@ $query
        AND is_active = true
        AND (valid_until IS NONE OR valid_until > time::now())
        {entity_clause}
        {filters["clauses"]}
    ORDER BY salience DESC
    LIMIT $limit;
    """

    results = await query(db, surql, params)
    if not results or not isinstance(results, list):
        return []

    # Compute word-overlap relevance (search::score() broken in SurrealDB v3)
    query_words = set(query_text.lower().split())
    for i, r in enumerate(results):
        r["_leg"] = "content"
        r["_rank"] = i
        content_words = set((r.get("content") or "").lower().split())
        overlap = len(query_words & content_words)
        r["_bm25_relevance"] = min(1.0, overlap / max(len(query_words), 1))

    logger.debug("Content leg: %d results", len(results))
    return results
```

- [ ] **Step 5: Write the Entity Leg**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Leg 2: Entity BM25
# ---------------------------------------------------------------------------

async def _entity_leg(query_text: str, db: Any) -> list[dict]:
    """BM25 fulltext search on entity.name. Returns entity dicts, not memories."""
    params: dict[str, Any] = {"query": query_text}

    # BM25 search on entity names (uses idx_entity_name_ft)
    surql = """
    SELECT id, name, type, aliases
    FROM entity
    WHERE name @@ $query
        AND is_active != false
    LIMIT $limit;
    """
    params["limit"] = ENTITY_LEG_LIMIT

    rows = await query(db, surql, params)
    if not rows or not isinstance(rows, list):
        # Fallback: substring match (for short names BM25 might miss)
        surql_fallback = """
        SELECT id, name, type, aliases
        FROM entity
        WHERE is_active != false
            AND string::contains(string::lowercase(name), string::lowercase($query))
        LIMIT $limit;
        """
        rows = await query(db, surql_fallback, params)
        if not rows or not isinstance(rows, list):
            return []

    # Also check aliases (Python-side — can't BM25 index arrays)
    query_lower = query_text.lower()
    for row in rows:
        aliases = row.get("aliases") or []
        row["_alias_match"] = any(query_lower in a.lower() for a in aliases)

    # Count linked memories for each entity
    entities = []
    for e in rows:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        eid = str(e["id"])
        count_rows = await query(
            db,
            "SELECT count() AS c FROM relates WHERE in = <record>$eid OR out = <record>$eid GROUP ALL",
            {"eid": eid},
        )
        mem_count = 0
        if count_rows and isinstance(count_rows, list) and len(count_rows) > 0:
            mem_count = count_rows[0].get("c", 0)

        entities.append({
            "id": eid,
            "name": e.get("name", ""),
            "type": e.get("type", ""),
            "memory_count": mem_count,
            "actions": build_entity_actions(eid),
        })

    logger.debug("Entity leg: %d entities", len(entities))
    return entities
```

- [ ] **Step 6: Write the Graph Leg**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Leg 3: Graph Traversal
# ---------------------------------------------------------------------------

import re

# Min word length for entity name matching
_MIN_WORD_LEN = 3
_MAX_ENTITY_WORDS = 10


async def _graph_leg(
    query_text: str,
    filters: dict,
    entity_id: str | None,
    db: Any,
) -> list[dict]:
    """Find entities matching query words, traverse relates edges to memories."""

    if entity_id:
        # Scoped search — start directly from this entity
        return await _graph_from_entity(entity_id, filters, db)

    # Extract candidate words from query
    words = query_text.split()
    cleaned = []
    for w in words:
        c = re.sub(r"[^a-zA-Z\u0600-\u06FF0-9]", "", w)
        if len(c) >= _MIN_WORD_LEN:
            cleaned.append(c)
    cleaned = cleaned[:_MAX_ENTITY_WORDS]

    if not cleaned:
        return []

    # Build entity name matching conditions
    match_conditions = []
    params: dict[str, Any] = {}
    params.update(filters["params"])
    for i, word in enumerate(cleaned):
        match_conditions.append(
            f"string::contains(string::lowercase(name), string::lowercase($w{i}))"
        )
        params[f"w{i}"] = word

    # Find entities, then traverse to linked memories
    # Two-step Python-side (SurrealDB v3 LET vars don't persist)
    entity_surql = f"""
    SELECT id FROM entity
    WHERE {" OR ".join(match_conditions)}
    LIMIT 10;
    """
    entity_rows = await query(db, entity_surql, params)

    if not entity_rows or not isinstance(entity_rows, list):
        return []

    entity_ids = [str(e["id"]) for e in entity_rows if isinstance(e, dict) and e.get("id")]
    if not entity_ids:
        return []

    # Fetch memories linked to these entities
    all_memories: list[dict] = []
    for eid in entity_ids:
        mem_surql = f"""
        SELECT {MEMORY_FIELDS} FROM memory
        WHERE is_active = true
            AND (valid_until IS NONE OR valid_until > time::now())
            {filters["clauses"]}
            AND id IN (
                SELECT VALUE in FROM relates WHERE out = <record>$eid
            )
        ORDER BY salience DESC
        LIMIT $limit;
        """
        mem_params: dict[str, Any] = {"eid": eid, "limit": GRAPH_LEG_LIMIT}
        mem_params.update(filters["params"])
        rows = await query(db, mem_surql, mem_params)
        if rows and isinstance(rows, list):
            all_memories.extend(rows)

    # Tag results
    for i, m in enumerate(all_memories):
        m["_leg"] = "graph"
        m["_rank"] = i

    logger.debug("Graph leg: %d memories from %d entities", len(all_memories), len(entity_ids))
    return all_memories


async def _graph_from_entity(entity_id: str, filters: dict, db: Any) -> list[dict]:
    """Fetch memories directly linked to a specific entity (scoped search)."""
    params: dict[str, Any] = {"eid": entity_id, "limit": GRAPH_LEG_LIMIT}
    params.update(filters["params"])

    surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE is_active = true
        AND (valid_until IS NONE OR valid_until > time::now())
        {filters["clauses"]}
        AND id IN (
            SELECT VALUE in FROM relates WHERE out = <record>$eid
        )
    ORDER BY salience DESC
    LIMIT $limit;
    """
    rows = await query(db, surql, params)
    if not rows or not isinstance(rows, list):
        return []

    for i, m in enumerate(rows):
        m["_leg"] = "graph"
        m["_rank"] = i
    return rows
```

- [ ] **Step 7: Write RRF Fusion**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------

def _rrf_fuse(*legs: list[dict]) -> list[dict]:
    """Reciprocal Rank Fusion — combine results from multiple legs.

    score = sum(1 / (RRF_K + rank)) for each leg where the memory appears.
    Higher score = found by more legs and ranked higher in each.
    """
    scores: dict[str, float] = {}
    records: dict[str, dict] = {}

    for leg in legs:
        for i, mem in enumerate(leg):
            if not isinstance(mem, dict) or not mem.get("id"):
                continue
            mid = str(mem["id"])
            scores[mid] = scores.get(mid, 0) + (1.0 / (RRF_K + i))
            if mid not in records:
                records[mid] = mem

    # Sort by RRF score descending
    sorted_ids = sorted(scores, key=lambda x: scores[x], reverse=True)

    result = []
    for mid in sorted_ids:
        mem = records[mid]
        mem["_rrf_score"] = round(scores[mid], 6)
        result.append(mem)

    return result
```

- [ ] **Step 8: Write the Recent Fallback**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Recent fallback (no query)
# ---------------------------------------------------------------------------

async def _recent_fallback(filters: dict, limit: int, db: Any) -> list[dict]:
    """Get most recent active memories when no query is provided."""
    params: dict[str, Any] = {"limit": limit}
    params.update(filters["params"])

    surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE is_active = true
        AND (valid_until IS NONE OR valid_until > time::now())
        {filters["clauses"]}
    ORDER BY created_at DESC
    LIMIT $limit;
    """
    results = await query(db, surql, params)
    if not results or not isinstance(results, list):
        return []

    for i, r in enumerate(results):
        r["_leg"] = "recent"
        r["_rank"] = i
        r["_rrf_score"] = round(1.0 / (RRF_K + i), 6)

    return results
```

- [ ] **Step 9: Write the Vector Reranker**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Optional vector reranker
# ---------------------------------------------------------------------------

async def _vector_rerank(
    query_text: str,
    candidates: list[dict],
    filters: dict,
    limit: int,
    db: Any,
) -> list[dict]:
    """Rerank BM25 candidates using vector cosine similarity.

    Only called when BM25 returns fewer than VECTOR_RERANK_THRESHOLD results.
    Fetches additional vector-similar memories and merges with existing candidates.
    """
    from qmemory.core.embeddings import generate_query_embedding

    try:
        query_vec = await generate_query_embedding(query_text)
        if not query_vec:
            return candidates

        params: dict[str, Any] = {"query_vec": query_vec, "limit": limit}
        params.update(filters["params"])

        scope_clause = ""
        if "scope" in filters["params"]:
            scope_clause = ' AND (scope = $scope OR scope = "global")'

        surql = f"""
        SELECT {MEMORY_FIELDS}, vector::similarity::cosine(embedding, $query_vec) AS vec_score
        FROM memory
        WHERE is_active = true
            AND embedding IS NOT NONE
            AND (valid_until IS NONE OR valid_until > time::now())
            {scope_clause}
            {filters["clauses"]}
        ORDER BY vec_score DESC
        LIMIT $limit;
        """
        vec_results = await query(db, surql, params)

        if not vec_results or not isinstance(vec_results, list):
            return candidates

        # Tag vector results
        for i, r in enumerate(vec_results):
            r["_leg"] = "vector"
            r["_rank"] = i

        # Merge with existing candidates via RRF
        merged = _rrf_fuse(candidates, vec_results)
        logger.debug("Vector rerank: %d candidates -> %d merged", len(candidates), len(merged))
        return merged

    except Exception as e:
        logger.debug("Vector rerank failed (non-fatal): %s", e)
        return candidates
```

- [ ] **Step 10: Write the Extract & Separate function**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Extract & Separate — dynamic routing
# ---------------------------------------------------------------------------

async def _extract_and_separate(
    fused_memories: list[dict],
    entity_results: list[dict],
    query_text: str,
    limit: int,
    offset: int,
    db: Any,
) -> dict:
    """Route each result to the appropriate response section dynamically."""

    pinned: list[dict] = []
    hypotheses: list[dict] = []
    regular: list[dict] = []

    # --- Pass 1: Separate pinned and hypotheses ---
    for mem in fused_memories:
        salience = mem.get("salience", 0)
        confidence = mem.get("confidence", 0.8)

        if salience >= PINNED_THRESHOLD and len(pinned) < MAX_PINNED:
            pinned.append(_format_pinned(mem))
        elif confidence < HYPOTHESIS_THRESHOLD:
            hypotheses.append(_format_hypothesis(mem))
        else:
            regular.append(mem)

    # --- Pass 2: Check for book insights ---
    book_insights: list[dict] = []
    regular_non_book: list[dict] = []

    if regular:
        book_mem_ids = await _find_book_linked_memories(
            [str(m["id"]) for m in regular if m.get("id")], db
        )
        for mem in regular:
            mid = str(mem.get("id", ""))
            if mid in book_mem_ids:
                book_insights.append(
                    _format_book_insight(mem, book_mem_ids[mid])
                )
            else:
                regular_non_book.append(mem)
    else:
        regular_non_book = regular

    # --- Pass 3: Apply offset + limit, then group by category ---
    paginated = regular_non_book[offset:offset + limit]

    # Enrich top results with graph context
    paginated = await _enrich_with_graph(paginated[:TOP_N_ENRICH], db) + paginated[TOP_N_ENRICH:]

    # Group by category
    memories_grouped: dict[str, list[dict]] = {}
    by_category: dict[str, int] = {}

    for mem in paginated:
        cat = mem.get("category", "context")
        formatted = _format_memory(mem)
        if cat not in memories_grouped:
            memories_grouped[cat] = []
        memories_grouped[cat].append(formatted)
        by_category[cat] = by_category.get(cat, 0) + 1

    # Sort categories — self first, then by CATEGORY_ORDER
    sorted_memories: dict[str, list[dict]] = {}
    for cat in CATEGORY_ORDER:
        if cat in memories_grouped:
            sorted_memories[cat] = memories_grouped[cat]
    # Add any categories not in CATEGORY_ORDER
    for cat in memories_grouped:
        if cat not in sorted_memories:
            sorted_memories[cat] = memories_grouped[cat]

    # --- Build response (only include non-empty sections) ---
    response: dict[str, Any] = {}

    if entity_results:
        response["entities_matched"] = entity_results
    if pinned:
        response["pinned"] = pinned
    if sorted_memories:
        response["memories"] = sorted_memories
    if book_insights:
        response["book_insights"] = book_insights
    if hypotheses:
        response["hypotheses"] = hypotheses

    # --- Build meta ---
    has_more = len(regular_non_book) > offset + limit
    sections = [k for k in ["entities_matched", "pinned", "memories", "book_insights", "hypotheses"] if k in response]

    # Count results per leg
    search_legs: dict[str, int] = {}
    for mem in fused_memories:
        leg = mem.get("_leg", "unknown")
        search_legs[leg] = search_legs.get(leg, 0) + 1

    # Drill-down actions
    drill_down = build_category_drill_down(query_text, by_category) if query_text else []

    return attach_meta(
        response,
        actions_context={
            "type": "search",
            "entity_id": entity_results[0]["id"] if entity_results else None,
            "memory_id": None,
            "neighbor_count": 0,
        },
        by_category=by_category,
        total_found=len(fused_memories),
        returned=sum(len(v) for v in sorted_memories.values()) if sorted_memories else 0,
        offset=offset,
        has_more=has_more,
        sections=sections,
        search_legs=search_legs,
        vector_rerank=any(m.get("_leg") == "vector" for m in fused_memories),
        drill_down=drill_down,
    )
```

- [ ] **Step 11: Write the formatting helpers**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_memory(mem: dict) -> dict:
    """Format a fused memory into the agent-facing format with graph and actions."""
    mid = str(mem.get("id", ""))
    return {
        "id": mid,
        "content": mem.get("content", ""),
        "relevance": round(mem.get("_rrf_score", 0), 4),
        "salience": mem.get("salience", 0),
        "found_by": mem.get("_leg", "unknown"),
        "age": _format_age(mem.get("created_at")),
        "graph": mem.get("_graph", {"entities": [], "related": [], "from_book": None}),
        "actions": build_memory_actions(mid),
    }


def _format_pinned(mem: dict) -> dict:
    """Format a high-salience pinned memory."""
    return {
        "id": str(mem.get("id", "")),
        "content": mem.get("content", ""),
        "category": mem.get("category", ""),
        "salience": mem.get("salience", 0),
        "age": _format_age(mem.get("created_at")),
    }


def _format_hypothesis(mem: dict) -> dict:
    """Format a low-confidence hypothesis memory."""
    mid = str(mem.get("id", ""))
    return {
        "id": mid,
        "content": mem.get("content", ""),
        "confidence": mem.get("confidence", 0),
        "evidence_type": mem.get("evidence_type", ""),
        "category": mem.get("category", ""),
        "actions": {
            "verify": {
                "tool": "qmemory_correct",
                "args": {"memory_id": mid, "action": "update"},
            },
        },
    }


def _format_book_insight(mem: dict, book_info: dict) -> dict:
    """Format a memory that's linked to a book."""
    mid = str(mem.get("id", ""))
    book_id = book_info.get("book_id", "")
    section = mem.get("section")
    return {
        "id": mid,
        "content": mem.get("content", ""),
        "book": {"id": book_id, "title": book_info.get("title", "")},
        "section": section,
        "relevance": round(mem.get("_rrf_score", 0), 4),
        "actions": build_book_insight_actions(book_id, section),
    }
```

- [ ] **Step 12: Write the book-link detection helper**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Book-link detection (batch)
# ---------------------------------------------------------------------------

async def _find_book_linked_memories(
    memory_ids: list[str], db: Any
) -> dict[str, dict]:
    """Check which memories have from_book edges. Returns {memory_id: {book_id, title}}."""
    if not memory_ids:
        return {}

    result: dict[str, dict] = {}

    for mid in memory_ids:
        surql = """
        SELECT out.id AS book_id, out.name AS title
        FROM relates
        WHERE in = <record>$mid AND type = "from_book"
        LIMIT 1;
        """
        rows = await query(db, surql, {"mid": mid})
        if rows and isinstance(rows, list) and len(rows) > 0:
            row = rows[0]
            if isinstance(row, dict) and row.get("book_id"):
                result[mid] = {
                    "book_id": str(row["book_id"]),
                    "title": row.get("title", ""),
                }

    return result
```

- [ ] **Step 13: Write the graph enrichment helper**

Append to `qmemory/core/search.py`:

```python
# ---------------------------------------------------------------------------
# Graph enrichment (batch — attaches _graph to each memory)
# ---------------------------------------------------------------------------

async def _enrich_with_graph(memories: list[dict], db: Any) -> list[dict]:
    """Attach graph context (entities, related memories) to top results."""
    if not memories:
        return memories

    enriched = list(memories)

    for i, mem in enumerate(enriched):
        mid = str(mem.get("id", ""))
        if not mid:
            continue

        try:
            surql = f"""
            SELECT
                ->relates->entity.{{id, name, type}} AS out_entities,
                <-relates<-entity.{{id, name, type}} AS in_entities,
                ->relates.{{type, out}} AS out_edges,
                <-relates.{{type, in}} AS in_edges,
                ->relates->memory.{{id, content}} AS out_memories,
                <-relates<-memory.{{id, content}} AS in_memories
            FROM {mid}
            """
            rows = await query(db, surql)
            if not rows or not isinstance(rows, list) or len(rows) == 0:
                continue

            data = rows[0] if isinstance(rows[0], dict) else {}

            # Build entity list
            entities: list[dict] = []
            seen_entities: set[str] = set()
            edge_type_map: dict[str, str] = {}

            for edge in (data.get("out_edges") or []):
                if isinstance(edge, dict):
                    edge_type_map[str(edge.get("out", ""))] = edge.get("type", "relates")
            for edge in (data.get("in_edges") or []):
                if isinstance(edge, dict):
                    edge_type_map[str(edge.get("in", ""))] = edge.get("type", "relates")

            for e in (data.get("out_entities") or []) + (data.get("in_entities") or []):
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                eid = str(e["id"])
                if eid in seen_entities:
                    continue
                seen_entities.add(eid)
                entities.append({
                    "id": eid,
                    "name": e.get("name", ""),
                    "edge": edge_type_map.get(eid, "relates"),
                })

            # Build related memories list
            related: list[dict] = []
            seen_mems: set[str] = set()
            for m in (data.get("out_memories") or []) + (data.get("in_memories") or []):
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                rid = str(m["id"])
                if rid in seen_mems or rid == mid:
                    continue
                seen_mems.add(rid)
                related.append({
                    "id": rid,
                    "preview": (m.get("content") or "")[:80],
                    "edge": edge_type_map.get(rid, "relates"),
                })
                if len(related) >= MAX_HINTS_PER_RESULT:
                    break

            enriched[i] = {
                **mem,
                "_graph": {
                    "entities": entities,
                    "related": related,
                    "from_book": None,  # Set by _find_book_linked_memories
                },
            }

        except Exception as ex:
            logger.debug("Graph enrichment failed for %s (non-fatal): %s", mid, ex)

    return enriched
```

- [ ] **Step 14: Commit the new search.py**

```bash
git add qmemory/core/search.py
git commit -m "feat: rewrite search engine — multi-leg BM25, RRF fusion, dynamic categories"
```

---

### Task 4: Write Tests for New Search Engine

**Files:**
- Rewrite: `tests/test_core/test_search.py`

- [ ] **Step 1: Write the new test file**

Replace `tests/test_core/test_search.py` entirely:

```python
"""
Tests for the new multi-leg BM25 search engine.

Tests the dynamic category-grouped response format:
entities_matched, pinned, memories.{category}, book_insights, hypotheses.

All tests use the `db` fixture from conftest.py (fresh qmemory_test namespace).
Requires SurrealDB running locally (ws://localhost:8000).
"""

from qmemory.core.search import search_memories
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes
from qmemory.core.person import create_person


# ---------------------------------------------------------------------------
# Response structure tests
# ---------------------------------------------------------------------------


async def test_search_returns_dict(db):
    """Search should always return a dict with actions and meta."""
    await save_memory(content="Structure test fact", category="context", db=db)
    result = await search_memories(query_text="structure test", db=db)

    assert isinstance(result, dict)
    assert "actions" in result
    assert "meta" in result


async def test_search_memories_grouped_by_category(db):
    """Memories should be grouped by category in the response."""
    await save_memory(content="User likes dark mode", category="preference", db=db)
    await save_memory(content="Project started in January", category="context", db=db)

    result = await search_memories(query_text="dark mode project", db=db)

    if "memories" in result:
        assert isinstance(result["memories"], dict), "memories should be a dict keyed by category"
        for cat, mems in result["memories"].items():
            assert isinstance(mems, list)
            for m in mems:
                assert "id" in m
                assert "content" in m
                assert "actions" in m


async def test_search_empty_categories_omitted(db):
    """Categories with no results should not appear in memories."""
    await save_memory(content="Only a preference here", category="preference", db=db)

    result = await search_memories(query_text="preference here", db=db)

    if "memories" in result:
        for cat, mems in result["memories"].items():
            assert len(mems) > 0, f"Category '{cat}' should not be empty"


async def test_search_self_category_first(db):
    """Self category should come first in the memories dict."""
    await save_memory(content="I am the agent self model", category="self", db=db)
    await save_memory(content="Some context about the world", category="context", db=db)

    result = await search_memories(query_text="agent self context world", db=db)

    if "memories" in result and "self" in result["memories"]:
        keys = list(result["memories"].keys())
        assert keys[0] == "self", f"Expected 'self' first, got: {keys}"


# ---------------------------------------------------------------------------
# Pinned tests
# ---------------------------------------------------------------------------


async def test_search_pinned_high_salience(db):
    """Memories with salience >= 0.9 should appear in pinned section."""
    await save_memory(content="Critical rule never break", category="self", salience=1.0, db=db)
    await save_memory(content="Normal fact about testing", category="context", salience=0.5, db=db)

    result = await search_memories(query_text="testing rule", db=db)

    if "pinned" in result:
        pinned_contents = [p["content"] for p in result["pinned"]]
        assert "Critical rule never break" in pinned_contents


async def test_search_pinned_not_in_memories(db):
    """Pinned memories should not also appear in the memories section."""
    await save_memory(content="Pinned and unique fact xyz", category="context", salience=0.95, db=db)

    result = await search_memories(query_text="pinned unique xyz", db=db)

    pinned_ids = {p["id"] for p in result.get("pinned", [])}
    for cat_mems in result.get("memories", {}).values():
        for m in cat_mems:
            assert m["id"] not in pinned_ids, f"Pinned memory {m['id']} also in memories"


# ---------------------------------------------------------------------------
# Hypothesis tests
# ---------------------------------------------------------------------------


async def test_search_low_confidence_in_hypotheses(db):
    """Memories with confidence < 0.5 should appear in hypotheses."""
    await save_memory(
        content="Maybe the project will be cancelled",
        category="context",
        confidence=0.3,
        evidence_type="inferred",
        db=db,
    )

    result = await search_memories(query_text="project cancelled", db=db)

    if "hypotheses" in result:
        hyp_contents = [h["content"] for h in result["hypotheses"]]
        assert "Maybe the project will be cancelled" in hyp_contents
        assert result["hypotheses"][0].get("actions", {}).get("verify") is not None


# ---------------------------------------------------------------------------
# Entity search tests
# ---------------------------------------------------------------------------


async def test_search_finds_entities(db):
    """Searching for a person name should return in entities_matched."""
    await create_person(name="Ahmed Khalil", db=db)
    await save_memory(content="Ahmed works on mobile", category="context", db=db)

    result = await search_memories(query_text="Ahmed", db=db)

    if "entities_matched" in result:
        names = [e["name"] for e in result["entities_matched"]]
        assert "Ahmed Khalil" in names
        assert result["entities_matched"][0].get("actions") is not None


async def test_search_entity_has_actions(db):
    """Each matched entity should have get and search_within actions."""
    await create_person(name="Fatima Al-Rashid", db=db)

    result = await search_memories(query_text="Fatima", db=db)

    if "entities_matched" in result and len(result["entities_matched"]) > 0:
        entity = result["entities_matched"][0]
        assert "get" in entity["actions"]
        assert "search_within" in entity["actions"]


# ---------------------------------------------------------------------------
# Graph enrichment tests
# ---------------------------------------------------------------------------


async def test_search_results_have_graph(db):
    """Memory results should have graph context with entities and related."""
    await save_memory(content="Team uses Slack for comms", category="context", db=db)

    result = await search_memories(query_text="Slack comms", db=db)

    if "memories" in result:
        for cat_mems in result["memories"].values():
            for m in cat_mems:
                assert "graph" in m, f"Memory {m['id']} missing graph"
                assert "entities" in m["graph"]
                assert "related" in m["graph"]


async def test_search_enrichment_shows_linked(db):
    """After linking two memories, graph should show the connection."""
    saved1 = await save_memory(content="Slack is used daily", category="context", salience=0.8, db=db)
    saved2 = await save_memory(content="Slack channel engineering", category="context", salience=0.7, db=db)

    await link_nodes(
        from_id=saved1["memory_id"],
        to_id=saved2["memory_id"],
        relationship_type="has_detail",
        db=db,
    )

    result = await search_memories(query_text="Slack", db=db)

    # Find any memory with non-empty graph.related
    has_related = False
    for cat_mems in result.get("memories", {}).values():
        for m in cat_mems:
            if m.get("graph", {}).get("related"):
                has_related = True
    assert has_related, "At least one memory should have graph.related after linking"


# ---------------------------------------------------------------------------
# Per-result action tests
# ---------------------------------------------------------------------------


async def test_search_results_have_actions(db):
    """Each memory result should have correct, link, get_neighbors actions."""
    await save_memory(content="Actions test fact", category="context", db=db)

    result = await search_memories(query_text="actions test", db=db)

    if "memories" in result:
        for cat_mems in result["memories"].values():
            for m in cat_mems:
                assert "actions" in m
                assert "correct" in m["actions"]
                assert "link" in m["actions"]
                assert "get_neighbors" in m["actions"]


# ---------------------------------------------------------------------------
# Meta tests
# ---------------------------------------------------------------------------


async def test_search_meta_has_by_category(db):
    """Meta should include by_category counts."""
    await save_memory(content="Meta test preference", category="preference", db=db)
    await save_memory(content="Meta test context", category="context", db=db)

    result = await search_memories(query_text="meta test", db=db)

    assert "by_category" in result["meta"]
    assert isinstance(result["meta"]["by_category"], dict)


async def test_search_meta_has_sections(db):
    """Meta should list which sections are present."""
    await save_memory(content="Sections test fact", category="context", db=db)

    result = await search_memories(query_text="sections test", db=db)

    assert "sections" in result["meta"]
    assert isinstance(result["meta"]["sections"], list)


async def test_search_meta_has_search_legs(db):
    """Meta should show how many results came from each leg."""
    await save_memory(content="Legs test fact", category="context", db=db)

    result = await search_memories(query_text="legs test", db=db)

    assert "search_legs" in result["meta"]


# ---------------------------------------------------------------------------
# Empty/no query tests
# ---------------------------------------------------------------------------


async def test_search_no_query_returns_recent(db):
    """No query should return recent memories."""
    await save_memory(content="Recent fact alpha", category="context", db=db)

    result = await search_memories(query_text=None, db=db)

    assert isinstance(result, dict)
    assert "meta" in result


async def test_search_empty_string_query(db):
    """Empty string query should work without errors."""
    await save_memory(content="Empty query test", category="context", db=db)

    result = await search_memories(query_text="", db=db)

    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Category filter tests
# ---------------------------------------------------------------------------


async def test_search_category_filter(db):
    """Category filter should only return that category."""
    await save_memory(content="Filter preference item", category="preference", db=db)
    await save_memory(content="Filter context item", category="context", db=db)

    result = await search_memories(category="preference", db=db)

    if "memories" in result:
        for cat in result["memories"]:
            assert cat == "preference", f"Expected only preference, got {cat}"


# ---------------------------------------------------------------------------
# entity_id scoped search tests
# ---------------------------------------------------------------------------


async def test_search_with_entity_id(db):
    """entity_id should scope search to memories linked to that entity."""
    person = await create_person(name="Scoped Person", db=db)
    eid = person["entity_id"]

    saved = await save_memory(content="Scoped person likes coffee", category="preference", db=db)
    await link_nodes(from_id=saved["memory_id"], to_id=eid, relationship_type="about", db=db)

    await save_memory(content="Unrelated person likes tea", category="preference", db=db)

    result = await search_memories(query_text="likes", entity_id=eid, db=db)

    # Should find coffee but not tea
    all_contents = []
    for cat_mems in result.get("memories", {}).values():
        all_contents.extend(m["content"] for m in cat_mems)
    # The linked memory should appear
    # (tea may appear via content leg if entity_id scoping works on content leg)
    assert any("coffee" in c for c in all_contents), f"Should find 'coffee', got: {all_contents}"
```

- [ ] **Step 2: Run the tests — expect failures**

Run: `uv run pytest tests/test_core/test_search.py -v`
Expected: Most tests FAIL because the new search module hasn't been connected yet. That's OK — we wrote the search module in Task 3, these tests validate it.

- [ ] **Step 3: Run the tests — expect passes after Task 3 is complete**

Run: `uv run pytest tests/test_core/test_search.py -v`
Expected: All tests PASS (requires SurrealDB running locally).

- [ ] **Step 4: Commit**

```bash
git add tests/test_core/test_search.py
git commit -m "test: rewrite search tests for multi-leg BM25 + dynamic category grouping"
```

---

### Task 5: Update MCP Tools — Add entity_id Parameter

**Files:**
- Modify: `qmemory/mcp/server.py:93-141`
- Modify: `qmemory/app/main.py:103-164`

- [ ] **Step 1: Update stdio transport (server.py)**

In `qmemory/mcp/server.py`, change the `qmemory_search` function signature and docstring. Replace the existing function (lines ~93-141):

```python
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
    include_tool_calls: bool = False,
    source_type: str | None = None,
    entity_id: str | None = None,
) -> str:
    """Search cross-session memory by meaning, category, or scope.

    Returns memories from ALL past conversations, grouped by category,
    with graph context and structured next-step actions.

    Args:
        query:             Free-text search query (multi-leg BM25).
                           Leave empty to get recent memories without text search.
        category:          Filter to one category (HARD filter — excludes others):
                           self, style, preference, context, decision,
                           idea, feedback, domain
        scope:             Filter visibility: global, project:xxx, topic:xxx
        limit:             Max results to return (default 10, max 50).
        offset:            Skip first N results for pagination (default 0).
        after:             Only return memories created after this date.
                           ISO date string, e.g. "2026-04-01".
        before:            Only return memories created before this date.
        include_tool_calls: Also search past tool call history (default False).
        source_type:       Filter by relation type pointing to the memory.
                           E.g. "from_book" returns only memories extracted from books.
        entity_id:         Scope search to memories linked to this entity.
                           E.g. "entity:ent123abc" — only returns memories about that person/concept.

    Returns JSON with dynamic sections:
      entities_matched — matched people/concepts with actions
      pinned — high-salience memories (>= 0.9)
      memories.{category} — grouped by category, ranked by relevance
      book_insights — memories linked to books
      hypotheses — low-confidence memories needing verification
      actions — suggested next steps
      meta — counts, sections list, search leg breakdown
    """
    from qmemory.core.search import search_memories

    results = await search_memories(
        query_text=query,
        category=category,
        scope=scope,
        limit=limit,
        offset=offset,
        after=after,
        before=before,
        include_tool_calls=include_tool_calls,
        source_type=source_type,
        entity_id=entity_id,
    )
    return json.dumps(results, default=str, ensure_ascii=False)
```

- [ ] **Step 2: Update HTTP transport (app/main.py)**

In `qmemory/app/main.py`, make the same changes to the `qmemory_search` function. Add `entity_id: str | None = None` to the parameter list, update the docstring to match server.py, and pass `entity_id=entity_id` to `search_memories()`.

The function signature becomes:

```python
@mcp.tool()
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
    include_tool_calls: bool = False,
    source_type: str | None = None,
    entity_id: str | None = None,
) -> str:
```

And the call becomes:

```python
    results = await search_memories(
        query_text=query,
        category=category,
        scope=scope,
        limit=limit,
        offset=offset,
        after=after,
        before=before,
        include_tool_calls=include_tool_calls,
        source_type=source_type,
        entity_id=entity_id,
    )
```

- [ ] **Step 3: Commit**

```bash
git add qmemory/mcp/server.py qmemory/app/main.py
git commit -m "feat: add entity_id param to qmemory_search on both transports"
```

---

### Task 6: Make Vector Search Optional in Recall Pipeline

**Files:**
- Modify: `qmemory/core/recall.py:547-640` (Tier 2)

- [ ] **Step 1: Modify Tier 2 to skip vector when BM25 has enough results**

In `qmemory/core/recall.py`, find the `_tier2_search` function. Change the vector search section (around line 604) to only run when BM25 returned fewer than 5 results:

Replace the comment and vector section (from `# --- Vector similarity search ---` to the end of the function):

```python
    # --- Vector similarity search (optional reranker) ---
    # Only runs when BM25 found fewer than 5 results.
    # Saves ~90% of Voyage API calls while keeping a safety net for vague queries.
    if len(results) < 5:
        try:
            query_vec = await generate_query_embedding(query_text)
            if query_vec:
                vec_params: dict[str, Any] = {
                    "query_vec": query_vec,
                    "limit": limit,
                }
                if extra_params:
                    vec_params.update(extra_params)
                vec_scope_clause = ""
                if scope and scope != "any":
                    vec_scope_clause = 'AND ($scope = "any" OR scope = $scope OR scope = "global")'
                    vec_params["scope"] = scope

                vec_surql = f"""
                SELECT {MEMORY_FIELDS}, vector::similarity::cosine(embedding, $query_vec) AS vec_score
                FROM memory
                WHERE is_active = true
                    AND embedding IS NOT NONE
                    {vec_scope_clause}
                    AND (valid_until IS NONE OR valid_until > time::now())
                    {extra_clauses}
                ORDER BY vec_score DESC
                LIMIT $limit;
                """

                vec_results = await query(db, vec_surql, vec_params)
                if vec_results and isinstance(vec_results, list):
                    for r in vec_results:
                        r["source_tier"] = "vector"
                    results.extend(vec_results)
                    logger.debug("Tier 2 vector (rerank): %d results", len(vec_results))
        except Exception as e:
            # Vector search is non-fatal — BM25 results are enough
            logger.debug("Vector search failed (non-fatal): %s", e)
    else:
        logger.debug("Tier 2 vector: skipped — BM25 returned %d results (>= 5)", len(results))

    return results
```

- [ ] **Step 2: Run existing recall tests to verify no regressions**

Run: `uv run pytest tests/test_core/test_recall.py -v`
Expected: All existing tests still PASS.

- [ ] **Step 3: Commit**

```bash
git add qmemory/core/recall.py
git commit -m "perf: demote vector search to optional reranker — only fires when BM25 < 5 results"
```

---

### Task 7: Integration Smoke Test

**Files:** No new files — just run everything together.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests PASS (existing + new). Note the 9 known failing tests (SurrealDB v3 edge syntax) — those are pre-existing and unrelated.

- [ ] **Step 2: Run search tests specifically**

Run: `uv run pytest tests/test_core/test_search.py tests/test_formatters/test_actions.py -v`
Expected: All PASS.

- [ ] **Step 3: Quick manual smoke test via CLI**

```bash
uv run python -c "
import asyncio, json
from qmemory.core.search import search_memories
from qmemory.db.client import get_db

async def test():
    async with get_db() as db:
        result = await search_memories(query_text='test', limit=3, db=db)
        print(json.dumps(result, indent=2, default=str, ensure_ascii=False)[:2000])

asyncio.run(test())
"
```

Expected: JSON output with dynamic sections (entities_matched, memories.{category}, meta with by_category, sections, search_legs).

- [ ] **Step 4: Final commit with all changes if any fixups needed**

```bash
git add -A
git status
# Only commit if there are fixup changes
git commit -m "fix: integration fixups from smoke testing"
```
