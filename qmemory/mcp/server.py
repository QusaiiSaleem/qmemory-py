"""
Qmemory MCP Server (stdio transport).

Local FastMCP server for Claude Code and developer use. All tool
definitions come from qmemory/mcp/operations.py via mount_operations().
Edit that file to change any tool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.operations import OPERATIONS, QMEMORY_INSTRUCTIONS
from qmemory.mcp.registry import mount_operations

mcp = FastMCP(
    "qmemory_mcp",
    instructions=QMEMORY_INSTRUCTIONS,
)

mount_operations(mcp, OPERATIONS)
