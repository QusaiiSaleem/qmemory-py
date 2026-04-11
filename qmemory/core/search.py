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
import re
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
VECTOR_RERANK_MIN_QUERY_CHARS = 6  # Skip reranker for queries shorter than this
VECTOR_RERANK_MIN_QUERY_WORDS = 2  # Skip reranker for single-word queries
ENTITY_LEG_LIMIT = 5        # Max entities to return
CONTENT_LEG_LIMIT = 50      # Max BM25 content results (pre-fusion)
GRAPH_LEG_LIMIT = 15        # Max graph-traversal results (pre-fusion)
DIVERSITY_CAP = 0.6         # Max fraction of results any single category can fill

# Category display order — self always first
CATEGORY_ORDER = [
    "self", "style", "preference", "context",
    "decision", "idea", "feedback", "domain",
]

# Min word length for entity name matching in graph leg
_MIN_WORD_LEN = 3
_MAX_ENTITY_WORDS = 10


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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
    logger.debug(
        "Search: query=%s category=%s entity_id=%s owner=%s",
        query_text, category, entity_id, owner_id,
    )

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
        # Only fires when ALL of these hold:
        #   1. There IS a query (not just "fetch recent")
        #   2. BM25 + graph found fewer than VECTOR_RERANK_THRESHOLD candidates
        #   3. The query has substance: at least 2 words AND >= 6 chars
        #
        # The third gate exists because vector reranking on near-empty queries
        # like "test" or "x" runs cosine similarity over the entire embedding
        # space (no useful BM25 candidates to seed from), which on the remote
        # SurrealDB took 193 seconds for one call in production. Single-token
        # queries don't benefit from semantic rescue — if BM25 didn't find
        # anything, vector won't either.
        if query_text and len(fused_memories) < VECTOR_RERANK_THRESHOLD:
            stripped = (query_text or "").strip()
            word_count = len(stripped.split())
            if (
                len(stripped) >= VECTOR_RERANK_MIN_QUERY_CHARS
                and word_count >= VECTOR_RERANK_MIN_QUERY_WORDS
            ):
                fused_memories = await _vector_rerank(
                    query_text, fused_memories, filters, limit, conn
                )
            else:
                logger.debug(
                    "search.vector_rerank_skipped reason=query_too_thin "
                    "len=%d words=%d",
                    len(stripped), word_count,
                )

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
    """BM25 fulltext search on memory.content.

    SurrealDB v3 has TWO bugs that compound here:

    1. `search::score(0)` always returns 0.0 — there's no server-side
       BM25 relevance ranking we can sort by.
    2. `WHERE content @@ $param` returns WRONG rows when the query text
       is bound via a parameter. The same query as a literal string
       (`@@ "..."`) returns the correct rows. Verified directly against
       production: parameterized form returned 0 NCNP-related rows for
       "الاستراتيجية الوطنية" while the literal form returned all 28.

    Workaround: inline the query text as a properly-escaped literal so
    we can actually find the matching rows. Then re-rank in Python by
    term frequency (count of query token occurrences in content) since
    we can't get scores from the server.

    Limit: fetch a wide pool (CONTENT_LEG_LIMIT, default 50) for RRF
    fusion to work with. Don't cap at the user's per-page limit.
    """
    params: dict[str, Any] = {"limit": CONTENT_LEG_LIMIT}
    params.update(filters["params"])

    # Inline the query text as an escaped literal — see workaround note above.
    escaped_query = _escape_surql_string(query_text)

    entity_clause = ""
    if entity_id:
        entity_clause = (
            " AND id IN (SELECT VALUE in FROM relates WHERE out = <record>$entity_id)"
        )
        params["entity_id"] = entity_id

    surql = f"""
    SELECT {MEMORY_FIELDS} FROM memory
    WHERE content @@ "{escaped_query}"
        AND is_active = true
        AND (valid_until IS NONE OR valid_until > time::now())
        {entity_clause}
        {filters["clauses"]}
    LIMIT $limit;
    """

    results = await query(db, surql, params)
    if not results or not isinstance(results, list):
        return []

    # Python-side ranking by term frequency. Robust to Arabic punctuation
    # and stemming because it counts substring occurrences, not whole-token
    # matches. Heavy weight on relevance, salience as tiebreaker.
    query_tokens = [
        t for t in _tokenize_for_relevance(query_text)
        if len(t) >= 2
    ]
    for r in results:
        content_lower = (r.get("content") or "").lower()
        # Sum of substring occurrences for each query token. A memory that
        # contains the full phrase scores higher than one with just one word.
        tf = sum(content_lower.count(t) for t in query_tokens)
        r["_leg"] = "content"
        r["_bm25_relevance"] = min(1.0, tf / max(len(query_tokens) * 2, 1))
        r["_term_freq"] = tf

    # Sort by term frequency DESC, then salience DESC as tiebreaker.
    results.sort(
        key=lambda r: (
            r.get("_term_freq", 0),
            r.get("salience", 0) or 0,
        ),
        reverse=True,
    )
    for i, r in enumerate(results):
        r["_rank"] = i

    logger.debug(
        "Content leg: %d results (top tf=%d)",
        len(results),
        results[0].get("_term_freq", 0) if results else 0,
    )
    return results


