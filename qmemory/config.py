"""
Qmemory Configuration

Pydantic Settings class that reads configuration from environment variables.
All Qmemory-specific settings use the QMEMORY_ prefix to avoid collisions
with other projects (e.g. Awqaf) that may share the same environment.

API keys (ANTHROPIC_API_KEY, VOYAGE_API_KEY, etc.) use their standard names
since they are shared credentials, not Qmemory-specific.

Usage:
    from qmemory.config import get_settings
    s = get_settings()
    print(s.surreal_url)   # reads QMEMORY_SURREAL_URL from env
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class QmemorySettings(BaseSettings):
    """
    All runtime configuration for Qmemory.

    Environment variable names are set via Field(alias=...).
    The populate_by_name=True option also allows using the Python
    attribute name directly (useful in tests and scripts).
    """

    # ------------------------------------------------------------------
    # SurrealDB connection — all QMEMORY_-prefixed to avoid conflicts
    # ------------------------------------------------------------------

    # WebSocket URL for SurrealDB, e.g. ws://localhost:8000
    surreal_url: str = Field("ws://localhost:8000", alias="QMEMORY_SURREAL_URL")

    # SurrealDB admin username
    surreal_user: str = Field("root", alias="QMEMORY_SURREAL_USER")

    # SurrealDB admin password
    surreal_pass: str = Field("root", alias="QMEMORY_SURREAL_PASS")

    # SurrealDB namespace (logical grouping above database level)
    surreal_ns: str = Field("qmemory", alias="QMEMORY_SURREAL_NS")

    # SurrealDB database name within the namespace
    surreal_db: str = Field("main", alias="QMEMORY_SURREAL_DB")

    # ------------------------------------------------------------------
    # API keys — standard names (shared with other tools in the env)
    # ------------------------------------------------------------------

    # Anthropic API key — used for Claude-based LLM calls
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")

    # ZAI API key — used for free/cheap LLM calls in background workers
    zai_api_key: str = Field("", alias="ZAI_API_KEY")

    # Voyage AI API key — used for text embeddings
    voyage_api_key: str = Field("", alias="VOYAGE_API_KEY")

    # Google / Gemini API key — used for book ingestion and OCR
    google_api_key: str = Field("", alias="GOOGLE_API_KEY")

    # ------------------------------------------------------------------
    # Tuning parameters — QMEMORY_-prefixed
    # ------------------------------------------------------------------

    # Maximum fraction of context window used for memory injection (0.0–1.0)
    budget_pct: float = Field(0.15, alias="QMEMORY_BUDGET_PCT")

    # Extraction preset: "economy", "balanced", or "aggressive"
    extraction_mode: str = Field("balanced", alias="QMEMORY_EXTRACTION_MODE")

    # Linker service idle interval in seconds (default: 30 min)
    linker_interval: int = Field(1800, alias="QMEMORY_LINKER_INTERVAL")

    # Reflect service idle interval in seconds (default: 30 min)
    reflect_interval: int = Field(1800, alias="QMEMORY_REFLECT_INTERVAL")

    # Minimum salience for a memory to be included in recall results
    min_salience_recall: float = Field(0.3, alias="QMEMORY_MIN_SALIENCE_RECALL")

    # Enable verbose debug logging
    debug: bool = Field(False, alias="QMEMORY_DEBUG")

    # ------------------------------------------------------------------
    # Pydantic settings config
    # ------------------------------------------------------------------

    model_config = {
        # Load from .env file if present (won't override real env vars)
        "env_file": ".env",
        # Allow using either the Python attribute name OR the alias when
        # constructing the model directly in code (handy for tests)
        "populate_by_name": True,
    }


@lru_cache(maxsize=1)
def get_settings() -> QmemorySettings:
    """
    Return the singleton QmemorySettings instance.

    Using lru_cache means the environment is only read once per process.
    In tests, call get_settings.cache_clear() to force a re-read after
    patching environment variables.

    Example:
        from qmemory.config import get_settings
        s = get_settings()
        print(s.surreal_url)
    """
    return QmemorySettings()  # type: ignore[call-arg]  # Pydantic reads from env vars
