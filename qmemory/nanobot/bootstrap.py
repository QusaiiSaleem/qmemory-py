"""
NanoBot tool: qmemory_bootstrap

Thin wrapper around qmemory.core.recall.assemble_context.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to load full memory context
at the start of a conversation.

The nanobot-ai import is guarded — this module is safely importable even
when nanobot-ai is not installed. It will only fail at runtime if actually
invoked without the SDK present.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Guard the nanobot-ai import.
# If the SDK is installed (pip install qmemory[nanobot]), this works fine.
# If it's NOT installed (e.g. running plain MCP mode), the module still loads
# without errors — it just won't be usable as a NanoBot tool.
# ---------------------------------------------------------------------------
try:
    from nanobot.agent.tools.base import Tool  # type: ignore[import-untyped]

    _NANOBOT_AVAILABLE = True
except ImportError:
    # Fallback: create a no-op base class so the class definition below still
    # works syntactically. Tools registered via entry points won't be loaded
    # unless nanobot-ai is installed, so this path is safe.
    class Tool:  # type: ignore[no-redef]
        pass

    _NANOBOT_AVAILABLE = False


class QmemoryBootstrapTool(Tool):
    """Load full memory context at the start of a conversation.

    Calls assemble_context() which runs the 4-tier recall pipeline and
    returns a formatted text block ready for injection into the agent's
    context window.
    """

    # The tool name matches the MCP tool name for consistency across both
    # integration paths (MCP for Claude Code, NanoBot for Rakeezah).

    @property
    def name(self) -> str:
        return "qmemory_bootstrap"

    @property
    def description(self) -> str:
        return (
            "Load your full memory context for this session. "
            "Call this at the START of every conversation to remember who you are "
            "and what you know. Returns your self-model, cross-session memories "
            "grouped by category, graph map, and session info."
        )

    @property
    def parameters(self) -> dict:
        """JSON Schema that describes the input parameters this tool accepts.

        Matches the MCP tool signature exactly so both wrappers behave the same.
        """
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": (
                        "Identifies this session context. Use the channel/topic "
                        "name if available (e.g. 'telegram/topic:7'), otherwise "
                        "leave as 'default'."
                    ),
                    "default": "default",
                },
            },
            "required": [],  # session_key is optional — defaults to "default"
        }

    async def execute(self, **kwargs) -> str:
        """Execute the bootstrap: load and return the full memory context.

        Returns a formatted string (not JSON) ready for injection into
        the agent's system prompt or first user turn.
        """
        # Extract kwargs — NanoBot's Tool base class passes parameters as
        # keyword arguments to execute(), so we pull them out here.
        session_key = kwargs.get("session_key", "default")

        # Import here (not at module top) to keep the module loadable even
        # without SurrealDB running — only fails when actually called.
        from qmemory.core.recall import assemble_context

        return await assemble_context(session_key)
