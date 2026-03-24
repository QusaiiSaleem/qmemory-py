"""
NanoBot tool: qmemory_person

Thin wrapper around qmemory.core.person.create_person.
Used by Rakeezah (FastAPI + nanobot-ai SDK) to create or find person entities
with multiple contact identities linked across different systems.

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


class QmemoryPersonTool(Tool):
    """Create or find a person entity with linked identities across systems.

    Calls create_person() which either creates a new entity node or returns
    the existing one if a person with this name already exists (no duplicates).
    Contact identities (Telegram, WhatsApp, email, etc.) are linked via
    has_identity edges.
    """

    @property
    def name(self) -> str:
        return "qmemory_person"

    @property
    def description(self) -> str:
        return (
            "Create or find a person entity with linked identities across systems. "
            "A person can have multiple contact identities — Telegram, WhatsApp, "
            "email, etc. — all linked via has_identity edges. If a person with this "
            "name already exists, returns the existing record (no duplicate created)."
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The person's display name. Example: 'Ahmed Al-Rashid'"
                    ),
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional alternative names or nicknames. "
                        "Example: ['Ahmed', 'Abu Omar']"
                    ),
                },
                "contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "system": {
                                "type": "string",
                                "description": (
                                    "The platform: telegram, whatsapp, email, smartsheet"
                                ),
                            },
                            "handle": {
                                "type": "string",
                                "description": (
                                    "The identifier in that system (username, email, ID)"
                                ),
                            },
                        },
                        "required": ["system", "handle"],
                    },
                    "description": (
                        "Optional list of contact identities. Each entry needs 'system' and 'handle'. "
                        "Example: [{\"system\": \"telegram\", \"handle\": \"@ahmed_rashid\"}, "
                        "{\"system\": \"email\", \"handle\": \"ahmed@example.com\"}]"
                    ),
                },
            },
            "required": ["name"],  # Only the display name is required
        }

    async def execute(self, **kwargs) -> str:
        """Execute the person create/find and return the result as a JSON string.

        Returns JSON with entity_id, contact_ids, links_created, and
        action ("created" or "found").
        """
        # Extract kwargs — NanoBot's Tool base class passes parameters as
        # keyword arguments to execute().
        name = kwargs.get("name", "")
        aliases = kwargs.get("aliases")
        contacts = kwargs.get("contacts")

        from qmemory.core.person import create_person

        result = await create_person(
            name=name,
            aliases=aliases,
            contacts=contacts,
        )
        return json.dumps(result, default=str, ensure_ascii=False)
