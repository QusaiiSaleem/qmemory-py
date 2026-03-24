"""
Voyage AI Embedding Generation

This module provides two async functions for generating vector embeddings:
- generate_embedding()       → for storing memory content (input_type="document")
- generate_query_embedding() → for search queries (input_type="query")

Why two different input types?
Voyage AI recommends using "document" when you're embedding text for storage,
and "query" when you're embedding a search term. They're optimised differently
under the hood — using "query" for both would hurt recall quality.

Why LRU cache on queries?
Users tend to ask similar questions repeatedly (e.g. "budget", "team").
Each Voyage API call costs ~200ms. Caching the last 50 unique query strings
makes repeated searches instant at effectively zero cost.

Why lazy-init the client?
The Voyage client opens an HTTP connection pool when created. We only want
that to happen if the API key exists and someone actually calls these functions.
Importing this module at startup should be free.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import voyageai

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level client (lazy-initialised on first use)
# ---------------------------------------------------------------------------

# Holds the singleton AsyncClient once it's been created.
# None means "not yet initialised" — we create it on first call.
_client: voyageai.AsyncClient | None = None

# The Voyage model to use for all embeddings.
# voyage-3-large produces 1024-dimensional vectors with strong multilingual support.
_MODEL = "voyage-3-large"

# ---------------------------------------------------------------------------
# LRU cache for query embeddings
# ---------------------------------------------------------------------------

# We use an OrderedDict to implement a simple LRU (Least Recently Used) cache.
# OrderedDict remembers insertion order, so we can move accessed items to the
# end and evict the oldest item from the front when we exceed the limit.
_query_cache: OrderedDict[str, list[float]] = OrderedDict()

# Maximum number of unique query strings to cache.
# 50 entries × ~4KB per 1024-dim float vector ≈ ~200KB RAM — negligible.
_CACHE_MAX = 50


def _get_client() -> voyageai.AsyncClient | None:
    """
    Return the shared Voyage AsyncClient, creating it on first call.

    Returns None (without raising) if no VOYAGE_API_KEY is configured.
    This allows the rest of the system to degrade gracefully — embeddings
    simply won't be generated, but everything else keeps working.
    """
    global _client

    # Already initialised — just return it
    if _client is not None:
        return _client

    # Read the API key from settings (loaded from environment / .env file)
    from qmemory.config import get_settings

    key = get_settings().voyage_api_key

    if not key:
        # No key configured — log once at debug level and bail out.
        # We use debug (not warning) because "no key" is a valid dev-mode state.
        logger.debug("VOYAGE_API_KEY not set — embeddings disabled")
        return None

    # Create the async client. It opens an HTTP connection pool here,
    # which is why we cache it at module level rather than recreating per call.
    _client = voyageai.AsyncClient(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_embedding(text: str) -> list[float] | None:
    """
    Generate a document embedding for the given text.

    Use this when storing memory content — Voyage optimises "document"
    embeddings for retrieval (they're the "answers" side of search).

    Args:
        text: The text to embed. Whitespace-only strings are treated as empty.

    Returns:
        A list of floats (1024 dimensions for voyage-3-large), or None if:
        - text is empty / whitespace-only
        - VOYAGE_API_KEY is not configured
        - The Voyage API call fails for any reason
    """
    # Guard: don't send empty strings to the API
    if not text or not text.strip():
        return None

    client = _get_client()
    if client is None:
        return None

    try:
        # embed() accepts a list of texts; we always send exactly one.
        # input_type="document" tells Voyage this text is being stored, not queried.
        result = await client.embed([text], model=_MODEL, input_type="document")
        return result.embeddings[0]
    except Exception as e:
        # Any Voyage error (rate limit, network issue, bad input) → return None.
        # We log a warning so the problem is visible without crashing the caller.
        logger.warning(f"Embedding generation failed: {e}")
        return None


async def generate_query_embedding(text: str) -> list[float] | None:
    """
    Generate a query embedding for semantic search.

    Use this when embedding a user's search query — Voyage optimises "query"
    embeddings for matching against stored document embeddings.

    Results are cached (LRU, 50 entries) because:
    - Users repeat similar queries (e.g. "budget", "team roster")
    - Each API call costs ~200ms
    - Vectors are deterministic — same text → same vector every time

    Args:
        text: The search query to embed.

    Returns:
        A list of floats (1024 dimensions), or None on any failure.
    """
    # Guard: don't embed empty strings
    if not text or not text.strip():
        return None

    # --- Check cache first ---
    if text in _query_cache:
        # Move to end = "most recently used" — prevents premature eviction
        _query_cache.move_to_end(text)
        return _query_cache[text]

    client = _get_client()
    if client is None:
        return None

    try:
        # input_type="query" tells Voyage this is the search-side embedding
        result = await client.embed([text], model=_MODEL, input_type="query")
        vec = result.embeddings[0]

        # --- Store in cache ---
        _query_cache[text] = vec

        # Evict the oldest entry if we've exceeded the limit.
        # last=False means "remove from the front" (least recently used).
        if len(_query_cache) > _CACHE_MAX:
            _query_cache.popitem(last=False)

        return vec
    except Exception as e:
        logger.warning(f"Query embedding failed: {e}")
        return None
