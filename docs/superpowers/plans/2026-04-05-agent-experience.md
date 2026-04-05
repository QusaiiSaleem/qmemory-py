# Agent Experience Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix qmemory's access layer so AI agents can find, navigate, and build on the knowledge graph — not just dump flat lists.

**Architecture:** Incremental changes to existing 8 tools + 1 new tool (`qmemory_get`). Every response gets a shared `actions` + `meta` footer. Search ranking switches from salience-only to composite score. Two new shared helpers (`formatters/actions.py`, `formatters/response.py`) keep the response logic DRY across all tools.

**Tech Stack:** Python 3.11+, SurrealDB v3, FastMCP, pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-05-agent-experience-design.md`

---

## File Map

```
NEW FILES:
  qmemory/core/get.py              — fetch by ID + neighbor traversal (new tool logic)
  qmemory/formatters/actions.py    — build_actions() shared helper
  qmemory/formatters/response.py   — attach_meta() shared helper
  tests/test_core/test_get.py      — tests for qmemory_get

MODIFIED FILES:
  qmemory/core/recall.py           — composite ranking, hard filters, pinned, offset, after/before
  qmemory/core/search.py           — new response format, entity search, structured actions
  qmemory/core/save.py             — add meta (indexed, embedding_generated) to response
  qmemory/core/correct.py          — add changes detail + meta to response
  qmemory/core/link.py             — add both-ends preview + meta to response
  qmemory/core/person.py           — add memory_count + meta to response
  qmemory/core/books.py            — replace _nudge with actions + meta
  qmemory/mcp/server.py            — add qmemory_get, update search params + docstrings
  qmemory/app/main.py              — mirror all server.py changes (both transports in sync)
  tests/test_core/test_recall.py   — update for new ranking + filters
  tests/test_core/test_search.py   — update for new response format
```

---

## Task 1: Shared Response Helpers

**Files:**
- Create: `qmemory/formatters/actions.py`
- Create: `qmemory/formatters/response.py`
- Modify: `qmemory/formatters/__init__.py`

These two helpers are used by every tool, so we build them first.

- [ ] **Step 1: Create `formatters/actions.py`**

```python
"""
Shared action builder — generates structured next-step suggestions for agents.

Every qmemory tool response includes an "actions" list. Each action is a
ready-to-use tool call: {"tool": "...", "args": {...}, "reason": "..."}.
The agent can copy-paste these directly instead of parsing a text nudge.
"""
from __future__ import annotations


def build_actions(context: dict) -> list[dict]:
    """Build suggested next-step tool calls based on what just happened.

    Args:
        context: A dict describing the operation result. Keys used:
            - "type": operation type ("search", "save", "correct", "link", "person", "get", "books", "bootstrap")
            - "memory_id": ID of the memory just created/modified
            - "entity_id": ID of an entity involved
            - "has_neighbors": whether the result has graph connections
            - "neighbor_count": how many connections exist
            - "dedup_similar_id": ID of a similar memory found during dedup
            - "from_id" / "to_id": link endpoints
            - "book_id": book entity for browsing
            - "total_memories": count for bootstrap
            - "ids": list of IDs for batch get

    Returns:
        List of action dicts: [{"tool": "...", "args": {...}, "reason": "..."}]
    """
    actions: list[dict] = []
    op_type = context.get("type", "")

    if op_type == "search":
        # Suggest exploring entities that matched
        if context.get("entity_id"):
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [context["entity_id"]], "include_neighbors": True},
                "reason": f"Entity matched your query — explore its memory graph",
            })
        # Suggest fetching neighbors for connected results
        if context.get("memory_id") and context.get("neighbor_count", 0) > 0:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [context["memory_id"]], "include_neighbors": True},
                "reason": f"{context['neighbor_count']} connection(s) to explore",
            })

    elif op_type == "save":
        mid = context.get("memory_id", "")
        if context.get("dedup_similar_id"):
            actions.append({
                "tool": "qmemory_link",
                "args": {"from_id": mid, "to_id": context["dedup_similar_id"], "relationship_type": "related_to"},
                "reason": "Similar memory found during dedup — consider linking",
            })
        elif mid:
            actions.append({
                "tool": "qmemory_search",
                "args": {"query": context.get("content_preview", "")[:50]},
                "reason": "Find related memories to link with this one",
            })

    elif op_type == "correct":
        new_id = context.get("new_memory_id") or context.get("memory_id", "")
        if new_id:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [new_id], "include_neighbors": True},
                "reason": "Verify corrected memory and its connections",
            })

    elif op_type == "link":
        for endpoint in ["from_id", "to_id"]:
            nid = context.get(endpoint)
            count = context.get(f"edge_count_{endpoint.split('_')[0]}", 0)
            if nid and count > 0:
                actions.append({
                    "tool": "qmemory_get",
                    "args": {"ids": [nid], "include_neighbors": True},
                    "reason": f"Now has {count} connection(s) — explore",
                })

    elif op_type == "person":
        eid = context.get("entity_id", "")
        mem_count = context.get("memory_count", 0)
        if eid:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [eid], "include_neighbors": True},
                "reason": f"{mem_count} memories linked to this person" if mem_count else "Explore person graph",
            })

    elif op_type == "get":
        # Suggest getting neighbors if not already included
        if context.get("ids") and not context.get("include_neighbors"):
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": context["ids"], "include_neighbors": True},
                "reason": "Fetch graph neighbors for these nodes",
            })

    elif op_type == "books":
        if context.get("book_id") and not context.get("section"):
            actions.append({
                "tool": "qmemory_books",
                "args": {"book_id": context["book_id"]},
                "reason": "Browse this book's sections",
            })
        elif context.get("book_id") and context.get("section"):
            actions.append({
                "tool": "qmemory_search",
                "args": {"source_type": "from_book", "query": context.get("section", "")},
                "reason": "Find insights from this section",
            })

    elif op_type == "bootstrap":
        total = context.get("total_memories", 0)
        if total > 20:
            actions.append({
                "tool": "qmemory_search",
                "args": {"query": ""},
                "reason": f"{total} memories available — search for specifics",
            })

    return actions
```

- [ ] **Step 2: Create `formatters/response.py`**

```python
"""
Shared response helper — ensures every response has actions + meta.

The universal contract: every qmemory tool response includes:
  - "actions": list of structured next-step tool calls (always present)
  - "meta": dict with operation metadata (always present)
"""
from __future__ import annotations

import time
from typing import Any

from qmemory.formatters.actions import build_actions


