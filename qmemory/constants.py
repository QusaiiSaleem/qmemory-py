"""
Qmemory Constants

All constant values used across the codebase.
Ported from src/constants.ts — values must stay in sync.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Memory categories
# The 8 types of facts the agent can store.
# Order matters: "self" is injected FIRST in context.
# ---------------------------------------------------------------------------

MEMORY_CATEGORIES: list[str] = [
    "style",       # Communication preferences (language, tone, format)
    "preference",  # General user preferences
    "context",     # Facts about projects, orgs, situations
    "decision",    # Past decisions made, with rationale
    "idea",        # Future plans, suggestions, proposals
    "feedback",    # User corrections and error reports
    "domain",      # Sector/domain knowledge
    "self",        # Agent's self-knowledge (soul) — injected first in context
]

# ---------------------------------------------------------------------------
# Extraction presets
# Controls how aggressively facts are extracted from conversations.
# Tuned for different OpenClaw plan tiers.
# ---------------------------------------------------------------------------

EXTRACTION_PRESETS: dict[str, dict] = {
    # economy: For Lite plans — minimal token usage, ~1-2 extractions/hour
    "economy": {
        "hourly_budget": 2,         # Max extractions per hour
        "score_threshold": 7,       # Min score to extract (1-10 scale)
        "min_content_length": 300,  # Min chars before considering extraction
        "dm_priority": 5,           # Bonus for DM channels (more personal)
        "group_priority": 1,        # Bonus for group channels
        "keyword_bonus": 5,         # Bonus when trigger keywords detected
        "long_content_bonus": 2,    # Bonus for messages >500 chars
        "quiet_conversation_bonus": 2,  # Bonus when <3 msgs in last 10 min
    },
    # balanced: For Pro plans — normal operation, ~3-5 extractions/hour (default)
    "balanced": {
        "hourly_budget": 5,
        "score_threshold": 4,
        "min_content_length": 200,
        "dm_priority": 4,
        "group_priority": 1,
        "keyword_bonus": 5,
        "long_content_bonus": 2,
        "quiet_conversation_bonus": 2,
    },
    # aggressive: For Team/Unlimited plans — extract everything, no limits
    "aggressive": {
        "hourly_budget": float("inf"),  # No hourly cap
        "score_threshold": 1,
        "min_content_length": 100,
        "dm_priority": 2,
        "group_priority": 2,
        "keyword_bonus": 5,
        "long_content_bonus": 1,
        "quiet_conversation_bonus": 1,
    },
}

# ---------------------------------------------------------------------------
# Extraction keywords
# If any of these words appear in a message, extraction is triggered
# regardless of the score threshold.
# ---------------------------------------------------------------------------

EXTRACTION_KEYWORDS: list[str] = [
    "remember",
    "note",
    "important",
    "don't forget",
    "save this",
    "keep in mind",
    "for the record",
    "just so you know",
]

# ---------------------------------------------------------------------------
# Default config values
# These mirror the OpenClaw plugin defaults in openclaw.plugin.json
# and the DEFAULT_CONFIG in src/constants.ts.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    # SurrealDB connection
    "surrealdb_url": "ws://localhost:8000",
    "surrealdb_user": "root",
    "surrealdb_pass": "root",
    "namespace": "qmemory",
    "database": "main",
    # Context window management
    "context_threshold": 0.75,      # Compact at 75% context window
    "fresh_tail_count": 32,         # Protect last N messages from compaction
    "memory_budget_pct": 0.15,      # Max 15% of context for memory injection
    # Embedding
    "embedding_provider": "none",   # "voyage", "openai", or "none"
    "embedding_model": "voyage-3",
    "embedding_dimension": 1024,
    # Background services
    "linker_interval_ms": 1_800_000,    # 30 minutes idle interval
    "reflect_interval_ms": 1_800_000,   # 30 minutes idle interval
    # Recall
    "min_salience_recall": 0.3,
    # Subagent (model override not actually possible via SDK — kept for compat)
    "subagent_model": "zai/glm-5",
    # Extraction preset
    "extraction_mode": "balanced",
    # Debug logging
    "debug": False,
}

# ---------------------------------------------------------------------------
# Salience decay constants
# Biological memory model — memories decay differently based on recall history
# ---------------------------------------------------------------------------

SALIENCE_DECAY_NEVER_RECALLED = 0.90    # 10% decay per cycle if never recalled
SALIENCE_DECAY_STALE = 0.98             # 2% decay per cycle if recalled but stale
SALIENCE_DECAY_CEMENTED_FLOOR = 0.5    # Cemented memories never drop below this
SALIENCE_CEMENTED_THRESHOLD = 5        # recall_count >= this = cemented memory

# ---------------------------------------------------------------------------
# Token budget
# Maximum fraction of the context window used for memory injection
# ---------------------------------------------------------------------------

MEMORY_BUDGET_PCT = 0.15   # 15% of context window

# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------

HYPOTHESIS_CONFIDENCE_THRESHOLD = 0.5  # Below this → shown as hypothesis, not fact
