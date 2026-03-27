"""
Qmemory Cloud — FastAPI + FastMCP HTTP Server

This is the HTTP entry point for Qmemory Cloud. It creates:
  1. A FastMCP server with the same 7 tools as qmemory/mcp/server.py
  2. A FastAPI app with auth pages (login, signup, logout)
  3. Session-based auth using signed cookies (SessionMiddleware)
  4. Mounts the FastMCP server at /mcp/ inside the FastAPI app

The existing qmemory/mcp/server.py stays untouched — it handles stdio
transport for Claude Code. This file handles HTTP transport for
Claude.ai and other HTTP-based MCP clients.

Run with:
    uv run uvicorn qmemory.app.main:api --port 3777
"""

from __future__ import annotations

import json
import logging
import time

import fastmcp
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from qmemory.app.auth import resolve_api_token
from qmemory.app.config import get_app_settings
from qmemory.app.routes.auth import get_session_user, router as auth_router
from qmemory.app.routes.connect import router as connect_router
from qmemory.app.routes.dashboard import router as dashboard_router
from qmemory.app.routes.memories import router as memories_router
from qmemory.app.routes.oauth import router as oauth_router
from qmemory.app.routes.tokens import router as tokens_router
from qmemory.db.client import _user_db, get_db, is_healthy, query
from qmemory.db.provision import provision_user_db

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Configure root logger for structured output when running as main app
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# ---------------------------------------------------------------------------
# FastMCP server — same 7 tools as qmemory/mcp/server.py
# ---------------------------------------------------------------------------

mcp = fastmcp.FastMCP(
    "Qmemory",
    instructions=(
        "Graph memory for AI agents. "
        "Call qmemory_bootstrap first to load your full memory context. "
        "Then use qmemory_search to find specific memories, qmemory_save to "
        "record new facts, qmemory_correct to fix errors, qmemory_link to "
        "create relationships between knowledge nodes, and qmemory_person to "
        "manage person entities."
    ),
)


# ---------------------------------------------------------------------------
# Tool 1: qmemory_bootstrap (read-only)
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_bootstrap(session_key: str = "default") -> str:
    """Load your full memory context for this session.

    Call this at the START of every conversation to remember who you are
    and what you know. Returns your self-model, cross-session memories
    grouped by category, graph map, and session info.

    Args:
        session_key: Identifies this session context. Use the channel/topic
                     name if available (e.g. "telegram/topic:7"), otherwise
                     leave as "default".

    Returns a formatted text block injected into your context window.
    """
    start = time.monotonic()
    logger.info("Tool call: qmemory_bootstrap(session_key=%s)", session_key)

    from qmemory.core.recall import assemble_context

    result = await assemble_context(session_key)

    elapsed = time.monotonic() - start
    logger.info("qmemory_bootstrap completed in %.2fs", elapsed)
    return result