def attach_meta(response: dict, *, actions_context: dict | None = None, **meta_fields: Any) -> dict:
    """Ensure a response dict has 'actions' and 'meta' keys.

    Args:
        response:        The tool's response dict (modified in place and returned).
        actions_context: If provided, passed to build_actions() to generate suggestions.
                         If None, actions will be an empty list.
        **meta_fields:   Key-value pairs added to the meta dict.

    Returns:
        The same response dict with 'actions' and 'meta' guaranteed present.
    """
    # Build actions from context, or use empty list
    if "actions" not in response:
        if actions_context:
            response["actions"] = build_actions(actions_context)
        else:
            response["actions"] = []

    # Build meta from kwargs, preserving any existing meta
    if "meta" not in response:
        response["meta"] = {}
    response["meta"].update(meta_fields)

    return response
```

- [ ] **Step 3: Export from `__init__.py`**

Add to `qmemory/formatters/__init__.py`:

```python
from qmemory.formatters.actions import build_actions
from qmemory.formatters.response import attach_meta
```

- [ ] **Step 4: Commit**

```bash
git add qmemory/formatters/actions.py qmemory/formatters/response.py qmemory/formatters/__init__.py
git commit -m "feat: add shared response helpers — build_actions() + attach_meta()"
```

---

## Task 2: Composite Ranking + Source Tier Tagging

**Files:**
- Modify: `qmemory/core/recall.py`
- Modify: `tests/test_core/test_recall.py`

This is the P0 fix — search results ranked by relevance, not just salience.

- [ ] **Step 1: Write the failing test for composite ranking**

Add to `tests/test_core/test_recall.py`:

```python
async def test_composite_ranking_relevance_beats_salience(db):
    """
    A memory with high relevance (found via BM25) but low salience
    should rank ABOVE a memory with high salience but no text match,
    when a query is provided.
    """
    # High salience, but irrelevant to the query
    await save_memory(
        content="Always use emoji in responses",
        category="style",
        salience=1.0,
        db=db,
    )

    # Low salience, but directly matches the query
    await save_memory(
        content="The project budget for Q3 is 200K",
        category="context",
        salience=0.3,
        db=db,
    )

    results = await recall(query_text="project budget Q3", limit=10, db=db)

    # The budget memory should rank first because it matches the query
    assert len(results) >= 2
    budget_idx = next(i for i, r in enumerate(results) if "budget" in r["content"])
    emoji_idx = next(i for i, r in enumerate(results) if "emoji" in r["content"])
    assert budget_idx < emoji_idx, "Relevant result should rank above high-salience irrelevant result"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_core/test_recall.py::test_composite_ranking_relevance_beats_salience -v
```

Expected: FAIL — currently both are sorted by salience DESC, so emoji (1.0) ranks above budget (0.3).

- [ ] **Step 3: Write the failing test for source_tier tagging**

Add to `tests/test_core/test_recall.py`:

```python
async def test_recall_results_have_source_tier(db):
    """Every result from recall() should have a 'source_tier' field."""
    await save_memory(
        content="Testing source tier tagging",
        category="context",
        salience=0.5,
        db=db,
    )

    results = await recall(query_text="source tier tagging", limit=5, db=db)
    assert len(results) >= 1

    for r in results:
        assert "source_tier" in r, f"Result missing source_tier: {r.get('id')}"
        assert r["source_tier"] in ("bm25", "vector", "graph", "recent", "source_type"), \
            f"Unexpected source_tier value: {r['source_tier']}"
```

- [ ] **Step 4: Run test to verify it fails**

```bash
uv run pytest tests/test_core/test_recall.py::test_recall_results_have_source_tier -v
```

Expected: FAIL — `source_tier` key doesn't exist yet.

- [ ] **Step 5: Implement source_tier tagging in recall.py**

In `qmemory/core/recall.py`, modify each tier function to tag results with their source:

In `_tier0_source_type()`, after `result = await query(...)` (around line 380):

```python
    result = await query(db, fetch_surql, params)
    if result and isinstance(result, list):
        for r in result:
            r["source_tier"] = "source_type"
        return result
    return []
```

In `_tier1_graph_linked()`, after building the memories list (around line 480):

```python
        return memories
```
Change to:
```python
        for m in memories:
            m["source_tier"] = "graph"
        return memories
```

In `_tier2_search()`, after BM25 results (around line 532):

```python
    if bm25_results and isinstance(bm25_results, list):
        for r in bm25_results:
            r["source_tier"] = "bm25"
        results.extend(bm25_results)
```

And after vector results (around line 563):

```python
            if vec_results and isinstance(vec_results, list):
                for r in vec_results:
                    r["source_tier"] = "vector"
                results.extend(vec_results)
```

In `_tier4_recent_fallback()`, after results (around line 652):

```python
    result = await query(db, surql, params)
    if result and isinstance(result, list):
        for r in result:
            r["source_tier"] = "recent"
        return result
    return []
```

In `_tier3_category_filter()`, after results (around line 613):

```python
    result = await query(db, surql, params)
    if result and isinstance(result, list):
        for r in result:
            if "source_tier" not in r:
                r["source_tier"] = "recent"
        return result
    return []
```

- [ ] **Step 6: Implement composite scoring in recall.py**

In `qmemory/core/recall.py`, add the scoring function after the constants section (around line 77):

```python
def _compute_composite_score(memory: dict, has_query: bool) -> float:
    """Compute a composite ranking score combining relevance, salience, and recency.

    When a query is present: relevance dominates (0.6 weight).
    When no query (browsing): salience dominates (0.7 weight).
    """
    salience = memory.get("salience", 0.5)
    source_tier = memory.get("source_tier", "recent")

    # Relevance based on source tier
    tier_relevance = {
        "vector": memory.get("vec_score", 0.7),  # actual cosine similarity
        "bm25": 1.0,       # matched text — high relevance
        "graph": 0.85,     # found via graph — good signal
        "source_type": 0.8,  # found via source_type filter
        "recent": 0.3,     # fallback — low confidence
    }
    relevance = tier_relevance.get(source_tier, 0.3)

    # Recency bonus — small boost for recent memories
    recency_bonus = 0.0
    created_at = memory.get("created_at")
    if created_at:
        from datetime import datetime, timezone
        try:
            if isinstance(created_at, datetime):
                dt = created_at
            else:
                dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
            recency_bonus = max(0.0, 0.1 - (age_days * 0.002))
        except Exception:
            pass

    if has_query:
        # Query mode: relevance dominates
        return (0.6 * relevance) + (0.3 * salience) + recency_bonus
    else:
        # Browse mode: salience dominates
        return (0.7 * salience) + recency_bonus + (0.3 * relevance)
```

- [ ] **Step 7: Replace salience sort with composite sort**

In `recall()` function (around line 224), change:

```python
    # --- Sort by salience DESC (most important first) ---
    deduped.sort(key=lambda m: m.get("salience", 0), reverse=True)
```

To:

```python
    # --- Sort by composite score DESC (relevance + salience + recency) ---
    has_query = query_text is not None and len(query_text.strip()) > 0
    for m in deduped:
        m["_score"] = _compute_composite_score(m, has_query)
    deduped.sort(key=lambda m: m["_score"], reverse=True)
