"""
Voyage AI Embedding Generation (direct HTTP — no SDK dependency)

This module provides two async functions for generating vector embeddings:
- generate_embedding()       → for storing memory content (input_type="document")
- generate_query_embedding() → for search queries (input_type="query")

Why two different input types?
Voyage AI recommends using "document" when you're embedding text for storage,
and "query" when you're embedding a search term. They're optimised differently
under the hood — using "query" for both would hurt recall quality.

Why direct HTTP instead of the voyageai SDK?
The voyageai SDK v0.3.7 uses Pydantic v1 internally, which crashes on Python 3.14+.
The API is a single POST endpoint — httpx does the same thing with zero extra deps.

Why LRU cache on queries?
Users tend to ask similar questions repeatedly (e.g. "budget", "team").
Each Voyage API call costs ~200ms. Caching the last 50 unique query strings
makes repeated searches instant at effectively zero cost.
"""

from __future__ import annotations

import logging
from collections import OrderedDict

import httpx

from qmemory.config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Voyage AI embedding API endpoint
_API_URL = "https://api.voyageai.com/v1/embeddings"

# The Voyage model to use for all embeddings.
# voyage-3-large produces 1024-dimensional vectors with strong multilingual support.
_MODEL = "voyage-3-large"

# ---------------------------------------------------------------------------
# Shared HTTP client (lazy-initialised on first use)
# ---------------------------------------------------------------------------

# Holds the singleton httpx.AsyncClient once it's been created.
_http_client: httpx.AsyncClient | None = None

# ---------------------------------------------------------------------------
# LRU cache for query embeddings
# ---------------------------------------------------------------------------

_query_cache: OrderedDict[str, list[float]] = OrderedDict()
_CACHE_MAX = 50


def _get_http_client() -> httpx.AsyncClient | None:
    """
    Return a shared httpx.AsyncClient with the Voyage API key header.

    Returns None if VOYAGE_API_KEY is not configured — embeddings
    are silently disabled so the rest of the system keeps working.
    """
    global _http_client

    if _http_client is not None:
        return _http_client

    key = get_settings().voyage_api_key
    if not key:
        logger.debug("VOYAGE_API_KEY not set — embeddings disabled")
        return None

    # Create a persistent client with auth header and reasonable timeout
    _http_client = httpx.AsyncClient(
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    return _http_client


async def _call_voyage(text: str, input_type: str) -> list[float] | None:
    """
    Call the Voyage AI embedding API directly via HTTP.

    Args:
        text:       The text to embed.
        input_type: "document" for storage, "query" for search.

    Returns:
        A list of 1024 floats, or None on any failure.
    """
    client = _get_http_client()
    if client is None:
        return None

    try:
        response = await client.post(
            _API_URL,
            json={
                "input": [text],
                "model": _MODEL,
                "input_type": input_type,
            },
        )
        response.raise_for_status()

        data = response.json()
        # Response format: {"data": [{"embedding": [...], "index": 0}], ...}
        return data["data"][0]["embedding"]

    except Exception as e:
        logger.warning("Voyage API call failed (%s): %s", input_type, e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_embedding(text: str) -> list[float] | None:
    """
    Generate a document embedding for the given text.

    Use this when storing memory content — Voyage optimises "document"
    embeddings for retrieval (they're the "answers" side of search).

    Returns a list of 1024 floats, or None if text is empty, key is
    missing, or the API call fails.
    """
    if not text or not text.strip():
        return None
    return await _call_voyage(text, "document")


async def generate_query_embedding(text: str) -> list[float] | None:
    """
    Generate a query embedding for semantic search.

    Results are cached (LRU, 50 entries) because users repeat similar
    queries and each API call costs ~200ms.

    Returns a list of 1024 floats, or None on any failure.
    """
    if not text or not text.strip():
        return None

    # Check cache first
    if text in _query_cache:
        _query_cache.move_to_end(text)
        return _query_cache[text]

    vec = await _call_voyage(text, "query")
    if vec is None:
        return None

    # Store in cache, evict oldest if full
    _query_cache[text] = vec
    if len(_query_cache) > _CACHE_MAX:
        _query_cache.popitem(last=False)

    return vec
