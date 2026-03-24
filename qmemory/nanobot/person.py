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
    from nanobot.tools import BaseTool  # type: ignore[import-untyped]

    _NANOBOT_AVAILABLE = True
except ImportError:
    class BaseTool:  # type: ignore[no-redef]
        pass

    _NANOBOT_AVAILABLE = False


class QmemoryPersonTool(BaseTool):
    """Create or find a person entity with linked identities across systems.

    Calls create_person() which either creates a new entity node or returns
    the existing one if a person with this name already exists (no duplicates).
    Contact identities (Telegram, WhatsApp, email, etc.) are linked via
    has_identity edges.
    """

    name = "qmemory_person"

    description = (
        "Create or find a person entity with linked identities across systems. "
        "A person can have multiple contact identities — Telegram, WhatsApp, "
        "email, etc. — all linked via has_identity edges. If a person with this "
        "name already exists, returns the existing record (no duplicate created)."
    )

    parameters = {
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

    async def run(
        self,
        name: str,
        aliases: list[str] | None = None,
        contacts: list[dict] | None = None,
    ) -> str:
        """Execute the person create/find and return the result as a JSON string.

        Returns JSON with entity_id, contact_ids, links_created, and
        action ("created" or "found").
        """
        from qmemory.core.person import create_person

        result = await create_person(
            name=name,
            aliases=aliases,
            contacts=contacts,
        )
        return json.dumps(result, default=str, ensure_ascii=False)