```

- [ ] **Step 8: Run tests**

```bash
uv run pytest tests/test_core/test_recall.py -v
```

Expected: All tests pass, including the two new ones.

- [ ] **Step 9: Commit**

```bash
git add qmemory/core/recall.py tests/test_core/test_recall.py
git commit -m "feat: composite ranking — relevance beats salience when query is present"
```

---

## Task 3: Hard Category Filter + Date Filtering + Offset

**Files:**
- Modify: `qmemory/core/recall.py`
- Modify: `tests/test_core/test_recall.py`

- [ ] **Step 1: Write the failing test for hard category filter**

Add to `tests/test_core/test_recall.py`:

```python
async def test_hard_category_filter(db):
    """When category is set, ONLY that category should appear in results."""
    await save_memory(content="I prefer dark mode", category="preference", salience=0.9, db=db)
    await save_memory(content="The project deadline is March", category="context", salience=0.9, db=db)
    await save_memory(content="Use bullet points", category="style", salience=0.9, db=db)

    results = await recall(categories=["context"], limit=10, db=db)

    categories_found = {r["category"] for r in results}
    assert categories_found == {"context"}, f"Expected only 'context', got: {categories_found}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_core/test_recall.py::test_hard_category_filter -v
```

Expected: FAIL — currently other categories leak through from Tier 2 and Tier 4.

- [ ] **Step 3: Write the failing test for date filtering**

```python
async def test_recall_after_filter(db):
    """The 'after' parameter should exclude memories created before that date."""
    from qmemory.db.client import query as db_query

    # Save a memory, then backdate it
    result = await save_memory(content="Old memory from January", category="context", db=db)
    old_id = result["memory_id"]
    old_suffix = old_id.split(":", 1)[1]
    await db_query(db, f"UPDATE memory:`{old_suffix}` SET created_at = <datetime>'2026-01-01T00:00:00Z'")

    # Save a recent memory
    await save_memory(content="Recent memory from today", category="context", db=db)

    results = await recall(after="2026-04-01", limit=10, db=db)

    contents = [r["content"] for r in results]
    assert "Recent memory from today" in contents
    assert "Old memory from January" not in contents
```

- [ ] **Step 4: Write the failing test for offset pagination**

```python
async def test_recall_offset_pagination(db):
    """Offset should skip the first N results."""
    for i in range(5):
        await save_memory(
            content=f"Memory number {i}",
            category="context",
            salience=0.5 + (i * 0.05),  # slightly different salience
            db=db,
        )

    page1 = await recall(limit=2, offset=0, db=db)
    page2 = await recall(limit=2, offset=2, db=db)

    page1_ids = {str(r["id"]) for r in page1}
    page2_ids = {str(r["id"]) for r in page2}

    # Pages should not overlap
    assert page1_ids.isdisjoint(page2_ids), "Page 1 and Page 2 should have different results"
    assert len(page1) == 2
    assert len(page2) == 2
```

- [ ] **Step 5: Run all three new tests to verify they fail**

```bash
uv run pytest tests/test_core/test_recall.py::test_hard_category_filter tests/test_core/test_recall.py::test_recall_after_filter tests/test_core/test_recall.py::test_recall_offset_pagination -v
```

Expected: All 3 FAIL.

- [ ] **Step 6: Add `after`, `before`, `offset` params + category propagation to `recall()`**

In `qmemory/core/recall.py`, update the `recall()` signature (around line 156):

```python
async def recall(
    query_text: str | None = None,
    scope: str | None = None,
    categories: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    min_salience: float | None = None,
    token_budget: int | None = None,
    owner_id: str | None = None,
    source_type: str | None = None,
    after: str | None = None,
    before: str | None = None,
    db: Any = None,
) -> list[dict]:
```

Pass the new params through to `_run_tiers()`:

```python
    if db is not None:
        collected = await _run_tiers(
            query_text, scope, categories, limit, min_salience,
            target_count, source_type, after, before, db,
        )
    else:
        async with get_db() as conn:
            collected = await _run_tiers(
                query_text, scope, categories, limit, min_salience,
                target_count, source_type, after, before, conn,
            )
```

And apply offset at the end (around line 234), change:

```python
    return deduped[:limit]
```

To:

```python
    # Apply offset for pagination, then trim to limit
    return deduped[offset:offset + limit]
```

- [ ] **Step 7: Update `_run_tiers()` to accept and propagate filters**

Update `_run_tiers()` signature:

```python
async def _run_tiers(
    query_text: str | None,
    scope: str | None,
    categories: list[str] | None,
    limit: int,
    min_salience: float | None,
    target_count: int,
    source_type: str | None,
    after: str | None,
    before: str | None,
    db: Any,
) -> list[dict]:
```

Build shared filter clauses at the top of `_run_tiers()`:

```python
    # Build shared filter clauses that apply to ALL tiers
    shared_clauses = ""
    shared_params: dict[str, Any] = {}

    if categories:
        shared_clauses += " AND category IN $cats"
        shared_params["cats"] = categories

    if after:
        shared_clauses += " AND created_at >= <datetime>$after"
        shared_params["after"] = after

    if before:
        shared_clauses += " AND created_at <= <datetime>$before"
        shared_params["before"] = before
```

Pass `shared_clauses` and `shared_params` to each tier. For each tier function, add `extra_clauses: str = ""` and `extra_params: dict | None = None` parameters. Inside each tier, append `extra_clauses` to the WHERE clause and merge `extra_params` into the query params dict.

- [ ] **Step 8: Update `_tier2_search()` to accept extra filters**

Change the BM25 query in `_tier2_search()` to include extra clauses:

```python
async def _tier2_search(
    query_text: str,
    scope: str | None,
    limit: int,
    db: Any,
    extra_clauses: str = "",
    extra_params: dict | None = None,
) -> list[dict]:
```

In the BM25 query, add `{extra_clauses}` after the existing WHERE conditions, and merge `extra_params` into `params`:

```python
    params: dict[str, Any] = {"query": query_text, "limit": limit}
    if extra_params:
        params.update(extra_params)

    bm25_surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE content @@ $query
        AND is_active = true
        {scope_clause}
        AND (valid_until IS NONE OR valid_until > time::now())
        {extra_clauses}
    ORDER BY salience DESC
    LIMIT $limit;
    """
```

Apply the same pattern to the vector query within the same function.

- [ ] **Step 9: Update `_tier4_recent_fallback()` to accept extra filters**

```python
async def _tier4_recent_fallback(
    scope: str | None,
    db: Any,
    extra_clauses: str = "",
    extra_params: dict | None = None,
) -> list[dict]:
```

Add `{extra_clauses}` to WHERE and merge params:

```python
    params: dict[str, Any] = {"limit": RECENT_FALLBACK_LIMIT}
    if extra_params:
        params.update(extra_params)

    surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE is_active = true
        {scope_clause}
        AND (valid_until IS NONE OR valid_until > time::now())
        {extra_clauses}
    ORDER BY created_at DESC
    LIMIT $limit;
    """
