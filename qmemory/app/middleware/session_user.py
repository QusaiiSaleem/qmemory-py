"""
SessionUserMiddleware — routes web UI requests to the logged-in user's DB.

Parallel to MCPUserMiddleware, but triggered by a session cookie instead
of a URL path. When the current request has a logged-in session (via
Starlette's SessionMiddleware), this middleware reads the session cookie,
pulls `db_name`, and sets the _user_db ContextVar for the duration of
the request. That way, /dashboard, /memories, /connect, etc. automatically
query the right user's database without any route-level changes.

Skips:
- /mcp/* paths (handled by MCPUserMiddleware)
- Requests with no session cookie
- Requests where the session exists but has no user_code

This is a PURE ASGI MIDDLEWARE for the same reason as MCPUserMiddleware:
BaseHTTPMiddleware runs downstream in a separate task context so
ContextVar mutations don't propagate.

Session cookie parsing: we use itsdangerous to decode the signed cookie
directly, matching the format Starlette's SessionMiddleware writes.
"""

from __future__ import annotations

import json
import logging
from base64 import b64decode

from itsdangerous import BadSignature, TimestampSigner

from qmemory.db.client import _user_db

logger = logging.getLogger(__name__)

_COOKIE_NAME = "qmemory_session"
_PATH_PREFIX_SKIP = ("/mcp/", "/_mcp/", "/health", "/static/", "/signup", "/login")


class SessionUserMiddleware:
    """Pure ASGI middleware that routes logged-in web UI requests to user DBs."""

    def __init__(self, app, *, secret_key: str):
        self.app = app
        self._signer = TimestampSigner(str(secret_key))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Fast-path: skip MCP, static, and the pages that handle their own auth.
        if any(path.startswith(p) for p in _PATH_PREFIX_SKIP):
            await self.app(scope, receive, send)
            return

        db_name = self._extract_db_name_from_cookie(scope)
        if not db_name:
            await self.app(scope, receive, send)
            return

        token = _user_db.set(db_name)
        try:
            await self.app(scope, receive, send)
        finally:
            _user_db.reset(token)

    def _extract_db_name_from_cookie(self, scope) -> str | None:
        """Decode Starlette's signed session cookie, return db_name or None.

        Starlette stores sessions as `b64(json_payload).<sig>.<ts>` using
        itsdangerous.TimestampSigner. We re-decode that format here without
        touching Starlette's request object (which isn't available in pure
        ASGI middleware).
        """
        headers = dict(scope.get("headers", []))
        cookie_header = headers.get(b"cookie", b"").decode("latin-1")
        if not cookie_header:
            return None

        # Find the qmemory_session cookie value
        raw_value: str | None = None
        for pair in cookie_header.split(";"):
            if "=" not in pair:
                continue
            k, _, v = pair.strip().partition("=")
            if k == _COOKIE_NAME:
                raw_value = v
                break
        if not raw_value:
            return None

        try:
            data = self._signer.unsign(raw_value.encode("latin-1"), max_age=604800)
            payload = json.loads(b64decode(data))
        except BadSignature:
            logger.debug("session.bad_signature")
            return None
        except Exception:
            logger.debug("session.decode_failed", exc_info=True)
            return None

        if not isinstance(payload, dict):
            return None
        return payload.get("db_name") or None
