"""
FastMCP mount helper — loops OPERATIONS and registers each tool on
the given FastMCP server via the official SDK's @mcp.tool() decorator.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.errors import safe_tool
from qmemory.mcp.operations import Operation


def mount_operations(mcp: FastMCP, operations: list[Operation]) -> None:
    """Register every Operation as a tool on the given server."""
    for op in operations:
        _register_one(mcp, op)


def _register_one(mcp: FastMCP, op: Operation) -> None:
    """Register a single Operation with the FastMCP server.

    Uses inspect.Signature to give the wrapper the same parameters
    as the input model, so FastMCP's schema introspection produces
    the right JSON schema.
    """
    input_model = op.input_model
    fields = input_model.model_fields

    sig_params: list[inspect.Parameter] = []
    for name, field in fields.items():
        if field.is_required():
            default: Any = inspect.Parameter.empty
        else:
            default = field.default if field.default is not None else None
        sig_params.append(
            inspect.Parameter(
                name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=field.annotation,
            )
        )

    async def wrapper(**kwargs: Any) -> str:
        try:
            validated = input_model(**kwargs)
        except Exception as exc:
            return json.dumps(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"Invalid arguments for {op.name}: {exc}",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return await safe_tool(name=op.name, handler=op.handler, validated=validated)

    wrapper.__name__ = op.name
    wrapper.__doc__ = op.description
    wrapper.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=sig_params, return_annotation=str
    )

    mcp.tool(
        name=op.name,
        description=op.description,
        annotations=op.annotations,
    )(wrapper)
