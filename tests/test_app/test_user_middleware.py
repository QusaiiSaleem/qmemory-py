"""Tests for MCPUserMiddleware path rewriting and routing."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from qmemory.app.middleware.user_context import MCPUserMiddleware
from qmemory.db.client import _user_db, apply_admin_schema, get_admin_db, query


@pytest.fixture
async def admin_with_test_user():
    async with get_admin_db(database="admin_test") as conn:
        await apply_admin_schema(conn)
        await query(conn, "REMOVE TABLE IF EXISTS user")
        await apply_admin_schema(conn)
        await query(
            conn,
            """CREATE user SET
                user_code = 'test-abc12',
                display_name = 'Tester',
                db_name = 'user_test-abc12',
                is_active = true""",
        )
    yield "test-abc12"
    async with get_admin_db(database="admin_test") as conn:
        await query(conn, "REMOVE TABLE IF EXISTS user")


@pytest.fixture
def probe_app(monkeypatch):
    monkeypatch.setattr(
        "qmemory.app.middleware.user_context._ADMIN_DB_NAME",
        "admin_test",
    )
    app = FastAPI()
    app.add_middleware(MCPUserMiddleware)

    @app.get("/mcp/{rest:path}")
    async def probe(rest: str):
        return JSONResponse({"seen_path": f"/mcp/{rest}", "user_db": _user_db.get()})

    return app


async def test_unknown_user_code_returns_404(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/u/no-such-user/tools/list")
    assert r.status_code == 404


async def test_known_user_code_rewrites_path_and_sets_context(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/u/test-abc12/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["seen_path"] == "/mcp/tools/list"
    assert data["user_db"] == "user_test-abc12"


async def test_non_user_scoped_mcp_path_passes_through(admin_with_test_user, probe_app):
    async with AsyncClient(transport=ASGITransport(app=probe_app), base_url="http://test") as c:
        r = await c.get("/mcp/tools/list")
    assert r.status_code == 200
    data = r.json()
    assert data["seen_path"] == "/mcp/tools/list"
    assert data["user_db"] is None
