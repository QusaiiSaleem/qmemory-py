"""
Core Recall — 4-Tier Memory Pipeline + Context Assembly

This is the READ side of Qmemory. While save.py, correct.py, and link.py
write to the graph, THIS module reads from it — retrieving the memories
most relevant to the current session and assembling them into a formatted
context block for AI agents.

Two main functions:

  recall()           — The 4-tier recall cascade. Finds the most relevant
                       memories by trying four strategies in priority order,
                       merging results, and sorting by salience.

  assemble_context() — Builds the full context block the agent sees.
                       Calls recall() internally and formats everything
                       into a single string: self-model first, then
                       session header, then memories grouped by category.

The 4 tiers (tried in order, results merged):
  1. Graph traversal — Find entities matching query words, then traverse
     their `relates` edges to find connected memories.
  2. BM25 + Vector  — Full-text search (@@) and cosine similarity search.
     BM25 always works; vector search only runs if embeddings exist.
  3. Category filter — If specific categories were requested, fetch
     memories matching those categories sorted by salience.
  4. Recent fallback — If we still don't have enough results, grab the
     most recently created memories as a safety net.

Design decisions:
  - Each tier only runs if previous tiers didn't find enough results
    (threshold: 1.5x the requested limit). This saves DB round-trips.
  - All results are deduplicated by ID (graph + BM25 may overlap).
  - Final sort is by salience DESC (most important first).
  - Optional token_budget trims the results to fit a token cap.
  - Accepts an optional `db` connection for test injection.
  - parse_session_key() is a pure function — no DB needed, easy to test.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from qmemory.constants import MEMORY_CATEGORIES
from qmemory.core.embeddings import generate_query_embedding
from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many memories the "recent fallback" tier fetches
RECENT_FALLBACK_LIMIT = 15

# Minimum query length before we attempt graph traversal (Tier 1)
# Short queries like "hi" or "ok" won't produce useful entity matches
MIN_QUERY_LENGTH_FOR_GRAPH = 10

# Minimum word length when extracting entity candidates from query text
MIN_WORD_LENGTH = 3

# Maximum number of words to extract from query for entity matching
MAX_ENTITY_WORDS = 10


# ---------------------------------------------------------------------------
# Session key parsing (pure function — no DB needed)
# ---------------------------------------------------------------------------


def parse_session_key(session_key: str) -> dict:
    """
    Parse a session key string into its components.

    Session keys encode the channel, group, and topic information for a
    conversation context. The format varies but typically looks like:
      "agent:main:telegram:group:-123:topic:7"
      "telegram:group:456:topic:12"
      "test:session:1"

    This function extracts:
      - channel:  The messaging platform ("telegram", "discord", etc.)
      - topic_id: The topic ID within a group (for supergroup topics)
      - group_id: The group/chat ID
      - scope:    The visibility scope derived from the key
      - chat_type: The type of chat ("direct", "group", "subagent", "cron")

    Args:
        session_key: The raw session key string.

    Returns:
        dict with keys: channel, topic_id, group_id, scope, chat_type
    """
    # Default values — returned when we can't parse anything useful
    result = {
        "channel": None,
        "topic_id": None,
        "group_id": None,
        "scope": "global",
        "chat_type": "direct",
    }

    if not session_key:
        return result

    # Split on colons to examine each segment
    parts = session_key.split(":")

    # Walk through the parts looking for known segment types
    for i, part in enumerate(parts):
        # Channel detection — recognized platform names
        if part in ("telegram", "discord", "slack", "whatsapp"):
            result["channel"] = part

        # Topic segment — the next part is the topic ID
        elif part == "topic" and i + 1 < len(parts):
            result["topic_id"] = parts[i + 1]
            result["scope"] = f"topic:{parts[i + 1]}"

        # Group segment — the next part is the group ID
        elif part == "group" and i + 1 < len(parts):
            result["group_id"] = parts[i + 1]
            result["chat_type"] = "group"
            # Only set group scope if there's no topic (topic is more specific)
            if result["scope"] == "global":
                result["scope"] = f"group:{parts[i + 1]}"

    # Check for special session types
    if "subagent" in session_key:
        result["chat_type"] = "subagent"
    if "cron" in session_key:
        result["chat_type"] = "cron"

    return result


# ---------------------------------------------------------------------------
# Main recall function — the 4-tier pipeline
# ---------------------------------------------------------------------------


async def recall(
    query_text: str | None = None,
    scope: str | None = None,
    categories: list[str] | None = None,
    limit: int = 20,
    min_salience: float | None = None,
    token_budget: int | None = None,
    db: Any = None,
) -> list[dict]:
    """
    Run the 4-tier recall pipeline to find the most relevant memories.

    Each tier tries a different strategy. Results from all tiers are merged,
    deduplicated by ID, sorted by salience (most important first), and
    trimmed to the requested limit.

    Args:
        query_text:    Free-text search query. Used by Tier 1 (graph) and
                       Tier 2 (BM25/vector). If None, only Tiers 3 and 4 run.
        scope:         Filter memories to this scope (e.g. "global",
                       "topic:7"). If None, searches all scopes.
        categories:    Filter to specific categories (e.g. ["context", "self"]).
                       Used by Tier 3 (category filter).
        limit:         Maximum number of memories to return. Default 20.
        min_salience:  Minimum salience threshold. Memories below this are
                       excluded from Tier 3 (category) and Tier 4 (recent).
        token_budget:  If set, trim results to fit within this many tokens
                       (estimated at ~4 chars per token).
        db:            Optional SurrealDB connection. If None, creates a
                       fresh one via get_db().

    Returns:
        List of memory dicts, sorted by salience DESC, deduplicated by ID.
        Each dict has at minimum: id, content, category, salience, scope.
    """
    # We'll collect results from all tiers into this list
    collected: list[dict] = []

    # The target count controls when we skip later tiers.
    # If we already have 1.5x the requested limit, skip the next tier.
    target_count = limit

    if db is not None:
        # Test mode: use the provided connection directly
        collected = await _run_tiers(
            query_text, scope, categories, limit, min_salience,
            target_count, db,
        )
    else:
        # Production mode: create a fresh connection for this operation
        async with get_db() as conn:
            collected = await _run_tiers(
                query_text, scope, categories, limit, min_salience,
                target_count, conn,
            )

    # --- Merge: deduplicate by ID ---
    # Multiple tiers may find the same memory. Keep only the first
    # occurrence (which comes from the higher-priority tier).
    deduped = _deduplicate_by_id(collected)
    logger.debug("Recall merged: %d unique memories from %d total", len(deduped), len(collected))

    # --- Sort by salience DESC (most important first) ---
    deduped.sort(key=lambda m: m.get("salience", 0), reverse=True)

    # --- Apply token budget if provided ---
    # Roughly estimate tokens at ~4 characters per token.
    # Trim from the bottom (lowest salience) until we fit.
    if token_budget and token_budget > 0:
        deduped = _fit_to_token_budget(deduped, token_budget)
        logger.debug("Recall: %d memories fit in %d token budget", len(deduped), token_budget)

    # --- Trim to the requested limit ---
    return deduped[:limit]


# ---------------------------------------------------------------------------
# Internal: run all 4 tiers against a DB connection
# ---------------------------------------------------------------------------


async def _run_tiers(
    query_text: str | None,
    scope: str | None,
    categories: list[str] | None,
    limit: int,
    min_salience: float | None,
    target_count: int,
    db: Any,
) -> list[dict]:
    """
    Execute the 4-tier cascade against a single DB connection.

    Each tier only runs if previous tiers haven't collected enough results.
    This saves unnecessary database round-trips.
    """
    collected: list[dict] = []

    # --- Tier 1: Graph-linked memories ---
    # Find memories connected via `relates` edges to entities mentioned
    # in the query text. Only runs if query is long enough to extract
    # meaningful entity names from it.
    if query_text and len(query_text) >= MIN_QUERY_LENGTH_FOR_GRAPH:
        tier1 = await _tier1_graph_linked(query_text, scope, db)
        collected.extend(tier1)
        logger.debug("Recall tier 1 (graph): %d memories", len(tier1))
    else:
        logger.debug("Recall tier 1 (graph): skipped — no query or too short")

    # --- Tier 2: BM25 + Vector search ---
    # Full-text search and semantic vector search combined.
    # Skip if Tier 1 already found plenty of results.
    if query_text and len(collected) < target_count * 1.5:
        tier2 = await _tier2_search(query_text, scope, limit, db)
        collected.extend(tier2)
        logger.debug("Recall tier 2 (BM25+vector): %d memories", len(tier2))

    # --- Tier 3: Category filter ---
    # If the caller specified categories, fetch memories matching those
    # categories sorted by salience. Skip if we already have enough.
    if categories and len(categories) > 0 and len(collected) < target_count * 1.5:
        tier3 = await _tier3_category_filter(
            categories, scope, min_salience or 0, limit, db,
        )
        collected.extend(tier3)
        logger.debug("Recall tier 3 (category): %d memories", len(tier3))

    # --- Tier 4: Recent fallback ---
    # If previous tiers didn't find enough, grab the most recent memories.
    # This ensures the agent always has SOMETHING to work with.
    if len(collected) < target_count:
        tier4 = await _tier4_recent_fallback(scope, db)
        collected.extend(tier4)
        logger.debug("Recall tier 4 (recent): %d memories", len(tier4))

    return collected


# ---------------------------------------------------------------------------
# Tier 1: Graph-linked memories
# ---------------------------------------------------------------------------


async def _tier1_graph_linked(
    query_text: str,
    scope: str | None,
    db: Any,
) -> list[dict]:
    """
    Find memories linked via graph edges to entities matching the query.

    Strategy:
    1. Extract candidate entity names from the query text (words >= 3 chars)
    2. Find entities whose name contains any of these words
    3. Traverse their `relates` edges to find connected memories

    This is the highest-quality tier because graph connections represent
    explicit, curated relationships (created by the agent or linker service).
    """
    # Extract likely entity names from the query text.
    # We look for words that are 3+ characters, stripping punctuation.
    # Keep Arabic characters (U+0600-U+06FF) + Latin + digits.
    words = query_text.split()
    cleaned_words = []
    for w in words:
        # Strip punctuation but keep Arabic, Latin, and digits
        cleaned = re.sub(r"[^a-zA-Z\u0600-\u06FF0-9]", "", w)
        if len(cleaned) >= MIN_WORD_LENGTH:
            cleaned_words.append(cleaned)

    # Cap the number of words to prevent huge queries
    cleaned_words = cleaned_words[:MAX_ENTITY_WORDS]

    if not cleaned_words:
        return []

    # Build the entity name matching condition.
    # For each word, check if the entity name contains it (case-insensitive).
    # We use parameterized queries with indexed params ($w0, $w1, etc.)
    match_conditions = []
    params: dict[str, Any] = {}
    for i, word in enumerate(cleaned_words):
        match_conditions.append(
            f"string::contains(string::lowercase(name), string::lowercase($w{i}))"
        )
        params[f"w{i}"] = word

    # Add scope filter — only return memories from current scope + global
    scope_filter = ""
    if scope and scope != "any":
        scope_filter = 'AND (scope = $scope OR scope = "global")'
        params["scope"] = scope

    # Two-step query:
    # Step 1: Find entities matching query words
    # Step 2: Traverse relates edges to find connected memories
    surql = f"""
    LET $entities = (
        SELECT id FROM entity
        WHERE {" OR ".join(match_conditions)}
        LIMIT 10
    );
    SELECT * FROM memory
    WHERE is_active = true
        AND (valid_until IS NONE OR valid_until > time::now())
        {scope_filter}
        AND id IN (
            SELECT VALUE <-relates<-.id FROM $entities
            WHERE <-relates<-.id IS NOT NONE
        )[0] ?? []
    ORDER BY salience DESC
    LIMIT 15;
    """

    result = await query(db, surql, params)

    # The multi-statement query returns a list of results.
    # The memories are in the second statement's result.
    if result is None:
        return []

    # query() normalizes the result — it may be a flat list of memories
    # or a nested list (one per statement). Handle both cases.
    if isinstance(result, list):
        # If nested (list of lists), take the last non-empty list
        # which is the SELECT * FROM memory result
        memories = []
        for item in result:
            if isinstance(item, dict) and item.get("id"):
                memories.append(item)
            elif isinstance(item, list):
                memories.extend(item)
        return memories

    return []


# ---------------------------------------------------------------------------
# Tier 2: BM25 + Vector search
# ---------------------------------------------------------------------------


async def _tier2_search(
    query_text: str,
    scope: str | None,
    limit: int,
    db: Any,
) -> list[dict]:
    """
    Combined BM25 full-text search and vector similarity search.

    BM25 uses SurrealDB's @@ operator for full-text matching.
    Vector search uses cosine similarity on embeddings (when available).

    Note: SurrealDB 3.0 has a known bug where search::score() returns 0.
    The @@ matching still WORKS — it finds matching documents — but the
    score isn't usable for ranking. We rely on salience for ordering instead.
    Vector cosine similarity scores DO work correctly.
    """
    results: list[dict] = []

    # Build scope filter clause
    scope_clause = ""
    params: dict[str, Any] = {"query": query_text, "limit": limit}
    if scope and scope != "any":
        scope_clause = 'AND ($scope = "any" OR scope = $scope OR scope = "global")'
        params["scope"] = scope

    # --- BM25 full-text search ---
    # Uses SurrealDB's @@ operator for full-text matching.
    # Falls back to salience ordering since search::score() is broken in v3.
    bm25_surql = f"""
    SELECT * FROM memory
    WHERE content @@ $query
        AND is_active = true
        {scope_clause}
        AND (valid_until IS NONE OR valid_until > time::now())
    ORDER BY salience DESC
    LIMIT $limit;
    """

    bm25_results = await query(db, bm25_surql, params)
    if bm25_results and isinstance(bm25_results, list):
        results.extend(bm25_results)
        logger.debug("Tier 2 BM25: %d results", len(bm25_results))

    # --- Vector similarity search ---
    # Only runs if we can generate a query embedding (Voyage API key configured).
    # This finds semantically similar memories even when exact words don't match.
    try:
        query_vec = await generate_query_embedding(query_text)
        if query_vec:
            vec_params: dict[str, Any] = {
                "query_vec": query_vec,
                "limit": limit,
            }
            vec_scope_clause = ""
            if scope and scope != "any":
                vec_scope_clause = 'AND ($scope = "any" OR scope = $scope OR scope = "global")'
                vec_params["scope"] = scope

            vec_surql = f"""
            SELECT *, vector::similarity::cosine(embedding, $query_vec) AS vec_score
            FROM memory
            WHERE is_active = true
                AND embedding IS NOT NONE
                {vec_scope_clause}
                AND (valid_until IS NONE OR valid_until > time::now())
            ORDER BY vec_score DESC
            LIMIT $limit;
            """

            vec_results = await query(db, vec_surql, vec_params)
            if vec_results and isinstance(vec_results, list):
                results.extend(vec_results)
                logger.debug("Tier 2 vector: %d results", len(vec_results))
    except Exception as e:
        # Vector search is non-fatal — BM25 results are enough
        logger.debug("Vector search failed (non-fatal): %s", e)

    return results


# ---------------------------------------------------------------------------
# Tier 3: Category filter
# ---------------------------------------------------------------------------


async def _tier3_category_filter(
    categories: list[str],
    scope: str | None,
    min_salience: float,
    limit: int,
    db: Any,
) -> list[dict]:
    """
    Fetch memories matching specific categories, sorted by salience.

    This tier is useful when the caller knows what TYPE of memory they
    want (e.g. "self" memories for the agent's self-model, or "preference"
    memories for user preferences). No text search — just category + salience.
    """
    params: dict[str, Any] = {
        "cats": categories,
        "min_salience": min_salience,
        "limit": limit,
    }

    scope_clause = ""
    if scope and scope != "any":
        scope_clause = 'AND ($scope = "any" OR scope = $scope OR scope = "global")'
        params["scope"] = scope

    surql = f"""
    SELECT * FROM memory
    WHERE category IN $cats
        AND is_active = true
        AND salience >= $min_salience
        {scope_clause}
        AND (valid_until IS NONE OR valid_until > time::now())
    ORDER BY salience DESC
    LIMIT $limit;
    """

    result = await query(db, surql, params)
    if result and isinstance(result, list):
        return result
    return []


# ---------------------------------------------------------------------------
# Tier 4: Recent fallback
# ---------------------------------------------------------------------------


async def _tier4_recent_fallback(
    scope: str | None,
    db: Any,
) -> list[dict]:
    """
    Get the most recently created active memories as a safety net.

    This tier runs only when the other tiers didn't find enough results.
    It ensures the agent always has SOME memories to work with, even when
    the query doesn't match anything specific.
    """
    params: dict[str, Any] = {"limit": RECENT_FALLBACK_LIMIT}

    scope_clause = ""
    if scope and scope != "any":
        scope_clause = 'AND ($scope = "any" OR scope = $scope OR scope = "global")'
        params["scope"] = scope

    surql = f"""
    SELECT * FROM memory
    WHERE is_active = true
        {scope_clause}
        AND (valid_until IS NONE OR valid_until > time::now())
    ORDER BY created_at DESC
    LIMIT $limit;
    """

    result = await query(db, surql, params)
    if result and isinstance(result, list):
        return result
    return []


# ---------------------------------------------------------------------------
# Deduplication helper
# ---------------------------------------------------------------------------


def _deduplicate_by_id(memories: list[dict]) -> list[dict]:
    """
    Remove duplicate memories by ID, keeping the first occurrence.

    When multiple tiers find the same memory, the first occurrence
    (from the higher-priority tier) is kept. This preserves the
    priority ordering: graph > BM25/vector > category > recent.
    """
    seen: set[str] = set()
    unique: list[dict] = []

    for mem in memories:
        mem_id = str(mem.get("id", ""))
        if mem_id and mem_id not in seen:
            seen.add(mem_id)
            unique.append(mem)

    return unique


# ---------------------------------------------------------------------------
# Token budget helper
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """
    Estimate token count using the ~4 characters per token heuristic.

    This is a rough estimate — actual tokenization depends on the model.
    But it's fast and good enough for budget enforcement.
    """
    return max(1, len(text) // 4)


def _fit_to_token_budget(memories: list[dict], budget: int) -> list[dict]:
    """
    Trim the memory list to fit within a token budget.

    Keeps adding memories (from highest salience to lowest) until
    the total estimated tokens would exceed the budget. Memories
    that don't fit are dropped.

    This ensures the most important memories always make the cut.
    """
    fitted: list[dict] = []
    used_tokens = 0

    for mem in memories:
        content = mem.get("content", "")
        tokens = _estimate_tokens(content)

        if used_tokens + tokens > budget:
            break  # No more room — stop adding

        fitted.append(mem)
        used_tokens += tokens

    return fitted


# ---------------------------------------------------------------------------
# Age formatting helper
# ---------------------------------------------------------------------------


def _format_age(created_at: Any) -> str:
    """
    Format a created_at timestamp into a human-readable age string.

    Examples: "2h ago", "3d ago", "1mo ago", "just now"

    Handles datetime objects, ISO strings, and None gracefully.
    """
    if created_at is None:
        return "unknown age"

    try:
        # If it's already a datetime, use it directly
        if isinstance(created_at, datetime):
            dt = created_at
        else:
            # Try parsing as ISO string (SurrealDB returns ISO format)
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))

        # Make sure we compare timezone-aware datetimes
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        diff = now - dt
        seconds = diff.total_seconds()

        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m ago"
        elif seconds < 86400:
            return f"{int(seconds / 3600)}h ago"
        elif seconds < 2592000:  # ~30 days
            return f"{int(seconds / 86400)}d ago"
        else:
            return f"{int(seconds / 2592000)}mo ago"

    except Exception:
        return "unknown age"


# ---------------------------------------------------------------------------
# Context assembly — builds the full context block for AI agents
# ---------------------------------------------------------------------------


async def assemble_context(
    session_key: str,
    db: Any = None,
) -> str:
    """
    Build the full context block that gets injected into the AI agent's prompt.

    This is the main "read" function that qmemory_bootstrap calls. It:
    1. Parses the session key to determine channel, topic, and scope
    2. Loads self-model memories (agent's self-knowledge) — injected FIRST
    3. Loads all other memories, sorted by salience
    4. Formats everything into a structured text block

    The resulting string is injected as a systemPromptAddition so the agent
    "remembers" facts from previous sessions.

    Args:
        session_key: The current session key (e.g. "telegram:group:123:topic:7").
        db:          Optional SurrealDB connection. If None, creates a fresh one.

    Returns:
        A formatted string ready for injection into the agent's system prompt.
        Returns a minimal header if no memories exist.
    """
    if db is not None:
        return await _assemble(session_key, db)
    else:
        async with get_db() as conn:
            return await _assemble(session_key, conn)


async def _assemble(session_key: str, db: Any) -> str:
    """
    Internal assembly logic — called with an active DB connection.
    """
    # --- Step 1: Parse session key ---
    parsed = parse_session_key(session_key)
    scope = parsed["scope"]

    # --- Step 2: Load self-model memories (injected FIRST) ---
    # Self-knowledge is the agent's "soul" — what it knows about itself,
    # its communication patterns, what works and what to avoid.
    self_memories = await _tier3_category_filter(
        categories=["self"],
        scope=None,  # Self-knowledge is always global
        min_salience=0,
        limit=10,
        db=db,
    )

    # --- Step 3: Load all other memories for the current scope ---
    # We recall both scope-specific AND global memories.
    # High-salience memories (critical rules, preferences) are always included.
    memories = await recall(
        scope=scope if scope != "global" else None,
        limit=50,
        db=db,
    )

    # Remove self memories from the general list to avoid duplication
    # (they're already in self_memories and will be formatted separately)
    self_ids = {str(m.get("id", "")) for m in self_memories}
    other_memories = [m for m in memories if str(m.get("id", "")) not in self_ids]

    # --- Step 4: Format everything ---
    lines: list[str] = []

    # Part 1: Self-model (injected FIRST — most important)
    if self_memories:
        lines.append("## Agent Self-Model")
        for m in self_memories:
            lines.append(f"- {m.get('content', '')}")

    # Part 2: Session header
    channel_label = parsed["channel"] or "direct"
    topic_label = f"/topic:{parsed['topic_id']}" if parsed.get("topic_id") else ""
    scope_label = f" | scope: {scope}" if scope != "global" else ""
    mem_count = len(self_memories) + len(other_memories)
    lines.append(f"\n## Session: {channel_label}/{parsed['chat_type']}{topic_label}{scope_label} | {mem_count} memories recalled")

    # Part 3: Memories grouped by category
    for cat in MEMORY_CATEGORIES:
        # Skip "self" — already formatted above
        if cat == "self":
            continue

        # Filter memories for this category
        cat_mems = [m for m in other_memories if m.get("category") == cat]

        if cat_mems:
            lines.append(f"\n### {cat.title()}")
            for m in cat_mems:
                # Format each memory with its ID, scope, age, and content
                mem_id = m.get("id", "???")
                mem_scope = m.get("scope", "global")
                age = _format_age(m.get("created_at"))
                content = m.get("content", "")
                lines.append(f"- [{mem_id}] [{mem_scope}] ({age}) {content}")

    return "\n".join(lines)
