"""Tests for the MCP error wrapper."""

from __future__ import annotations

import json

from pydantic import BaseModel

from qmemory.mcp.errors import safe_tool


class _EchoInput(BaseModel):
    value: str


async def _ok_handler(i: _EchoInput) -> dict:
    return {"echoed": i.value}


async def _raise_handler(i: _EchoInput) -> dict:
    raise RuntimeError("boom")


async def test_safe_tool_returns_json_text_on_success():
    result_text = await safe_tool(
        name="test_echo",
        handler=_ok_handler,
        validated=_EchoInput(value="hello"),
    )
    parsed = json.loads(result_text)
    assert parsed == {"echoed": "hello"}


async def test_safe_tool_catches_exceptions_and_returns_is_error():
    result_text = await safe_tool(
        name="test_raise",
        handler=_raise_handler,
        validated=_EchoInput(value="anything"),
    )
    parsed = json.loads(result_text)
    assert parsed["isError"] is True
    assert "content" in parsed
    assert parsed["content"][0]["type"] == "text"
    assert "test_raise" in parsed["content"][0]["text"]
