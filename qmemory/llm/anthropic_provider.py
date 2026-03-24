"""
Anthropic Claude LLM Provider

Wraps the Anthropic Python SDK to implement the LLMProvider protocol.

Two modes:
  1. Plain text  — no schema → returns response.content[0].text
  2. Structured  — schema provided → uses tool_use pattern to get a dict

Why tool_use for structured output?
  Claude's "structured outputs" feature works via forced tool calling.
  We define a fake tool named "respond" whose input_schema is our desired JSON
  Schema. By setting tool_choice={"type": "tool", "name": "respond"}, Claude
  MUST call this tool, which forces it to return JSON matching our schema.
  This is more reliable than asking Claude to "return valid JSON" in the prompt.

Graceful degradation:
  If the API call fails for any reason (rate limit, network error, invalid key),
  we catch the exception, log a warning, and return "" or {} depending on mode.
  This is intentional — dedup's rule-based fallback handles the "" case.
"""

import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)


class ClaudeProvider:
    """
    LLM provider backed by Anthropic's Claude models.

    Reads the ANTHROPIC_API_KEY environment variable automatically
    (the anthropic SDK looks for it by default — no need to pass it here).

    Args:
        model: The Claude model ID to use. Defaults to Haiku (cheapest/fastest).
               Use "claude-sonnet-4-5-20250514" for higher quality tasks.
    """

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        # AsyncAnthropic picks up ANTHROPIC_API_KEY from the environment.
        # We don't pass the key explicitly to avoid hardcoding secrets.
        self._client = anthropic.AsyncAnthropic()
        self.model = model

    async def complete(self, prompt: str, schema: dict | None = None) -> str | dict:
        """
        Send a prompt to Claude and return the response.

        Args:
            prompt: The full prompt to send. No system prompt — keep it simple.
            schema: If provided, use structured output via the tool_use pattern.
                    The dict must be a valid JSON Schema object.

        Returns:
            str  — plain text response when schema=None
            dict — structured response when schema is provided
                   Returns {} if the LLM call fails in structured mode.

        Note:
            max_tokens=1024 is sufficient for all Qmemory tasks (dedup decisions,
            relationship detection, etc.) since responses are short structured dicts.
        """
        try:
            if schema:
                # ---- Structured output mode ----
                # We define a single "respond" tool with our desired JSON Schema.
                # tool_choice forces Claude to call this tool (no free-text response).
                # Claude's response will always be a tool_use block with block.input
                # being a dict that matches the schema.
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[{
                        "name": "respond",
                        "description": "Respond with structured data",
                        "input_schema": schema,
                    }],
                    # Force Claude to use the "respond" tool (no free-text allowed)
                    tool_choice={"type": "tool", "name": "respond"},
                )

                # Walk through the content blocks to find the tool_use block.
                # With tool_choice forced, there should always be exactly one.
                for block in response.content:
                    if block.type == "tool_use":
                        # block.input is the dict matching our schema
                        return block.input  # type: ignore[return-value]

                # If somehow no tool_use block found, return empty dict
                logger.warning("ClaudeProvider: no tool_use block in structured response")
                return {}

            else:
                # ---- Plain text mode ----
                # Standard message creation — Claude responds with free text.
                response = await self._client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    messages=[{"role": "user", "content": prompt}],
                )
                # response.content[0] is a TextBlock — .text gives the string
                return response.content[0].text

        except Exception as e:
            # Graceful degradation — log and return empty value.
            # dedup's rule-based fallback will handle the empty string case.
            logger.warning("ClaudeProvider.complete() failed: %s", e)
            return "" if not schema else {}
