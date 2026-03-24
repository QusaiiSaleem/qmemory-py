"""
NanoBot tool: qmemory_save

Thin wrapper around qmemory.core.save.save_memory.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to persist new facts into the
memory graph with automatic deduplication.

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


class QmemorySaveTool(Tool):
    """Save a fact to cross-session memory with evidence tracking.

    Calls save_memory() which runs deduplication automatically — if a similar
    memory exists it will UPDATE or NOOP instead of creating a duplicate.
    """

    @property
    def name(self) -> str:
        return "qmemory_save"

    @property
    def description(self) -> str:
        return (
            "Save a fact to cross-session memory with evidence tracking. "
            "Runs deduplication automatically — if a similar memory exists it will "
            "UPDATE or NOOP instead of creating a duplicate. Returns the action taken."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The fact to remember. One clear statement. "
                        "Example: 'Qusai prefers concise bullet points over paragraphs'"
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "What type of fact this is: "
                        "self (what the agent knows about itself), "
                        "style (communication preferences), "
                        "preference (general user preferences), "
                        "context (facts about projects/orgs/situations), "
                        "decision (past decisions made with rationale), "
                        "idea (future plans or proposals), "
                        "feedback (user corrections and error reports), "
                        "domain (sector/domain knowledge)"
                    ),
                },
                "salience": {
                    "type": "number",
                    "description": (
                        "Importance 0.0-1.0. High-salience memories are recalled first. "
                        "0.9+ = critical, 0.7 = important, 0.5 = normal, 0.3 = low"
                    ),
                    "default": 0.5,
                },
                "scope": {
                    "type": "string",
                    "description": "Who can see this: global | project:xxx | topic:xxx",
                    "default": "global",
                },
                "confidence": {
                    "type": "number",
                    "description": (
                        "How certain are you? 0.0-1.0. Use < 0.5 for hypotheses."
                    ),
                    "default": 0.8,
                },
                "source_person": {
                    "type": "string",
                    "description": (
                        "Who said this? Pass entity ID (e.g. 'ent1234abc') if known."
                    ),
                },
                "evidence_type": {
                    "type": "string",
                    "description": (
                        "How was this learned? "
                        "observed (you witnessed it directly), "
                        "reported (someone told you), "
                        "inferred (you deduced it), "
                        "self (the agent learned this about itself)"
                    ),
                    "default": "observed",
                },
                "context_mood": {
                    "type": "string",
                    "description": (
                        "Situation when this was learned: "
                        "calm_decision | heated_discussion | brainstorm | "
                        "correction | casual | urgent"
                    ),
                },
            },
            "required": ["content", "category"],  # These two are always needed
        }

    async def execute(self, **kwargs) -> str:
        """Execute the save and return the result as a JSON string.

        Returns JSON with action (ADD/UPDATE/NOOP), memory_id, and a nudge
        suggesting which nearby memories to link with qmemory_link.
        """
        # Extract kwargs — NanoBot's Tool base class passes parameters as
        # keyword arguments to execute().
        content = kwargs.get("content", "")
        category = kwargs.get("category", "")
        salience = kwargs.get("salience", 0.5)
        scope = kwargs.get("scope", "global")
        confidence = kwargs.get("confidence", 0.8)
        source_person = kwargs.get("source_person")
        evidence_type = kwargs.get("evidence_type", "observed")
        context_mood = kwargs.get("context_mood")

        from qmemory.core.save import save_memory

        result = await save_memory(
            content=content,
            category=category,
            salience=salience,
            scope=scope,
            confidence=confidence,
            source_person=source_person,
            evidence_type=evidence_type,
            context_mood=context_mood,
        )
        return json.dumps(result, default=str, ensure_ascii=False)
