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

from qmemory.app.config import get_app_settings
from qmemory.app.routes.auth import get_session_user, router as auth_router
from qmemory.app.routes.connect import router as connect_router
from qmemory.db.client import is_healthy

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
mcp_app = mcp.http_app(path="/mcp/", json_response=True, stateless_http=True)

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
# Include auth routes (login, signup, logout)
# ---------------------------------------------------------------------------
api.include_router(auth_router)
api.include_router(connect_router)
logger.info("Auth routes included: /login, /signup, /logout")
logger.info("Connect route included: /connect")


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


# ---------------------------------------------------------------------------
# Placeholder: /dashboard (will be built in Task 9)
# ---------------------------------------------------------------------------


@api.get("/dashboard")
async def dashboard_placeholder(request: Request):
    """Temporary placeholder for the dashboard page."""
    from fastapi.responses import HTMLResponse

    user = get_session_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    name = user.get("name", "")
    email = user.get("email", "")
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>لوحة التحكم — Qmemory</title>
    <link href="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Cairo:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>* {{ font-family: 'Cairo', sans-serif; }}</style>
</head>
<body class="min-h-screen bg-base-200">
    <nav class="navbar bg-base-100 shadow-sm">
        <div class="flex-1">
            <a href="/dashboard" class="btn btn-ghost text-xl">🧠 Qmemory</a>
        </div>
        <div class="flex-none">
            <form method="post" action="/logout">
                <button type="submit" class="btn btn-ghost btn-sm">خروج</button>
            </form>
        </div>
    </nav>
    <main class="container mx-auto p-4 max-w-4xl">
        <div class="card bg-base-100 shadow-xl mt-8">
            <div class="card-body text-center">
                <h2 class="card-title text-2xl justify-center mb-4">مرحباً {name}! 👋</h2>
                <p class="text-gray-500">{email}</p>
                <p class="mt-4 text-gray-400">لوحة التحكم قادمة قريباً...</p>
            </div>
        </div>
    </main>
</body>
</html>"""
    )


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


# Mount the MCP sub-app at /mcp/ inside the FastAPI app.
# The MCP endpoint will be accessible at /mcp/mcp/ (mount path + internal path).
api.mount("/mcp", mcp_app)

logger.info("Qmemory Cloud app created — MCP mounted at /mcp/")
