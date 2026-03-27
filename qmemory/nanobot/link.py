"""
NanoBot tool: qmemory_link

Thin wrapper around qmemory.core.link.link_nodes.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to create relationship edges
between any two nodes in the memory graph.

This is how a flat list of facts becomes a connected knowledge graph.

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


class QmemoryLinkTool(Tool):
    """Create a relationship edge between any two nodes in the memory graph.

    Calls link_nodes() which creates a 'relates' edge with a typed relationship.
    The relationship_type can be any string — there is no fixed list.
    """

    # Plain class attributes — matches NanoBot's built-in tool pattern.
    name = "qmemory_link"

    description = (
        "Create a relationship edge between any two nodes in the memory graph. "
        "This is what turns a flat list of facts into a connected knowledge graph. "
        "The relationship type can be ANYTHING meaningful — choose a type that "
        "describes WHY these two things are connected."
    )

    parameters = {
        "type": "object",
        "properties": {
            "from_id": {
                "type": "string",
                "description": (
                    "Source node ID. Examples: "
                    "'memory:mem1710864000000abc', "
                    "'entity:ent1710864000000xyz', "
                    "'session:ses1710864000000def'"
                ),
            },
            "to_id": {
                "type": "string",
                "description": "Target node ID. Can be a different table type.",
            },
            "relationship_type": {
                "type": "string",
                "description": (
                    "Any string describing the connection. Examples: "
                    "supports, contradicts, caused_by, depends_on, "
                    "belongs_to_topic, has_identity, related_to, "
                    "blocks, inspired_by, summarizes, expands_on"
                ),
            },
            "reason": {
                "type": "string",
                "description": "Optional note explaining why this connection exists.",
            },
            "confidence": {
                "type": "number",
                "description": "How confident are you in this connection? 0.0-1.0.",
            },
        },
        "required": ["from_id", "to_id", "relationship_type"],
    }

    async def execute(
        self,
        from_id: str,
        to_id: str,
        relationship_type: str,
        reason: str | None = None,
        confidence: float | None = None,
        **kwargs,
    ) -> str:
        """Execute the link creation and return the result as a JSON string.

        Returns JSON with edge_id and an exploration nudge, or null if either
        node doesn't exist.
        """
        from qmemory.core.link import link_nodes

        result = await link_nodes(
            from_id=from_id,
            to_id=to_id,
            relationship_type=relationship_type,
            reason=reason,
            confidence=confidence,
        )
        return json.dumps(result, default=str, ensure_ascii=False)
