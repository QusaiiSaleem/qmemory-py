"""
LLM Provider Base Protocol

Defines the interface that all LLM providers must implement.

Using Python's Protocol class means we don't need inheritance — any class
that has a `complete(prompt, schema)` method automatically satisfies the
protocol. This is called "structural subtyping" or "duck typing" with type hints.

Why a Protocol instead of an abstract base class (ABC)?
- ABC requires explicit `class ClaudeProvider(LLMProvider)` inheritance
- Protocol just checks shape — if it has the right method, it fits
- More flexible: easier to swap providers or mock in tests
"""

from typing import Any, Protocol


class LLMProvider(Protocol):
    """
    Interface for LLM providers used by Qmemory's background tasks.

    Any class with a `complete(prompt, schema)` async method satisfies
    this protocol — no explicit inheritance required.

    Used by:
    - dedup.py   — check if a new memory duplicates an existing one
    - linker     — find relationships between memories
    - reflect    — extract patterns and compress old facts
    """

    async def complete(self, prompt: str, schema: dict | None = None) -> str | dict:
        """
        Send a prompt to the LLM and return the response.

        Args:
            prompt: The full prompt text to send.
            schema: Optional JSON Schema dict for structured output.
                    If provided, the LLM MUST return a dict matching this schema.
                    If None, the LLM returns a plain string.

        Returns:
            str  — when schema is None (free-form text response)
            dict — when schema is provided (structured output matching the schema)
        """
        ...