```

- [ ] **Step 10: Update `_tier1_graph_linked()` and `_tier3_category_filter()` similarly**

Same pattern — add `extra_clauses` and `extra_params` parameters, inject into WHERE.

- [ ] **Step 11: Wire the shared clauses in `_run_tiers()`**

Pass `shared_clauses` and `shared_params` to each tier call:

```python
    if query_text and len(query_text) >= MIN_QUERY_LENGTH_FOR_GRAPH:
        tier1 = await _tier1_graph_linked(query_text, scope, db, extra_clauses=shared_clauses, extra_params=shared_params)
        ...

    if query_text and len(collected) < target_count * 1.5:
        tier2 = await _tier2_search(query_text, scope, limit, db, extra_clauses=shared_clauses, extra_params=shared_params)
        ...

    if categories and len(categories) > 0 and len(collected) < target_count * 1.5:
        tier3 = await _tier3_category_filter(categories, scope, min_salience or 0, limit, db, extra_clauses=shared_clauses, extra_params=shared_params)
        ...

    if len(collected) < target_count:
        tier4 = await _tier4_recent_fallback(scope, db, extra_clauses=shared_clauses, extra_params=shared_params)
        ...
```

- [ ] **Step 12: Run tests**

```bash
uv run pytest tests/test_core/test_recall.py -v
```

Expected: All tests pass, including the 3 new filter tests.

- [ ] **Step 13: Commit**

```bash
git add qmemory/core/recall.py tests/test_core/test_recall.py
git commit -m "feat: hard category filter, date filtering (after/before), offset pagination"
```

---

## Task 4: New Tool — `qmemory_get`

**Files:**
- Create: `qmemory/core/get.py`
- Create: `tests/test_core/test_get.py`

- [ ] **Step 1: Write the failing test for basic fetch**

Create `tests/test_core/test_get.py`:

```python
"""Tests for qmemory.core.get — fetch by ID + neighbor traversal."""

from qmemory.core.get import get_memories
from qmemory.core.save import save_memory
from qmemory.core.link import link_nodes


async def test_get_by_id_returns_memory(db):
    """Fetching a saved memory by ID should return its full content."""
    saved = await save_memory(content="Test fact for get_by_id", category="context", db=db)
    mem_id = saved["memory_id"]

    result = await get_memories(ids=[mem_id], db=db)

    assert len(result["memories"]) == 1
    assert result["memories"][0]["content"] == "Test fact for get_by_id"
    assert result["not_found"] == []
    assert "actions" in result
    assert "meta" in result
    assert result["meta"]["found"] == 1
    assert result["meta"]["requested"] == 1


async def test_get_not_found(db):
    """Fetching a non-existent ID should list it in not_found."""
    result = await get_memories(ids=["memory:nonexistent999"], db=db)

    assert result["memories"] == []
    assert "memory:nonexistent999" in result["not_found"]
    assert result["meta"]["found"] == 0
    assert result["meta"]["requested"] == 1


async def test_get_mixed_found_and_not_found(db):
    """Mix of existing and non-existing IDs returns both found and not_found."""
    saved = await save_memory(content="Existing memory", category="context", db=db)
    mem_id = saved["memory_id"]

    result = await get_memories(ids=[mem_id, "memory:fake123"], db=db)

    assert len(result["memories"]) == 1
    assert result["memories"][0]["content"] == "Existing memory"
    assert "memory:fake123" in result["not_found"]
    assert result["meta"]["found"] == 1
    assert result["meta"]["requested"] == 2
```

- [ ] **Step 2: Write the failing test for neighbors**

Add to `tests/test_core/test_get.py`:

```python
async def test_get_with_neighbors(db):
    """include_neighbors=True should return connected nodes."""
    saved_a = await save_memory(content="Memory A", category="context", db=db)
    saved_b = await save_memory(content="Memory B", category="context", db=db)
    id_a = saved_a["memory_id"]
    id_b = saved_b["memory_id"]

    # Link A → B
    await link_nodes(from_id=id_a, to_id=id_b, relationship_type="supports", db=db)

    result = await get_memories(ids=[id_a], include_neighbors=True, db=db)

    assert len(result["memories"]) == 1
    mem = result["memories"][0]
    assert mem["neighbors"]["count"] >= 1
    neighbor_ids = [n["id"] for n in mem["neighbors"]["items"]]
    assert id_b in neighbor_ids


async def test_get_max_ids_limit(db):
    """Requesting more than 20 IDs should raise ValueError."""
    import pytest
    ids = [f"memory:mem{i}" for i in range(25)]
    with pytest.raises(ValueError, match="Maximum 20 IDs"):
        await get_memories(ids=ids, db=db)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run pytest tests/test_core/test_get.py -v
