"""
MCPUserMiddleware — extracts user_code from /mcp/u/{code}/ URLs,
resolves it to a database name via the admin DB, and sets the
_user_db ContextVar for the duration of the request.

Path rewriting:
    Incoming:  /mcp/u/calm-k7m3p/tools/list
    Forwarded: /_mcp/tools/list

This is a PURE ASGI MIDDLEWARE (not BaseHTTPMiddleware) because
Starlette's BaseHTTPMiddleware runs the downstream in a separate task
context, which prevents ContextVar mutations from propagating to the
mounted FastMCP handler. Pure ASGI middleware runs in the same task
and so the _user_db ContextVar set before `await self.app(scope, ...)`
is visible inside core/search.py's get_db() call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from qmemory.db.client import _user_db, get_admin_db, query

logger = logging.getLogger(__name__)

_USER_PATH_RE = re.compile(r"^/mcp/u/([a-z0-9-]+)(/.*)?$")

# Module-level default — overridable in tests via monkeypatch.
_ADMIN_DB_NAME: str = "admin"


class MCPUserMiddleware:
    """Pure ASGI middleware that routes /mcp/u/{code}/... to a user DB."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Only intercept HTTP requests on the MCP user path.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        match = _USER_PATH_RE.match(path)
        if not match:
            await self.app(scope, receive, send)
            return

        user_code = match.group(1)
        tail = match.group(2) or "/"

        try:
            async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
                rows = await query(
                    admin,
                    "SELECT db_name, is_active FROM user WHERE user_code = $code",
                    {"code": user_code},
                )
        except Exception:
            logger.exception("Admin lookup failed for user %s", user_code)
            await _send_json(send, 503, {"error": "admin_unreachable"})
            return

        if not rows or not rows[0].get("is_active", False):
            logger.info("404 for unknown user_code: %s", user_code)
            await _send_json(send, 404, {"error": "not_found"})
            return

        db_name = rows[0]["db_name"]
        logger.info("MCP request for user %s -> db=%s", user_code, db_name)

        # Rewrite path so the downstream /_mcp mount handles it.
        rewritten = "/_mcp" + tail
        new_scope = dict(scope)
        new_scope["path"] = rewritten
        new_scope["raw_path"] = rewritten.encode()

        # Set the ContextVar BEFORE calling the downstream app. Because
        # this middleware is pure-ASGI (not BaseHTTPMiddleware), the
        # downstream runs in the same task and inherits our context.
        token = _user_db.set(db_name)
        try:
            await self.app(new_scope, receive, send)
        finally:
            _user_db.reset(token)

        # Fire-and-forget last_active_at update.
        asyncio.create_task(_touch_user(user_code))


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _touch_user(user_code: str) -> None:
    try:
        async with get_admin_db(database=_ADMIN_DB_NAME) as admin:
            await query(
                admin,
                "UPDATE user SET last_active_at = time::now() WHERE user_code = $code",
                {"code": user_code},
            )
    except Exception:
        logger.debug("last_active_at update failed for %s", user_code, exc_info=True)