# ---------------------------------------------------------------------------
# Tool 2: qmemory_search (read-only)
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    include_tool_calls: bool = False,
) -> str:
    """Search cross-session memory by meaning, category, or scope.

    Returns memories from ALL past conversations with graph connection hints
    and an exploration nudge. Use this to find what you know about a topic.

    Args:
        query:             Free-text search query (BM25 + vector similarity).
                           Leave empty to get recent memories without text search.
        category:          Filter to one category:
                           self, style, preference, context, decision,
                           idea, feedback, domain
        scope:             Filter visibility: global, project:xxx, topic:xxx
        limit:             Max results to return (default 10, max 50).
        include_tool_calls: Also search past tool call history (default False).

    Returns JSON with {"results": [...], "_nudge": "..."}.
    Each result includes connection hints so you can follow graph edges.
    """
    start = time.monotonic()
    logger.info(
        "Tool call: qmemory_search(query=%s, category=%s, limit=%d)",
        query,
        category,
        limit,
    )

    from qmemory.core.search import search_memories

    results = await search_memories(
        query_text=query,
        category=category,
        scope=scope,
        limit=limit,
        include_tool_calls=include_tool_calls,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_search completed in %.2fs", elapsed)
    return json.dumps(results, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 3: qmemory_save
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_save(
    content: str,
    category: str,
    salience: float = 0.5,
    scope: str = "global",
    confidence: float = 0.8,
    source_person: str | None = None,
    evidence_type: str = "observed",
    context_mood: str | None = None,
) -> str:
    """Save a fact to cross-session memory with evidence tracking.

    Runs deduplication automatically — if a similar memory exists it will
    UPDATE or NOOP instead of creating a duplicate. Returns the action taken.

    Args:
        content:       The fact to remember. One clear statement.
                       Example: "Qusai prefers concise bullet points over paragraphs"
        category:      What type of fact this is:
                       self, style, preference, context, decision,
                       idea, feedback, domain
        salience:      Importance 0.0-1.0. High-salience memories are recalled first.
        scope:         Who can see this: global | project:xxx | topic:xxx
        confidence:    How certain are you? 0.0-1.0. Use < 0.5 for hypotheses.
        source_person: Who said this? Pass entity ID if known.
        evidence_type: How was this learned? observed | reported | inferred | self
        context_mood:  Situation when learned:
                       calm_decision | heated_discussion | brainstorm |
                       correction | casual | urgent

    Returns JSON with action (ADD/UPDATE/NOOP), memory_id, and a nudge.
    """
    start = time.monotonic()
    logger.info(
        "Tool call: qmemory_save(category=%s, salience=%.2f)",
        category,
        salience,
    )

    from qmemory.core.save import save_memory

    result = await save_memory(
        content=content,
        category=category,
        salience=salience,
        scope=scope,
        confidence=confidence,
        source_person=source_person,
        evidence_type=evidence_type,
        context_mood=context_mood,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_save completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 4: qmemory_correct
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_correct(
    memory_id: str,
    action: str,
    new_content: str | None = None,
    updates: dict | None = None,
    edge_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Fix or delete a memory. Preserves full audit trail via soft-delete.

    Args:
        memory_id:   Full record ID, e.g. "memory:mem1710864000000abc".
        action:      What to do: correct | delete | update | unlink
        new_content: The corrected fact text. Required when action="correct".
        updates:     Metadata fields to change. Required when action="update".
        edge_id:     The relates edge ID to delete. Required when action="unlink".
        reason:      Optional note explaining why this change was made.

    Returns JSON with ok (bool) and details about what changed.
    """
    start = time.monotonic()
    logger.info(
        "Tool call: qmemory_correct(memory_id=%s, action=%s)",
        memory_id,
        action,
    )

    from qmemory.core.correct import correct_memory

    result = await correct_memory(
        memory_id=memory_id,
        action=action,
        new_content=new_content,
        updates=updates,
        edge_id=edge_id,
        reason=reason,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_correct completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5: qmemory_link
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_link(
    from_id: str,
    to_id: str,
    relationship_type: str,
    reason: str | None = None,
    confidence: float | None = None,
) -> str:
    """Create a relationship edge between any two nodes in the memory graph.

    Args:
        from_id:           Source node ID (e.g. "memory:mem1710864000000abc").
        to_id:             Target node ID. Can be a different table type.
        relationship_type: Any string (supports, contradicts, caused_by, etc.).
        reason:            Optional note explaining why this connection exists.
        confidence:        How confident in this connection? 0.0-1.0.

    Returns JSON with edge_id and an exploration nudge.
    """
    start = time.monotonic()
    logger.info(
        "Tool call: qmemory_link(from=%s, to=%s, type=%s)",
        from_id,
        to_id,
        relationship_type,
    )

    from qmemory.core.link import link_nodes

    result = await link_nodes(
        from_id=from_id,
        to_id=to_id,
        relationship_type=relationship_type,
        reason=reason,
        confidence=confidence,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_link completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 6: qmemory_person
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_person(
    name: str,
    aliases: list[str] | None = None,
    contacts: list[dict] | None = None,
) -> str:
    """Create or find a person entity with linked identities across systems.

    Args:
        name:     The person's display name. Example: "Ahmed Al-Rashid"
        aliases:  Optional alternative names or nicknames.
        contacts: Optional list of contact identities. Each dict needs:
                  - system:  "telegram", "whatsapp", "email", "smartsheet"
                  - handle:  The identifier in that system

    Returns JSON with entity_id, contact_ids, links_created, and action.
    """
    start = time.monotonic()
    logger.info("Tool call: qmemory_person(name=%s)", name)

    from qmemory.core.person import create_person

    result = await create_person(
        name=name,
        aliases=aliases,
        contacts=contacts,
    )

    elapsed = time.monotonic() - start
    logger.info("qmemory_person completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 7: qmemory_import (stub)
# ---------------------------------------------------------------------------


@mcp.tool()
async def qmemory_import(file_path: str) -> str:
    """Import a markdown file into the memory graph.

    Args:
        file_path: Absolute path to the markdown file to import.

    Note: Full implementation coming in a future update.
    """
    logger.info("Tool call: qmemory_import(file_path=%s)", file_path)
    return json.dumps(
        {
            "status": "not_implemented",
            "message": "Import is coming in a future update.",
            "file_path": file_path,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# FastAPI app — wraps the FastMCP server + adds /health
# ---------------------------------------------------------------------------

settings = get_app_settings()

# Create the MCP HTTP sub-app FIRST so we can grab its lifespan.
# FastMCP's StreamableHTTPSessionManager needs its lifespan to initialize
# the internal task group that processes MCP JSON-RPC requests.
# json_response=True lets clients use Accept: application/json (simpler).
# stateless_http=True means each request is independent (no session tracking).
# This is the simplest mode for Claude.ai and other HTTP MCP clients.
mcp_app = mcp.http_app(path="/", json_response=True, stateless_http=True)

api = FastAPI(
    title="Qmemory Cloud",
    version="1.0.0",
    description="Graph-based memory for AI agents — HTTP API",
    debug=settings.debug,
    # Pass the MCP app's lifespan so its task group gets initialized.
    # Without this, MCP requests fail with "Task group is not initialized".
    lifespan=mcp_app.lifespan,
)

# ---------------------------------------------------------------------------
# Middleware: signed session cookies (requires itsdangerous)
# ---------------------------------------------------------------------------
# SessionMiddleware stores session data in a signed cookie on the client.
# The secret_key signs the cookie so it can't be tampered with.
# max_age=604800 means the cookie lasts 7 days (60 * 60 * 24 * 7).
api.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    max_age=604800,
    session_cookie="qmemory_session",
    same_site="lax",
    https_only=False,  # Set to True in production with HTTPS
)

# ---------------------------------------------------------------------------
# CORS Middleware — allow Claude.ai and other MCP clients
# ---------------------------------------------------------------------------
# Claude.ai makes cross-origin requests for OAuth and MCP, so we need
# to respond to CORS preflight (OPTIONS) with proper headers.
from starlette.middleware.cors import CORSMiddleware

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, you may want to restrict this
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    max_age=86400,  # Cache preflight for 24 hours
)
logger.info("CORS middleware enabled — allowing all origins for OAuth/MCP")

# ---------------------------------------------------------------------------
# Include auth routes (login, signup, logout)
# ---------------------------------------------------------------------------
api.include_router(auth_router)
api.include_router(connect_router)
api.include_router(dashboard_router)
api.include_router(memories_router)
api.include_router(tokens_router)
api.include_router(oauth_router)
logger.info("Auth routes included: /login, /signup, /logout")
logger.info("Connect route included: /connect")
logger.info("Dashboard route included: /dashboard")
logger.info("Memory routes included: /memories, /memories/search, /memories/{id}")
logger.info("Tokens routes included: /tokens, /tokens/generate, /tokens/{id}")
logger.info("OAuth routes included: /authorize, /consent, /token")


# ---------------------------------------------------------------------------
# Root redirect: / → /dashboard (if logged in) or /login (if not)
# ---------------------------------------------------------------------------


@api.get("/")
async def root_redirect(request: Request):
    """Redirect root URL to dashboard or login page."""
    user = get_session_user(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


def _get_base_url(request: Request) -> str:
    """
    Get the public-facing base URL, respecting reverse proxy headers.

    Railway (and other proxies) terminate TLS and forward requests as HTTP.
    The app sees http:// but the public URL is https://. We check
    X-Forwarded-Proto to detect this.
    """
    base_url = str(request.base_url).rstrip("/")
    # Railway sets X-Forwarded-Proto: https when TLS is terminated at the proxy
    proto = request.headers.get("x-forwarded-proto", "")
    if proto == "https" and base_url.startswith("http://"):
        base_url = "https://" + base_url[7:]
    return base_url


@api.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """
    OAuth 2.0 Authorization Server Metadata (RFC 8414).

    Claude.ai uses this to discover our OAuth endpoints automatically.
    It tells the MCP client where to send users for authorization,
    where to register as a client, and where to exchange codes for tokens.
    """
    base_url = _get_base_url(request)

    logger.info("oauth.metadata_requested base_url=%s", base_url)

    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "registration_endpoint": f"{base_url}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["read", "write"],
    }


@api.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request):
    """
    OAuth 2.0 Protected Resource Metadata (RFC 9728).

    Tells MCP clients where the authorization server is for this resource.
    """
    base_url = _get_base_url(request)

    logger.info("oauth.protected_resource_requested base_url=%s", base_url)

    return {
        "resource": base_url,
        "authorization_servers": [base_url],
        "scopes_supported": ["read", "write"],
        "bearer_methods_supported": ["header"],  # Authorization: Bearer <token>
    }


@api.get("/health")
async def health_check():
    """Check if the server and SurrealDB are reachable."""
    start = time.monotonic()
    logger.info("Health check requested")

    db_ok = await is_healthy()

    elapsed = time.monotonic() - start
    status = "healthy" if db_ok else "degraded"
    logger.info("Health check: %s (%.2fs)", status, elapsed)

    return {
        "status": status,
        "database": "connected" if db_ok else "unreachable",
        "response_time_ms": round(elapsed * 1000, 1),
    }


# ---------------------------------------------------------------------------
# MCP Auth Middleware — validates API token and provisions user database
# ---------------------------------------------------------------------------
# Every request to /mcp/ must include:
#   Authorization: Bearer qm_ak_xxxxx
#
# The middleware:
# 1. Validates the token against api_token table
# 2. Provisions user database if it doesn't exist
# 3. Sets _user_db context var to route all DB calls to user's private database


class MCPAuthMiddleware:
    """ASGI middleware that validates API token and provisions user database."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def _resolve_bypass_user(self) -> dict | None:
        """Look up the bypass user from the database by email.

        Only called when QMEMORY_BYPASS_KEY is set and the request
        includes a matching ?key= parameter.
        """
        settings = get_app_settings()
        async with get_db() as db:
            result = await query(
                db,
                "SELECT * FROM user WHERE email = $email LIMIT 1",
                {"email": settings.bypass_user},
            )
            if result and isinstance(result, list) and len(result) > 0:
                return result[0]
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = StarletteRequest(scope)

        # NOTE: CORS preflight (OPTIONS) is handled by CORSMiddleware on the
        # FastAPI app — it intercepts OPTIONS before requests reach this middleware.
        # No duplicate CORS handling needed here.

        # Log all MCP requests for debugging
        logger.info(
            "mcp.request method=%s path=%s has_auth=%s",
            request.method,
            request.url.path,
            "authorization" in request.headers,
        )

        try:
            user = await resolve_api_token(request)
        except Exception as exc:
            # resolve_api_token raises HTTPException on invalid token
            status = getattr(exc, "status_code", 401)
            detail = getattr(exc, "detail", "Authentication failed")
            logger.warning("mcp.auth_failed reason=%s", detail)
            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": detail},
                },
                status_code=status,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        if user is None:
            # --- OAuth bypass (temporary single-user mode) ---
            # When QMEMORY_BYPASS_KEY env var exists, skip OAuth entirely
            # and route all unauthenticated requests to the bypass user.
            # This is a workaround for Claude.ai's broken OAuth flow.
            #
            # TO RE-ENABLE MULTI-USER OAUTH:
            #   1. Run: railway variables delete QMEMORY_BYPASS_KEY --service qmemory-api
            #   2. bypass_key becomes None → this block is skipped
            #   3. Normal 401 + OAuth flow takes over automatically
            settings = get_app_settings()
            if settings.bypass_key:
                bypass_user = await self._resolve_bypass_user()
                if bypass_user:
                    user_id = bypass_user.get("id", "")
                    if isinstance(user_id, str) and user_id.startswith("user:"):
                        raw_id = user_id[5:]
                        db_name = f"user_{raw_id}"
                        try:
                            await provision_user_db(raw_id)
                        except Exception as exc:
                            logger.warning("mcp.bypass_provision_failed: %s", exc)
                        _user_db.set(db_name)
                        logger.info("mcp.bypass_auth user=%s db=%s", settings.bypass_user, db_name)
                        await self.app(scope, receive, send)
                        return

            # No bypass — return 401 with WWW-Authenticate header
            # This tells MCP clients (like Claude.ai) to start the OAuth flow
            logger.info("mcp.no_token — returning 401 with WWW-Authenticate")
            resource_url = str(request.base_url).rstrip("/")
            proto = request.headers.get("x-forwarded-proto", "")
            if proto == "https" and resource_url.startswith("http://"):
                resource_url = "https://" + resource_url[7:]

            response = JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Authorization required",
                    },
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{resource_url}/.well-known/oauth-protected-resource"',
                },
            )
            await response(scope, receive, send)
            return

        # Token valid — provision database and set routing
        user_id = user.get("id", "")
        if user_id and user_id.startswith("user:"):
            raw_id = user_id[5:]  # Strip "user:" prefix
            db_name = f"user_{raw_id}"

            # Provision database if it doesn't exist (idempotent)
            try:
                await provision_user_db(raw_id)
                logger.debug("mcp.db_provisioned user_id=%s db=%s", raw_id, db_name)
            except Exception as exc:
                logger.warning(
                    "mcp.db_provision_failed user_id=%s reason=%s (continuing anyway)",
                    raw_id,
                    exc,
                )

            # Set the context var so all get_db() calls route to user's database
            _user_db.set(db_name)
            logger.debug("mcp.db_routing user_id=%s db=%s", raw_id, db_name)
        else:
            logger.warning("mcp.missing_user_id user=%s", user)

        # Pass through to the MCP app
        await self.app(scope, receive, send)


# Mount the MCP sub-app at /mcp/ inside the FastAPI app.
# The MCP endpoint will be accessible at /mcp/mcp/ (mount path + internal path).
api.mount("/mcp", MCPAuthMiddleware(mcp_app))

logger.info("Qmemory Cloud app created — MCP mounted at /mcp/ (auth required)")
