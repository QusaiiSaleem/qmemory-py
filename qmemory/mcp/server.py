"""
Qmemory MCP Server (stdio transport).

Local FastMCP server for Claude Code and developer use. All tool
definitions come from qmemory/mcp/operations.py via mount_operations().
Edit that file to change any tool.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations

mcp = FastMCP(
    "qmemory_mcp",
    instructions=(
        "Graph memory for AI agents. "
        "Call qmemory_bootstrap first to load your full memory context. "
        "Then use qmemory_search to find specific memories, qmemory_save to "
        "record new facts, qmemory_correct to fix errors, qmemory_link to "
        "create relationships between knowledge nodes, and qmemory_person to "
        "manage person entities."
    ),
)

mount_operations(mcp, OPERATIONS)