```

Expected: FAIL — `qmemory.core.get` module doesn't exist yet.

- [ ] **Step 4: Implement `core/get.py`**

Create `qmemory/core/get.py`:

```python
"""
Core Get — Fetch memories/entities by ID with optional neighbor traversal.

The fundamental "read by ID" operation that was missing from qmemory.
Supports batch fetch (up to 20 IDs) and graph neighbor expansion.
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.core.recall import MEMORY_FIELDS, _format_age
from qmemory.db.client import get_db, query
from qmemory.formatters.response import attach_meta

logger = logging.getLogger(__name__)

MAX_IDS = 20
MAX_NEIGHBORS_PER_NODE = 10


async def get_memories(
    ids: list[str],
    include_neighbors: bool = False,
    neighbor_depth: int = 1,
    db: Any = None,
) -> dict:
    """Fetch memories or entities by ID, optionally with graph neighbors.

    Args:
        ids:                List of record IDs (e.g. ["memory:mem123", "entity:ent456"]).
        include_neighbors:  If True, fetch connected nodes for each result.
        neighbor_depth:     How deep to traverse (1 or 2). Max 2.
        db:                 Optional SurrealDB connection for test injection.

    Returns:
        dict with: memories, not_found, actions, meta
    """
    if len(ids) > MAX_IDS:
        raise ValueError(f"Maximum {MAX_IDS} IDs per request. Got {len(ids)}.")

    neighbor_depth = min(neighbor_depth, 2)

    if db is not None:
        return await _get_impl(ids, include_neighbors, neighbor_depth, db)
    else:
        async with get_db() as conn:
            return await _get_impl(ids, include_neighbors, neighbor_depth, conn)


async def _get_impl(
    ids: list[str],
    include_neighbors: bool,
    neighbor_depth: int,
    db: Any,
) -> dict:
    """Internal implementation — called with an active DB connection."""

    # Batch fetch all requested IDs in one query
    id_list = ", ".join(ids)
    fetch_surql = f"""
    SELECT {MEMORY_FIELDS}, name, type, aliases, external_source, external_id
    FROM [{id_list}]
    """

    rows = await query(db, fetch_surql)
    rows = rows if rows and isinstance(rows, list) else []

    # Build found/not_found lists
    found_ids = {str(r["id"]) for r in rows if isinstance(r, dict) and r.get("id")}
    not_found = [rid for rid in ids if rid not in found_ids]

    # Format results
    memories = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("id"):
            continue

        mem = {
            "id": str(r["id"]),
            "content": r.get("content", r.get("name", "")),
            "category": r.get("category", r.get("type", "")),
            "salience": r.get("salience", 0),
            "age": _format_age(r.get("created_at")),
            "created_at": str(r.get("created_at", "")),
        }

        # Entity-specific fields
        if r.get("name"):
            mem["name"] = r["name"]
        if r.get("type"):
            mem["type"] = r["type"]
        if r.get("aliases"):
            mem["aliases"] = r["aliases"]

        # Neighbor stub — filled below if requested
        mem["neighbors"] = {"count": 0, "items": []}

        memories.append(mem)

    # Fetch neighbors if requested
    if include_neighbors and memories:
        await _attach_neighbors(memories, neighbor_depth, db)

    response = {
        "memories": memories,
        "not_found": not_found,
    }

    return attach_meta(
        response,
        actions_context={
            "type": "get",
            "ids": ids,
            "include_neighbors": include_neighbors,
        },
        found=len(memories),
        requested=len(ids),
    )


async def _attach_neighbors(
    memories: list[dict],
    depth: int,
    db: Any,
) -> None:
    """Fetch graph neighbors for each memory and attach them in place."""

    mem_ids = [m["id"] for m in memories]
    id_list = ", ".join(mem_ids)

    neighbor_surql = f"""
    SELECT id,
        ->relates.{{id, type, out}} AS out_edges,
        <-relates.{{id, type, in}} AS in_edges,
        ->relates->memory.{{id, content, category, salience}} AS out_memories,
        ->relates->entity.{{id, name, type}} AS out_entities,
        <-relates<-memory.{{id, content, category, salience}} AS in_memories,
        <-relates<-entity.{{id, name, type}} AS in_entities
    FROM [{id_list}]
    """

    rows = await query(db, neighbor_surql)
    if not rows or not isinstance(rows, list):
        return

    # Build lookup map
    neighbor_map: dict[str, dict] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            neighbor_map[str(row["id"])] = row

    # Build edge type lookup from edge data
    edge_types: dict[str, dict[str, str]] = {}  # {node_id: {target_id: edge_type}}
    for row in rows:
        if not isinstance(row, dict):
            continue
        node_id = str(row.get("id", ""))
        edge_types[node_id] = {}

        for edge in (row.get("out_edges") or []):
            if isinstance(edge, dict):
                target = str(edge.get("out", ""))
                edge_types[node_id][target] = edge.get("type", "relates")

        for edge in (row.get("in_edges") or []):
            if isinstance(edge, dict):
                source = str(edge.get("in", ""))
                edge_types[node_id][source] = edge.get("type", "relates")

    # Attach neighbors to each memory
    for mem in memories:
        mid = mem["id"]
        data = neighbor_map.get(mid, {})

        items: list[dict] = []
        seen: set[str] = set()

        # Out-going memory neighbors
        for m in (data.get("out_memories") or []):
            if isinstance(m, dict) and m.get("id"):
                tid = str(m["id"])
                if tid not in seen:
                    seen.add(tid)
                    items.append({
                        "id": tid,
                        "content_preview": (m.get("content") or "")[:80],
                        "category": m.get("category", ""),
                        "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                        "edge_direction": "out",
                    })

        # Out-going entity neighbors
        for e in (data.get("out_entities") or []):
            if isinstance(e, dict) and e.get("id"):
                tid = str(e["id"])
                if tid not in seen:
                    seen.add(tid)
                    items.append({
                        "id": tid,
                        "type": e.get("type", ""),
                        "name": e.get("name", ""),
                        "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                        "edge_direction": "out",
                    })

        # In-coming memory neighbors
        for m in (data.get("in_memories") or []):
            if isinstance(m, dict) and m.get("id"):
                tid = str(m["id"])
                if tid not in seen:
                    seen.add(tid)
                    items.append({
                        "id": tid,
                        "content_preview": (m.get("content") or "")[:80],
                        "category": m.get("category", ""),
                        "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                        "edge_direction": "in",
                    })

        # In-coming entity neighbors
        for e in (data.get("in_entities") or []):
            if isinstance(e, dict) and e.get("id"):
                tid = str(e["id"])
                if tid not in seen:
                    seen.add(tid)
                    items.append({
                        "id": tid,
                        "type": e.get("type", ""),
                        "name": e.get("name", ""),
                        "edge_type": edge_types.get(mid, {}).get(tid, "relates"),
                        "edge_direction": "in",
                    })

        mem["neighbors"] = {
            "count": len(items),
            "items": items[:MAX_NEIGHBORS_PER_NODE],
        }
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_core/test_get.py -v
```

Expected: All 5 tests pass.

- [ ] **Step 6: Commit**

```bash
git add qmemory/core/get.py tests/test_core/test_get.py
git commit -m "feat: add qmemory_get — fetch by ID with graph neighbor traversal"
```

---

## Task 5: Search Response Redesign + Entity Search

**Files:**
- Modify: `qmemory/core/search.py`
- Modify: `tests/test_core/test_search.py`

- [ ] **Step 1: Write the failing test for new response format**

In `tests/test_core/test_search.py`, add:

```python
async def test_search_response_has_new_format(db):
    """Search response should have pinned, entities, results, actions, meta."""
    await save_memory(content="Test the new format", category="context", salience=0.5, db=db)

    result = await search_memories(query_text="new format", db=db)

    assert "pinned" in result, "Response missing 'pinned'"
    assert "results" in result, "Response missing 'results'"
    assert "entities" in result, "Response missing 'entities'"
    assert "actions" in result, "Response missing 'actions'"
    assert "meta" in result, "Response missing 'meta'"
    assert isinstance(result["meta"], dict)
    assert "returned" in result["meta"]
    assert "has_more" in result["meta"]

    # Old format should be gone
    assert "_nudge" not in result, "Old _nudge should be replaced by actions"


async def test_search_pinned_separation(db):
    """Memories with salience >= 0.9 should appear in pinned, not results."""
    await save_memory(content="Critical rule always applies", category="self", salience=1.0, db=db)
    await save_memory(content="Normal fact about testing", category="context", salience=0.5, db=db)

    result = await search_memories(query_text="testing", db=db)

    pinned_contents = [m["content"] for m in result["pinned"]]
    result_contents = [m["content"] for m in result["results"]]

    assert "Critical rule always applies" in pinned_contents
    assert "Critical rule always applies" not in result_contents


async def test_search_results_have_relevance_and_tier(db):
    """Each result should have relevance score and source_tier."""
    await save_memory(content="Relevance test fact", category="context", db=db)

    result = await search_memories(query_text="relevance test", db=db)

    for r in result["results"]:
        assert "relevance" in r, f"Result missing 'relevance': {r.get('id')}"
        assert "source_tier" in r, f"Result missing 'source_tier': {r.get('id')}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_core/test_search.py::test_search_response_has_new_format tests/test_core/test_search.py::test_search_pinned_separation tests/test_core/test_search.py::test_search_results_have_relevance_and_tier -v
```

Expected: FAIL — current format doesn't have pinned/entities/actions/meta.

- [ ] **Step 3: Rewrite `search_memories()` in `search.py`**

Replace the `search_memories()` function body in `qmemory/core/search.py`:

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
    db: Any = None,
) -> dict:
    """Search memories with graph enrichment. Returns structured JSON with
    pinned, entities, results, actions, and meta."""

    logger.debug("Searching with owner=%s", owner_id)
    categories = [category] if category else None

    async def _run(conn):
        # Step 1: Run recall pipeline
        results = await recall(
            query_text=query_text,
            scope=scope,
            categories=categories,
            limit=limit + 3,  # fetch extra to account for pinned extraction
            offset=offset,
            owner_id=owner_id,
            source_type=source_type,
            after=after,
            before=before,
            db=conn,
        )

        # Step 2: Separate pinned (salience >= 0.9) from regular results
        pinned = []
        regular = []
        for r in results:
            if r.get("salience", 0) >= 0.9 and len(pinned) < 3:
                pinned.append(_format_result(r))
            else:
                regular.append(r)

        # Step 3: Enrich regular results with connections
        regular = await _enrich_with_connections(regular, conn)

        # Step 4: Format results with relevance + source_tier + age
        formatted_results = [_format_result(r) for r in regular[:limit]]

        # Step 5: Search entities in parallel
        entities = []
        if query_text:
            entities = await _search_entities(query_text, conn)

        # Step 6: Build response
        has_more = len(regular) > limit
        first_connected = next(
            (r for r in formatted_results if r.get("neighbors", {}).get("count", 0) > 0),
            None,
        )
        first_entity = entities[0] if entities else None

        response = {
            "pinned": pinned,
            "entities": entities,
            "results": formatted_results,
        }

        from qmemory.formatters.response import attach_meta
        return attach_meta(
            response,
            actions_context={
                "type": "search",
                "memory_id": first_connected["id"] if first_connected else None,
                "neighbor_count": first_connected["neighbors"]["count"] if first_connected else 0,
                "entity_id": first_entity["id"] if first_entity else None,
            },
            returned=len(formatted_results),
            offset=offset,
            has_more=has_more,
        )

    if db is not None:
        return await _run(db)
    else:
        async with get_db() as conn:
            return await _run(conn)


def _format_result(r: dict) -> dict:
    """Format a raw recall result into the agent-facing format."""
    from qmemory.core.recall import _format_age

    formatted = {
        "id": str(r.get("id", "")),
        "content": r.get("content", ""),
        "category": r.get("category", ""),
        "salience": r.get("salience", 0),
        "relevance": round(r.get("_score", r.get("salience", 0)), 3),
        "source_tier": r.get("source_tier", "unknown"),
        "age": _format_age(r.get("created_at")),
        "created_at": str(r.get("created_at", "")),
    }

    # Carry over connections if enriched
    if "connections" in r:
        # Convert old format to new neighbors format
        conn = r["connections"]
        formatted["neighbors"] = {
            "count": conn.get("total", 0),
            "items": [
                {
                    "id": h.get("target", ""),
                    "content_preview": h.get("target_name", "")[:80],
                    "edge_type": h.get("type", "relates"),
                    "edge_direction": "out",
                }
                for h in conn.get("hints", [])
            ],
        }
    else:
        formatted["neighbors"] = {"count": 0, "items": []}

    return formatted
```

- [ ] **Step 4: Add entity search function to `search.py`**

```python
async def _search_entities(query_text: str, db: Any) -> list[dict]:
    """Search entity table for persons/concepts matching the query."""
    try:
        params = {"query": query_text.lower()}

        entity_surql = """
        SELECT id, name, type
        FROM entity
        WHERE is_active != false
            AND (
                string::contains(string::lowercase(name), $query)
                OR $query IN aliases
            )
        LIMIT 5;
        """

        rows = await query(db, entity_surql, params)
        if not rows or not isinstance(rows, list):
            return []

        entities = []
        for e in rows:
            if not isinstance(e, dict) or not e.get("id"):
                continue

            eid = str(e["id"])
            # Count linked memories
            count_rows = await query(
                db,
                "SELECT count() AS c FROM relates WHERE in = <record>$eid OR out = <record>$eid GROUP ALL",
                {"eid": eid},
            )
            mem_count = count_rows[0]["c"] if count_rows and isinstance(count_rows, list) and count_rows[0].get("c") else 0

            entities.append({
                "id": eid,
                "name": e.get("name", ""),
                "type": e.get("type", ""),
                "memory_count": mem_count,
            })

        return entities

    except Exception as ex:
        logger.debug("Entity search failed (non-fatal): %s", ex)
        return []
```

- [ ] **Step 5: Update existing search tests for new format**

In `tests/test_core/test_search.py`, update `test_search_returns_results`:

```python
async def test_search_returns_results(db):
    """After saving a memory, searching for it should return at least one result."""
    await save_memory(content="The quarterly budget is 500K", category="context", salience=0.8, db=db)

    result = await search_memories(query_text="quarterly budget", db=db)

    assert isinstance(result, dict)
    assert "results" in result
    assert len(result["results"]) >= 1

    first = result["results"][0]
    assert "content" in first
    assert "id" in first
    assert "actions" in result
    assert "meta" in result
```

- [ ] **Step 6: Run all search tests**

```bash
uv run pytest tests/test_core/test_search.py -v
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add qmemory/core/search.py tests/test_core/test_search.py
git commit -m "feat: redesign search response — pinned/entities/results + actions + meta"
```

---

## Task 6: Update Remaining Tools' Responses

**Files:**
- Modify: `qmemory/core/save.py`
- Modify: `qmemory/core/correct.py`
- Modify: `qmemory/core/link.py`
- Modify: `qmemory/core/person.py`
- Modify: `qmemory/core/books.py`

Each tool gets `actions` + `meta` replacing `_nudge`.

- [ ] **Step 1: Update `save.py` response**

In `save_memory()`, replace the return block (around line 284-299) with:

```python
    from qmemory.formatters.response import attach_meta

    final_action = decision if decision in ("ADD", "UPDATE") else "ADD"

    response = {
        "action": final_action,
        "memory_id": full_memory_id,
        "content": content[:80],
    }

    return attach_meta(
        response,
        actions_context={
            "type": "save",
            "memory_id": full_memory_id,
            "content_preview": content,
            "dedup_similar_id": dedup_result.get("update_id"),
        },
        dedup_checked=True,
        dedup_candidates=dedup_result.get("candidates", 0) if isinstance(dedup_result, dict) else 0,
        embedding_generated=embedding is not None,
        indexed=True,
    )
```

Also update the NOOP return (around line 140):

```python
    if decision == "NOOP":
        from qmemory.formatters.response import attach_meta
        response = {"action": "NOOP", "memory_id": None}
        return attach_meta(response, dedup_checked=True, indexed=False)
```

- [ ] **Step 2: Update `correct.py` responses**

In `_handle_delete()`, replace the return (around line 191):

```python
    from qmemory.formatters.response import attach_meta
    response = {"ok": True, "action": "deleted", "memory_id": memory_id}
    return attach_meta(
        response,
        actions_context={"type": "correct", "memory_id": memory_id},
    )
```

In `_handle_correct()`, replace the return (around line 444):

```python
    from qmemory.formatters.response import attach_meta
    response = {
        "ok": True,
        "action": "corrected",
        "old_id": memory_id,
        "new_id": new_memory_id,
        "changes": {"content": f"{(old_memory.get('content', ''))[:40]} → {new_content[:40]}"},
    }
    return attach_meta(
        response,
        actions_context={"type": "correct", "new_memory_id": new_memory_id},
    )
```

In `_handle_update()`, replace the return (around line 264):

```python
    from qmemory.formatters.response import attach_meta
    response = {
        "ok": True,
        "action": "updated",
        "memory_id": memory_id,
        "changes": {k: v for k, v in updates.items() if k in ALLOWED_UPDATE_FIELDS},
    }
    return attach_meta(
        response,
        actions_context={"type": "correct", "memory_id": memory_id},
    )
```

In `_handle_unlink()`, replace the return (around line 305):

```python
    from qmemory.formatters.response import attach_meta
    response = {"ok": True, "action": "unlinked", "edge_id": edge_id}
    return attach_meta(response)
```

Also update the not_found returns in `_handle_delete` and `_handle_correct` similarly.

- [ ] **Step 3: Update `link.py` response**

In `_create_link()`, replace the return block (around line 238-253):

```python
    from qmemory.formatters.response import attach_meta

    # Fetch content previews for both ends
    from_preview = ""
    to_preview = ""
    if from_exists and isinstance(from_exists, list):
        from_preview = (from_exists[0].get("content") or from_exists[0].get("name", ""))[:80]
    if to_exists and isinstance(to_exists, list):
        to_preview = (to_exists[0].get("content") or to_exists[0].get("name", ""))[:80]

    # Count edges for both endpoints
    from_count_rows = await query(db, "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL", {"id": from_id})
    to_count_rows = await query(db, "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL", {"id": to_id})
    from_edge_count = from_count_rows[0]["c"] if from_count_rows and isinstance(from_count_rows, list) and from_count_rows[0].get("c") else 0
    to_edge_count = to_count_rows[0]["c"] if to_count_rows and isinstance(to_count_rows, list) and to_count_rows[0].get("c") else 0

    response = {
        "edge_id": edge_id,
        "from": {"id": from_id, "content_preview": from_preview},
        "to": {"id": to_id, "content_preview": to_preview},
        "relationship_type": relationship_type,
    }

    return attach_meta(
        response,
        actions_context={
            "type": "link",
            "from_id": from_id,
            "to_id": to_id,
            "edge_count_from": from_edge_count,
            "edge_count_to": to_edge_count,
        },
        edge_count_from=from_edge_count,
        edge_count_to=to_edge_count,
    )
```

- [ ] **Step 4: Update `person.py` response**

In `_create_person_impl()`, replace the return (around line 308):

```python
    from qmemory.formatters.response import attach_meta

    # Count memories linked to this person
    mem_count_rows = await query(
        db,
        "SELECT count() AS c FROM relates WHERE in = <record>$id OR out = <record>$id GROUP ALL",
        {"id": person_id},
    )
    mem_count = mem_count_rows[0]["c"] if mem_count_rows and isinstance(mem_count_rows, list) and mem_count_rows[0].get("c") else 0

    response = {
        "entity_id": person_id,
        "name": name,
        "action": action,
        "contacts": [
            {"system": c.get("system", ""), "handle": c.get("handle", ""), "entity_id": cid}
            for c, cid in zip(contacts, contact_ids)
        ] if contacts else [],
    }

    return attach_meta(
        response,
        actions_context={"type": "person", "entity_id": person_id, "memory_count": mem_count},
        memory_count=mem_count,
        contact_count=len(contact_ids),
        links_created=links_created,
    )
```

- [ ] **Step 5: Update `books.py` responses**

In `list_books()`, replace the return block inside `_run()`:

```python
        from qmemory.formatters.response import attach_meta
        book_list = [
            {"id": str(b["id"]), "name": b.get("name", ""), "chunk_count": b.get("chunk_count", 0)}
            for b in books if isinstance(b, dict)
        ]
        response = {"books": book_list}
        return attach_meta(
            response,
            actions_context={"type": "books"},
            total_books=len(book_list),
            level="list",
        )
```

Apply the same pattern to `list_sections()` and `read_section()` — replace `_nudge` with `attach_meta()`.

- [ ] **Step 6: Run all existing tests**

```bash
uv run pytest tests/ -v
```

Expected: Most tests pass. Some test assertions that check for `_nudge` key will need updating.

- [ ] **Step 7: Fix any tests that check for `_nudge`**

Search for `_nudge` in test files and update assertions to check for `actions` and `meta` instead.

- [ ] **Step 8: Commit**

```bash
git add qmemory/core/save.py qmemory/core/correct.py qmemory/core/link.py qmemory/core/person.py qmemory/core/books.py tests/
git commit -m "feat: add actions + meta to all tool responses, replace _nudge"
```

---

## Task 7: MCP Layer — Both Transports

**Files:**
- Modify: `qmemory/mcp/server.py`
- Modify: `qmemory/app/main.py`

- [ ] **Step 1: Add `qmemory_get` tool to `server.py`**

Add after the qmemory_search tool definition:

```python
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_get(
    ids: list[str],
    include_neighbors: bool = False,
    neighbor_depth: int = 1,
) -> str:
    """Fetch memories or entities by ID with optional graph neighbor traversal.

    Use this to:
    - Retrieve specific memories when you have their IDs
    - Explore the graph by following connections from search results
    - Verify that saved memories exist

    Args:
        ids:                List of record IDs to fetch.
                            Examples: ["memory:mem123abc", "entity:ent456xyz"]
                            Max 20 IDs per call.
        include_neighbors:  If True, also fetch connected nodes for each result.
                            Shows what each memory is linked to in the graph.
        neighbor_depth:     How deep to traverse connections (1 or 2). Default 1.
                            Depth 2 follows connections of connections.

    Returns JSON with:
    - memories: list of found records with full content + neighbor previews
    - not_found: list of IDs that don't exist (for verification)
    - actions: suggested next steps
    - meta: {found, requested}
    """
    from qmemory.core.get import get_memories

    result = await get_memories(
        ids=ids,
        include_neighbors=include_neighbors,
        neighbor_depth=neighbor_depth,
    )
    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 2: Update `qmemory_search` params in `server.py`**

Add `offset`, `after`, `before` parameters to the existing `qmemory_search`:

```python
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
) -> str:
```

Update the docstring to document the new params. Pass them through to `search_memories()`:

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
    )
```

- [ ] **Step 3: Mirror all changes in `app/main.py`**

Copy the exact same `qmemory_get` tool definition and `qmemory_search` param updates to `qmemory/app/main.py`. Add timing/logging wrappers to match the existing pattern in that file:

```python
@mcp.tool()
async def qmemory_get(
    ids: list[str],
    include_neighbors: bool = False,
    neighbor_depth: int = 1,
) -> str:
    """Fetch memories or entities by ID with optional graph neighbor traversal.
    [same docstring as server.py]
    """
    start = time.monotonic()
    logger.info("Tool call: qmemory_get(ids=%s, neighbors=%s)", ids[:3], include_neighbors)

    from qmemory.core.get import get_memories

    result = await get_memories(
        ids=ids,
        include_neighbors=include_neighbors,
        neighbor_depth=neighbor_depth,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_get completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 4: Update `qmemory_bootstrap` in both files to return JSON**

In both `server.py` and `main.py`, change bootstrap to return JSON instead of text:

```python
async def qmemory_bootstrap(session_key: str = "default") -> str:
    """Load your full memory context for this session.
    [existing docstring]
    """
    from qmemory.core.recall import assemble_context

    result = await assemble_context(session_key)

    # If result is already a dict (new format), serialize it
    # If it's a string (old format during transition), wrap it
    if isinstance(result, dict):
        return json.dumps(result, default=str, ensure_ascii=False)
    return result
```

Note: The actual conversion of `assemble_context()` to return a dict happens in Task 8.

- [ ] **Step 5: Update CLAUDE.md tool count**

In `/Users/qusaiabushanap/dev/qmemory-py/CLAUDE.md`, update the tool table to include `qmemory_get` (9 tools total).

- [ ] **Step 6: Commit**

```bash
git add qmemory/mcp/server.py qmemory/app/main.py CLAUDE.md
git commit -m "feat: add qmemory_get to MCP, update search params, sync both transports"
```

---

## Task 8: Convert Bootstrap to Structured JSON

**Files:**
- Modify: `qmemory/core/recall.py` (the `assemble_context()` and `_assemble()` functions)
- Modify: `tests/test_core/test_recall.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_core/test_recall.py`:

```python
async def test_assemble_context_returns_dict(db):
    """assemble_context should return a dict with self_model, memories, actions, meta."""
    await save_memory(content="I am Dona", category="self", salience=0.9, db=db)
    await save_memory(content="User likes dark mode", category="preference", salience=0.7, db=db)

    result = await assemble_context("default", db=db)

    assert isinstance(result, dict), "assemble_context should return a dict"
    assert "self_model" in result
    assert "memories" in result
    assert "actions" in result
    assert "meta" in result
    assert "total_memories" in result["meta"]
    assert "categories" in result["meta"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_core/test_recall.py::test_assemble_context_returns_dict -v
```

Expected: FAIL — currently returns a string.

- [ ] **Step 3: Rewrite `_assemble()` to return a dict**

In `qmemory/core/recall.py`, replace `_assemble()`:

```python
async def _assemble(session_key: str, owner_id: str | None, db: Any) -> dict:
    """Internal assembly logic — returns structured dict."""
    from qmemory.formatters.response import attach_meta

    parsed = parse_session_key(session_key)
    scope = parsed["scope"]

    # Load self-model
    self_memories = await _tier3_category_filter(
        categories=["self"], scope=None, min_salience=0, limit=10, db=db,
    )

    # Load all other memories
    memories = await recall(
        scope=scope if scope != "global" else None,
        limit=50, owner_id=owner_id, db=db,
    )

    self_ids = {str(m.get("id", "")) for m in self_memories}
    other_memories = [m for m in memories if str(m.get("id", "")) not in self_ids]

    # Group by category
    grouped: dict[str, list] = {}
    for m in other_memories:
        cat = m.get("category", "context")
        if cat == "self":
            continue
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append({
            "id": str(m.get("id", "")),
            "content": m.get("content", ""),
            "salience": m.get("salience", 0),
            "age": _format_age(m.get("created_at")),
        })

    # Count all categories
    cat_counts: dict[str, int] = {}
    for m in self_memories:
        cat_counts["self"] = cat_counts.get("self", 0) + 1
    for m in other_memories:
        cat = m.get("category", "context")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

    total = len(self_memories) + len(other_memories)

    response = {
        "self_model": [
            {"id": str(m.get("id", "")), "content": m.get("content", "")}
            for m in self_memories
        ],
        "memories": grouped,
    }

    return attach_meta(
        response,
        actions_context={"type": "bootstrap", "total_memories": total},
        total_memories=total,
        categories=cat_counts,
        session_scope=scope,
    )
```

- [ ] **Step 4: Run tests**

```bash
uv run pytest tests/test_core/test_recall.py -v
```

Expected: All tests pass. Existing tests that check `assemble_context` returns a string will need updating.

- [ ] **Step 5: Fix any broken tests**

Update any test that asserts `isinstance(result, str)` to `isinstance(result, dict)`.

- [ ] **Step 6: Commit**

```bash
git add qmemory/core/recall.py tests/test_core/test_recall.py
git commit -m "feat: convert bootstrap to structured JSON — self_model, memories, actions, meta"
```

---

## Task 9: Full Test Suite + Cleanup

**Files:**
- All test files
- Any remaining cleanup

- [ ] **Step 1: Run the full test suite**

```bash
uv run pytest tests/ -v
```

Fix any remaining failures.

- [ ] **Step 2: Check for any remaining `_nudge` references**

```bash
grep -r "_nudge" qmemory/ tests/ --include="*.py"
```

Remove or replace any remaining `_nudge` references.

- [ ] **Step 3: Verify both MCP transports are in sync**

Manually compare `qmemory/mcp/server.py` and `qmemory/app/main.py` — same tools, same params, same docstrings.

- [ ] **Step 4: Run full test suite one final time**

```bash
uv run pytest tests/ -v
```

Expected: All tests pass (except the 9 known SurrealDB v3 edge query failures).

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: cleanup _nudge references, sync transports, verify full test suite"
```