def _tokenize_for_relevance(text: str) -> list[str]:
    """Lowercase + split on whitespace + strip surrounding punctuation.

    Used only by the Python-side relevance scoring in _content_leg. We
    don't try to do stem extraction here (the BM25 index already does
    that on the SQL side); we just want a clean list of tokens to feed
    `content.count(token)` substring counts.
    """
    raw = text.lower().split()
    return [t.strip(".,!?:;()[]{}\"'`—–-…") for t in raw if t.strip(".,!?:;()[]{}\"'`—–-…")]


def _escape_surql_string(s: str) -> str:
    """Escape a string for inline use in a SurrealQL double-quoted literal.

    Required because SurrealDB v3's `WHERE content @@ $param` form returns
    wrong rows for fulltext search; only the literal form `@@ "..."` works
    correctly. We escape backslash and double-quote, the only metacharacters
    that can break out of a double-quoted SurrealQL string.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Leg 2: Entity BM25
# ---------------------------------------------------------------------------


async def _entity_leg(query_text: str, db: Any) -> list[dict]:
    """BM25 fulltext search on entity.name. Returns entity dicts, not memories."""
    params: dict[str, Any] = {"query": query_text, "limit": ENTITY_LEG_LIMIT}

    # BM25 search on entity names (uses idx_entity_name_ft)
    surql = """
    SELECT id, name, type, aliases
    FROM entity
    WHERE name @@ $query
        AND is_active != false
    LIMIT $limit;
    """

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


# ---------------------------------------------------------------------------
# Leg 3: Graph Traversal
# ---------------------------------------------------------------------------


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

    # Find matching entities
    entity_surql = f"""
    SELECT id FROM entity
    WHERE {" OR ".join(match_conditions)}
    LIMIT 10;
    """
    entity_rows = await query(db, entity_surql, params)

    if not entity_rows or not isinstance(entity_rows, list):
        return []

    entity_ids = [
        str(e["id"]) for e in entity_rows
        if isinstance(e, dict) and e.get("id")
    ]
    if not entity_ids:
        return []

    # Fetch memories linked to these entities.
    #
    # Performance: traverse FROM the (small) relates edge set OUTWARD
    # instead of `WHERE id IN (subquery)` against the (large) memory
    # table. The old `id IN (...)` form forced a full memory scan
    # checking each row against the edge set — for hub entities with
    # many edges this took 191 seconds in production.
    #
    # Field projection: use the destructuring `.{}` syntax to pull only
    # the fields in MEMORY_FIELDS (excluding the 1024-float `embedding`
    # array, which would waste ~120KB per call for 15 results). The
    # skill's gotcha doc warns against `SELECT *` on memory tables for
    # exactly this reason.
    all_memories: list[dict] = []
    for eid in entity_ids:
        # NOTE: v3 ORDER BY requires every order idiom to appear in the SELECT
        # clause. We use `in.<field> AS <field>` aliases so the ordered field
        # `in.salience` is "in selection" while still flattening the record
        # to top-level keys for the rest of the pipeline.
        mem_surql = """
        SELECT
            in.id AS id,
            in.content AS content,
            in.category AS category,
            in.salience AS salience,
            in.scope AS scope,
            in.confidence AS confidence,
            in.source_type AS source_type,
            in.evidence_type AS evidence_type,
            in.is_active AS is_active,
            in.linked AS linked,
            in.recall_count AS recall_count,
            in.last_recalled AS last_recalled,
            in.context_mood AS context_mood,
            in.source_person AS source_person,
            in.prev_version AS prev_version,
            in.valid_until AS valid_until,
            in.created_at AS created_at,
            in.updated_at AS updated_at
        FROM relates
        WHERE out = <record>$eid
            AND in.is_active = true
            AND (in.valid_until IS NONE OR in.valid_until > time::now())
        ORDER BY in.salience DESC
        LIMIT $limit;
        """
        mem_params: dict[str, Any] = {"eid": eid, "limit": GRAPH_LEG_LIMIT}
        rows = await query(db, mem_surql, mem_params)
        if rows and isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    all_memories.append(row)

    # Tag results
    for i, m in enumerate(all_memories):
        m["_leg"] = "graph"
        m["_rank"] = i

    logger.debug(
        "Graph leg: %d memories from %d entities",
        len(all_memories), len(entity_ids),
    )
    return all_memories


async def _graph_from_entity(
    entity_id: str, filters: dict, db: Any
) -> list[dict]:
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


# ---------------------------------------------------------------------------
# Recent fallback (no query)
# ---------------------------------------------------------------------------


async def _recent_fallback(
    filters: dict, limit: int, db: Any
) -> list[dict]:
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
        logger.debug(
            "Vector rerank: %d candidates -> %d merged",
            len(candidates), len(merged),
        )
        return merged

    except Exception as e:
        logger.debug("Vector rerank failed (non-fatal): %s", e)
        return candidates


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
    enriched_top = await _enrich_with_graph(paginated[:TOP_N_ENRICH], db)
    paginated = enriched_top + paginated[TOP_N_ENRICH:]

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

    # Apply type diversity cap: no single category may exceed
    # DIVERSITY_CAP fraction of `limit` results. Prevents monoculture
    # (e.g. 10 out of 10 results from one book).
    per_cat_cap = max(1, int(limit * DIVERSITY_CAP))
    memories_grouped = {
        cat: mems[:per_cat_cap] for cat, mems in memories_grouped.items()
    }
    # Keep by_category counts in sync with the capped totals so meta
    # reflects what the caller actually sees.
    by_category = {cat: len(mems) for cat, mems in memories_grouped.items()}

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
    sections = [
        k for k in [
            "entities_matched", "pinned", "memories",
            "book_insights", "hypotheses",
        ]
        if k in response
    ]

    # Count results per leg
    search_legs: dict[str, int] = {}
    for mem in fused_memories:
        leg = mem.get("_leg", "unknown")
        search_legs[leg] = search_legs.get(leg, 0) + 1

    # Drill-down actions
    drill_down = (
        build_category_drill_down(query_text, by_category)
        if query_text
        else []
    )

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


# ---------------------------------------------------------------------------
# Book-link detection (batch)
# ---------------------------------------------------------------------------


async def _find_book_linked_memories(
    memory_ids: list[str], db: Any
) -> dict[str, dict]:
    """Check which memories have from_book edges.

    Returns {memory_id: {book_id, title}}.
    """
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


# ---------------------------------------------------------------------------
# Graph enrichment (attaches _graph to each memory)
# ---------------------------------------------------------------------------


async def _enrich_with_graph(
    memories: list[dict], db: Any
) -> list[dict]:
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

            # Build edge type map
            edge_type_map: dict[str, str] = {}
            for edge in (data.get("out_edges") or []):
                if isinstance(edge, dict):
                    edge_type_map[str(edge.get("out", ""))] = edge.get("type", "relates")
            for edge in (data.get("in_edges") or []):
                if isinstance(edge, dict):
                    edge_type_map[str(edge.get("in", ""))] = edge.get("type", "relates")

            # Build entity list
            entities: list[dict] = []
            seen_entities: set[str] = set()
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
                    "from_book": None,
                },
            }

        except Exception as ex:
            logger.debug(
                "Graph enrichment failed for %s (non-fatal): %s", mid, ex
            )

    return enriched
