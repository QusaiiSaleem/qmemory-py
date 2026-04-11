"""
MCP error wrapper.

Every MCP tool call in Qmemory is wrapped by safe_tool(). It catches
any exception from the handler and returns a valid MCP tool error
response (isError: true + content block) instead of letting the
exception crash the JSON-RPC transport.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)


async def safe_tool(
    name: str,
    handler: Callable[[Any], Awaitable[Any]],
    validated: BaseModel,
) -> str:
    """Invoke an MCP tool handler with uniform error handling."""
    start = time.monotonic()
    logger.info("tool.call name=%s args=%s", name, _scrub_for_log(validated))

    try:
        result = await handler(validated)
    except Exception as exc:
        logger.exception("tool.error name=%s", name)
        error_text = (
            f"Internal error in {name}: {type(exc).__name__}. "
            "Check server logs for details."
        )
        return json.dumps(
            {
                "isError": True,
                "content": [{"type": "text", "text": error_text}],
            },
            ensure_ascii=False,
        )

    elapsed = time.monotonic() - start
    logger.info("tool.done name=%s elapsed=%.2fs", name, elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


def _scrub_for_log(model: BaseModel) -> str:
    """Return a single-line, compact JSON string of the input.

    Long string values are truncated. The result is JSON so it sits on
    one line in production logs (no Rich pretty-printing wrap).
    """
    data = model.model_dump()
    cleaned: dict = {}
    for k, v in data.items():
        if isinstance(v, str) and len(v) > 120:
            cleaned[k] = v[:117] + "..."
        elif v is None:
            # Skip nulls — they're just noise in the log line.
            continue
        else:
            cleaned[k] = v
    return json.dumps(cleaned, ensure_ascii=False, default=str, separators=(",", ":"))
