"""End-to-end test: two signed-up users see zero data overlap."""

from __future__ import annotations

import json
import re

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from qmemory.app.main import api
from qmemory.db.client import get_admin_db, get_db, query


_URL_CODE_RE = re.compile(r"/mcp/u/([a-z0-9-]+)/")


async def _signup(client: AsyncClient, display_name: str) -> str:
    """Submit signup form and extract the assigned user_code from the response HTML."""
    r = await client.post("/signup", data={"display_name": display_name})
    assert r.status_code == 200, r.text
    match = _URL_CODE_RE.search(r.text)
    assert match, f"could not find user_code in signup response: {r.text[:500]}"
    return match.group(1)


async def _mcp_call(client: AsyncClient, code: str, tool: str, args: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }
    r = await client.post(
        f"/mcp/u/{code}/",
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    assert "result" in data, f"no result in response: {data}"
    content_text = data["result"]["content"][0]["text"]
    return json.loads(content_text)


@pytest.fixture(autouse=True)
async def _cleanup_test_users():
    """Remove any IsoTest users and their databases before and after each run."""
    async def _cleanup():
        async with get_admin_db() as admin:
            rows = await query(
                admin,
                "SELECT user_code, db_name FROM user WHERE display_name IN ['IsoTest Alice', 'IsoTest Bob']",
            )
        if rows:
            for row in rows:
                async with get_db() as base:
                    # Backticks are required for hyphenated DB names (EFF wordlist
                    # codes like `user_audition-uk3um`). Without them the SurrealQL
                    # parser sees the hyphen as a minus operator and the REMOVE
                    # silently fails — leaving an orphan database with the admin
                    # row already deleted. Discovered 2026-04-27 after finding
                    # 12 such orphans across April test runs.
                    await query(base, f"REMOVE DATABASE IF EXISTS `{row['db_name']}`")
            async with get_admin_db() as admin:
                await query(
                    admin,
                    "DELETE user WHERE display_name IN ['IsoTest Alice', 'IsoTest Bob']",
                )

    await _cleanup()
    yield
    await _cleanup()


async def test_two_users_have_isolated_memory_graphs():
    # LifespanManager triggers FastAPI lifespan, which starts the FastMCP
    # StreamableHTTPSessionManager task group. Without it, MCP tool calls
    # fail with "Task group is not initialized".
    async with LifespanManager(api) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app),
            base_url="http://127.0.0.1:3777",
        ) as c:
            alice = await _signup(c, "IsoTest Alice")
            bob = await _signup(c, "IsoTest Bob")

            assert alice != bob

            await _mcp_call(c, alice, "qmemory_save", {
                "content": "Alice keeps a secret fact about project X for herself",
                "category": "context",
            })
            await _mcp_call(c, bob, "qmemory_save", {
                "content": "Bob has an unrelated note about his fishing hobby",
                "category": "preference",
            })

            alice_hits = await _mcp_call(c, alice, "qmemory_search", {"query": "project X"})
            bob_hits = await _mcp_call(c, bob, "qmemory_search", {"query": "project X"})

    alice_contents = _flatten_contents(alice_hits)
    bob_contents = _flatten_contents(bob_hits)

    assert any("Alice" in item for item in alice_contents), (
        f"Alice missing her own fact: {alice_contents}"
    )
    assert not any("Alice" in item for item in bob_contents), (
        f"Bob leaked Alice's data: {bob_contents}"
    )


def _flatten_contents(search_result: dict) -> list[str]:
    out: list[str] = []
    for cat_list in search_result.get("memories", {}).values():
        for mem in cat_list:
            out.append(mem.get("content", ""))
    return out
