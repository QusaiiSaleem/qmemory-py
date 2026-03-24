"""
Qmemory CLI

Entry point for all command-line operations:
- serve       — Start MCP server (stdio, for Claude Code)
- serve-http  — Start MCP server (HTTP, for Claude.ai)
- status      — Check SurrealDB connection + show record counts
- schema      — Apply database schema (idempotent)
- worker      — Placeholder for background worker (Phase 2)
"""

import asyncio

import click


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
def main():
    """Qmemory — Graph-based memory for AI agents."""
    pass


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@main.command()
def serve():
    """Start MCP server (stdio transport for Claude Code)."""
    # Import here so the module is only loaded when this command runs.
    # This also means the command works even if mcp/server.py doesn't exist
    # yet — it only fails at call time, not at import time.
    from qmemory.mcp.server import mcp

    mcp.run()  # Default transport is stdio


# ---------------------------------------------------------------------------
# serve-http
# ---------------------------------------------------------------------------


@main.command("serve-http")
@click.option("--port", default=3777, show_default=True, help="HTTP port to listen on.")
def serve_http(port):
    """Start MCP server (streamable-HTTP transport for Claude.ai)."""
    from qmemory.mcp.server import mcp

    mcp.run(transport="streamable-http", port=port)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command()
def status():
    """Check SurrealDB connection and show record counts for every table."""
    asyncio.run(_status())


async def _status():
    """Async implementation of the status check."""
    from qmemory.db.client import get_db, is_healthy, query

    # 1. Ping the database
    healthy = await is_healthy()
    if not healthy:
        click.echo("SurrealDB: NOT CONNECTED")
        click.echo("  Make sure SurrealDB is running — see CLAUDE.md for startup instructions.")
        return

    click.echo("SurrealDB: CONNECTED")

    # 2. Count rows in each table
    # ORDER matters for readability — core tables first, then edges/support tables
    tables = [
        "memory",
        "entity",
        "session",
        "message",
        "tool_call",
        "relates",
        "scratchpad",
        "metrics",
    ]

    click.echo("")
    click.echo("  Table            Count")
    click.echo("  " + "-" * 25)

    async with get_db() as db:
        for table in tables:
            # GROUP ALL collapses all rows into one count() result
            result = await query(db, f"SELECT count() FROM {table} GROUP ALL")

            # result is a list of dicts when rows exist, or None/empty on error
            count = 0
            if result and isinstance(result, list) and len(result) > 0:
                count = result[0].get("count", 0)

            # Left-align the table name in a 16-char column, right-align the count
            click.echo(f"  {table:<16} {count:>5}")

    click.echo("")


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


@main.command()
def schema():
    """Apply database schema to SurrealDB (safe to run multiple times)."""
    asyncio.run(_schema())


async def _schema():
    """Async implementation of schema application."""
    from qmemory.db.client import apply_schema, get_db

    click.echo("Applying schema...")

    async with get_db() as db:
        await apply_schema(db)

    click.echo("Schema applied successfully.")


# ---------------------------------------------------------------------------
# worker  (Phase 2 placeholder)
# ---------------------------------------------------------------------------


@main.command()
def worker():
    """Start background worker (linker, reflect, salience decay). Coming in Phase 2."""
    click.echo("Worker not implemented yet. Coming in Phase 2.")


# ---------------------------------------------------------------------------
# Entry point (when run directly as a script)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    main()
