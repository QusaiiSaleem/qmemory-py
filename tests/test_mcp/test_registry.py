"""Tests for the FastMCP mount helper."""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations


def test_mount_registers_nine_tools():
    mcp = FastMCP("test_mount")
    mount_operations(mcp, OPERATIONS)
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 9


def test_mount_tool_names_match_operations():
    mcp = FastMCP("test_mount_names")
    mount_operations(mcp, OPERATIONS)
    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    expected = {op.name for op in OPERATIONS}
    assert registered == expected
