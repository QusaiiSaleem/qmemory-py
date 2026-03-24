"""
NanoBot tool: qmemory_search

Thin wrapper around qmemory.core.search.search_memories.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to search cross-session memory
using BM25 + vector similarity, with optional category and scope filters.

The nanobot-ai import is guarded — this module is safely importable even
when nanobot-ai is not installed.
"""

from __future__ import annotations

import json

try:
    from nanobot.agent.tools.base import Tool  # type: ignore[import-untyped]

    _NANOBOT_AVAILABLE = True
except ImportError:
    class Tool:  # type: ignore[no-redef]
        pass

    _NANOBOT_AVAILABLE = False


class QmemorySearchTool(Tool):
    """Search cross-session memory by meaning, category, or scope.

    Calls search_memories() which runs BM25 + vector similarity search,
    then enriches results with graph connection hints.
    """

    @property
    def name(self) -> str:
        return "qmemory_search"

    @property
    def description(self) -> str:
        return (
            "Search cross-session memory by meaning, category, or scope. "
            "Returns memories from ALL past conversations with graph connection hints "
            "and an exploration nudge. Use this to find what you know about a topic."
        )

    @property
    def parameters(self) -> dict:
        """JSON Schema mirrors the MCP tool parameters exactly."""
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Free-text search query (BM25 + vector similarity). "
                        "Leave empty to get recent memories without text search."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Filter to one category: "
                        "self, style, preference, context, decision, "
                        "idea, feedback, domain"
                    ),
                },
                "scope": {
                    "type": "string",
                    "description": (
                        "Filter visibility: global, project:xxx, topic:xxx"
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 10, max 50).",
                    "default": 10,
                },
                "include_tool_calls": {
                    "type": "boolean",
                    "description": (
                        "Also search past tool call history (default False)."
                    ),
                    "default": False,
                },
            },
            "required": [],  # All parameters are optional
        }

    async def execute(self, **kwargs) -> str:
        """Execute the search and return results as a JSON string.

        Note: the core function uses 'query_text' as its parameter name,
        not 'query' — we map here so both MCP and NanoBot use the same
        public name ('query') while the core stays unchanged.

        Returns JSON with {"results": [...], "_nudge": "..."}.
        """
        # Extract kwargs — NanoBot's Tool base class passes parameters as
        # keyword arguments to execute().
        query = kwargs.get("query")
        category = kwargs.get("category")
        scope = kwargs.get("scope")
        limit = kwargs.get("limit", 10)
        include_tool_calls = kwargs.get("include_tool_calls", False)

        from qmemory.core.search import search_memories

        results = await search_memories(
            query_text=query,       # core uses query_text, not query
            category=category,
            scope=scope,
            limit=limit,
            include_tool_calls=include_tool_calls,
        )
        return json.dumps(results, default=str, ensure_ascii=False)
