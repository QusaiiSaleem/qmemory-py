"""
NanoBot tool: qmemory_correct

Thin wrapper around qmemory.core.correct.correct_memory.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to fix, soft-delete, update
metadata, or unlink memories.

Soft-delete only — memories are NEVER hard-deleted. is_active=false
preserves the full audit trail.

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


class QmemoryCorrectTool(Tool):
    """Fix or delete a memory. Preserves full audit trail via soft-delete.

    Calls correct_memory() which handles 4 action types:
    correct, delete, update, unlink.
    """

    # Plain class attributes — matches NanoBot's built-in tool pattern.
    name = "qmemory_correct"

    description = (
        "Fix or delete a memory. Preserves full audit trail via soft-delete. "
        "We NEVER hard-delete memories — soft-delete only (is_active = false). "
        "The 'correct' action creates a version chain so you can trace "
        "a fact's full history."
    )

    parameters = {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": (
                    "Full record ID, e.g. 'memory:mem1710864000000abc'. "
                    "Get this from qmemory_search results."
                ),
            },
            "action": {
                "type": "string",
                "description": (
                    "What to do: "
                    "correct (replace content, creates new version, soft-deletes old, requires new_content), "
                    "delete (soft-delete only, sets is_active=false), "
                    "update (change metadata without new version, requires updates dict), "
                    "unlink (remove a relates edge, requires edge_id)"
                ),
            },
            "new_content": {
                "type": "string",
                "description": (
                    "The corrected fact text. Required when action='correct'."
                ),
            },
            "updates": {
                "type": "object",
                "description": (
                    "Metadata fields to change. Required when action='update'. "
                    "Allowed keys: salience, scope, valid_until, category, confidence. "
                    "Example: {\"salience\": 0.9, \"scope\": \"project:qmemory\"}"
                ),
            },
            "edge_id": {
                "type": "string",
                "description": (
                    "The relates edge ID to delete. Required when action='unlink'."
                ),
            },
            "reason": {
                "type": "string",
                "description": "Optional note explaining why this change was made.",
            },
        },
        "required": ["memory_id", "action"],  # The target and action are always needed
    }

    async def execute(
        self,
        memory_id: str,
        action: str,
        new_content: str | None = None,
        updates: dict | None = None,
        edge_id: str | None = None,
        reason: str | None = None,
        **kwargs,
    ) -> str:
        """Execute the correction and return the result as a JSON string.

        Returns JSON with ok (bool) and details about what changed.
        """
        from qmemory.core.correct import correct_memory

        result = await correct_memory(
            memory_id=memory_id,
            action=action,
            new_content=new_content,
            updates=updates,
            edge_id=edge_id,
            reason=reason,
        )
        return json.dumps(result, default=str, ensure_ascii=False)
