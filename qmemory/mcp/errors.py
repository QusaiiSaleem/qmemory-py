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
    logger.info("Tool call: %s(%s)", name, _scrub_for_log(validated))

    try:
        result = await handler(validated)
    except Exception as exc:
        logger.exception("Unhandled exception in %s", name)
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
    logger.info("%s completed in %.2fs", name, elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


def _scrub_for_log(model: BaseModel) -> dict:
    """Return a short, log-safe dict of the input."""
    data = model.model_dump()
    for k, v in list(data.items()):
        if isinstance(v, str) and len(v) > 120:
            data[k] = v[:117] + "..."
    return data
