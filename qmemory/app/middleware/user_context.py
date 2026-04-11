"""
MCPUserMiddleware - extracts user_code from /mcp/u/{code}/ URLs,
resolves it to a database name via the admin DB, and sets the
_user_db ContextVar for the duration of the request.

Path rewriting:
    Incoming:  /mcp/u/calm-k7m3p/tools/list
    Forwarded: /mcp/tools/list

This lets the FastMCP sub-app (mounted at /mcp) stay oblivious to
the user-scoping layer.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from qmemory.db.client import _user_db, get_admin_db, query

logger = logging.getLogger(__name__)

_USER_PATH_RE = re.compile(r"^/mcp/u/([a-z0-9-]+)(/.*)?$")

# Module-level default — overridden in tests via monkeypatch.
_ADMIN_DB_NAME: str = "admin"


class MCPUserMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        match = _USER_PATH_RE.match(path)
        if not match:
            return await call_next(request)

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
            return JSONResponse({"error": "admin_unreachable"}, status_code=503)

        if not rows or not rows[0].get("is_active", False):
            logger.info("404 for unknown user_code: %s", user_code)
            return JSONResponse({"error": "not_found"}, status_code=404)

        db_name = rows[0]["db_name"]
        logger.info("MCP request for user %s -> db=%s", user_code, db_name)

        rewritten = "/mcp" + tail
        request.scope["path"] = rewritten
        request.scope["raw_path"] = rewritten.encode()

        token = _user_db.set(db_name)
        try:
            response = await call_next(request)
        finally:
            _user_db.reset(token)

        asyncio.create_task(_touch_user(user_code))

        return response


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
