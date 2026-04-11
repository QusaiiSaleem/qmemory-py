"""
Qmemory Cloud — FastAPI + FastMCP HTTP Server.

HTTP entry point for Qmemory Cloud. Tool definitions live in
qmemory/mcp/operations.py. Mounts the FastMCP HTTP sub-app at /mcp.

Run with:
    uv run uvicorn qmemory.app.main:api --port 3777
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from mcp.server.fastmcp import FastMCP
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from qmemory.app.config import get_app_settings
from qmemory.app.routes.auth import get_session_user, router as auth_router
from qmemory.app.routes.connect import router as connect_router
from qmemory.app.routes.dashboard import router as dashboard_router
from qmemory.app.routes.memories import router as memories_router
from qmemory.app.routes.tokens import router as tokens_router
from qmemory.db.client import is_healthy
from qmemory.mcp.operations import OPERATIONS
from qmemory.mcp.registry import mount_operations

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# ---------------------------------------------------------------------------
# FastMCP server — same OPERATIONS as stdio
# ---------------------------------------------------------------------------

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
    stateless_http=True,
    json_response=True,
    # Sub-app's internal route lives at / so mounting at /mcp gives a
    # clean /mcp/ URL (not /mcp/mcp/). Matches the pre-rebuild behavior.
    streamable_http_path="/",
)

mount_operations(mcp, OPERATIONS)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

settings = get_app_settings()

mcp_app = mcp.streamable_http_app()

# FastMCP's streamable_http_app() returns a Starlette instance whose
# lifespan lives on its router. We pass that to FastAPI so the MCP
# session manager's task group starts up when the API does.
api = FastAPI(
    title="Qmemory Cloud",
    version="1.0.0",
    description="Graph-based memory for AI agents - HTTP API",
    debug=settings.debug,
    lifespan=mcp_app.router.lifespan_context,
)

# ---------------------------------------------------------------------------
# Session middleware (signed cookies)
# ---------------------------------------------------------------------------

api.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=604800,
    session_cookie="qmemory_session",
    same_site="lax",
    https_only=False,
)

# ---------------------------------------------------------------------------
# CORS — explicit origin list (credentials + wildcard is broken)
# ---------------------------------------------------------------------------

api.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://claude.ai",
        "https://claude.com",
        "https://anthropic.com",
        "https://www.anthropic.com",
    ],
    allow_origin_regex=r"https://.*\.anthropic\.com",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
    max_age=86400,
)
logger.info("CORS middleware enabled - explicit claude.ai/anthropic.com origins")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

api.include_router(auth_router)
api.include_router(connect_router)
api.include_router(dashboard_router)
api.include_router(memories_router)
api.include_router(tokens_router)


@api.get("/")
async def root_redirect(request: Request):
    user = get_session_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@api.get("/health")
async def health_check():
    start = time.monotonic()
    db_ok = await is_healthy()
    elapsed = time.monotonic() - start
    return {
        "status": "healthy" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "response_time_ms": round(elapsed * 1000, 1),
    }


api.mount("/mcp", mcp_app)
logger.info("Qmemory Cloud app created - MCP mounted at /mcp/ (no auth yet)")
