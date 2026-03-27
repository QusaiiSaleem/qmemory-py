"""
Qmemory Cloud — App Settings

Configuration for the FastAPI + FastMCP HTTP server.
Reads from environment variables with the QMEMORY_ prefix, falling back
to defaults suitable for local development.

This is separate from qmemory/config.py (which configures the core memory
engine). This file configures the web application layer only.

Usage:
    from qmemory.app.config import get_app_settings
    settings = get_app_settings()
    print(settings.public_url)
"""

from __future__ import annotations

from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    """
    Web application settings for Qmemory Cloud.

    Each field maps to an environment variable with the QMEMORY_ prefix.
    For example, `secret_key` reads from QMEMORY_SECRET_KEY.
    """

    # --- SurrealDB connection (duplicated here for the web layer) ---
    surreal_url: str = "ws://localhost:8000"
    surreal_user: str = "root"
    surreal_pass: str = "root"
    surreal_ns: str = "qmemory"
    surreal_db: str = "main"

    # --- Web app settings ---
    # Secret key for session signing / CSRF — CHANGE in production!
    secret_key: str = "change-me-in-production"

    # The public URL where this server is reachable (used for CORS, links)
    public_url: str = "http://localhost:3777"

    # Enable debug mode (verbose logging, auto-reload, etc.)
    debug: bool = False

    # --- OAuth bypass (temporary single-user mode) ---
    # When set, requests to /mcp/?key=THIS_VALUE skip OAuth entirely
    # and route to the bypass user's database. Remove this env var
    # to re-enable full multi-user OAuth.
    bypass_key: str | None = None

    # The email of the user to route to when bypass_key is used.
    # Must be an existing user in the SurrealDB 'user' table.
    bypass_user: str = "hi@qusai.org"

    model_config = {"env_prefix": "QMEMORY_", "env_file": ".env"}


def get_app_settings() -> AppSettings:
    """Return a fresh AppSettings instance (reads env vars each time)."""
    return AppSettings()
