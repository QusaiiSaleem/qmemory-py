"""
Qmemory Token Budget — Smart Rate Limiter for Background LLM Work

Treats background LLM calls like a finite hourly resource.
Services must call can_spend() before calling an LLM subagent.
If the budget is exhausted, non-critical work is deferred until
the next hour window when old entries expire.

Priority tiers:
  critical — compaction / context-overflow prevention. ALWAYS allowed.
  normal   — afterTurn extraction, dedup. Allowed until budget is full.
  low      — linker, reflect, scratchpad. Cut off at 80% of normal budget.

Budget presets per extraction_mode:
  economy    — 30,000 tokens/hour, 50% reserved for critical work
  balanced   — 80,000 tokens/hour, 30% reserved for critical work  (default)
  aggressive — unlimited (hourly_limit = 0 means no cap)

Usage:
    from qmemory.core.token_budget import init_token_budget, can_spend, record_spend

    init_token_budget("balanced")          # call once at startup

    if can_spend(2000, priority="normal"):
        result = await run_llm(...)
        record_spend(2000, source="dedup", priority="normal")

Ported from src/core/token-budget.ts — behaviour must stay in sync.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TokenPriority = Literal["critical", "normal", "low"]


@dataclass
class _TokenRecord:
    """A single token spend event stored in the rolling ledger."""
    timestamp: float    # time.time() value when the spend was recorded
    tokens: int         # how many tokens were used
    source: str         # which service recorded this (e.g. "dedup", "linker")
    priority: TokenPriority


@dataclass
class _BudgetConfig:
    """Budget limits for a given extraction_mode."""
    # Max tokens per hour for background work. 0 = unlimited.
    hourly_limit: int
    # Fraction of the hourly limit reserved for critical work (0.0–1.0).
    # Normal + low work cannot use this reserved slice.
    critical_reserve: float


# ---------------------------------------------------------------------------
# Budget presets — one per extraction_mode
# ---------------------------------------------------------------------------

_BUDGET_PRESETS: dict[str, _BudgetConfig] = {
    # economy: ~15 background LLM calls/hour, half the budget held for compaction
    "economy": _BudgetConfig(
        hourly_limit=30_000,
        critical_reserve=0.5,
    ),
    # balanced: ~40 background LLM calls/hour, 30% held for compaction (default)
    "balanced": _BudgetConfig(
        hourly_limit=80_000,
        critical_reserve=0.3,
    ),
    # aggressive: no cap at all — use for Team/Unlimited plans
    "aggressive": _BudgetConfig(
        hourly_limit=0,
        critical_reserve=0.0,
    ),
}

# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------

# Rolling ledger of spend events in the last hour.
# A deque lets us efficiently pop expired entries from the left.
_ledger: deque[_TokenRecord] = deque()

# Active budget config. Starts at "balanced" and can be changed at startup.
_config: _BudgetConfig = _BUDGET_PRESETS["balanced"]

# How many seconds form the rolling window (1 hour).
_WINDOW_SECONDS = 3600

# Low-priority work is cut off when total usage exceeds this fraction of
# the non-critical budget (mirrors the 80% threshold in the TypeScript version).
_LOW_PRIORITY_CUTOFF = 0.80


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_token_budget(extraction_mode: str) -> None:
    """
    Set the active budget config from an extraction_mode name.

    Call this once at startup after loading config. Falls back to "balanced"
    if an unrecognised mode is passed.

    Args:
        extraction_mode: "economy", "balanced", or "aggressive".
    """
    global _config
    _config = _BUDGET_PRESETS.get(extraction_mode, _BUDGET_PRESETS["balanced"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _prune() -> None:
    """
    Remove ledger entries older than the rolling window (1 hour).

    Called before every read operation to keep the ledger accurate.
    The deque is ordered by time, so we only need to pop from the left.
    """
    cutoff = time.time() - _WINDOW_SECONDS
    while _ledger and _ledger[0].timestamp < cutoff:
        _ledger.popleft()


def _used_in_window() -> int:
    """
    Return the total tokens spent in the last hour.

    Prunes expired entries first so the count is always fresh.
    """
    _prune()
    return sum(r.tokens for r in _ledger)


def _used_by_priority(priority: TokenPriority) -> int:
    """
    Return tokens spent in the last hour for a specific priority tier.

    Used only for the budget snapshot (logging / debugging).
    """
    _prune()
    return sum(r.tokens for r in _ledger if r.priority == priority)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def can_spend(estimated_tokens: int, priority: TokenPriority = "normal") -> bool:
    """
    Check whether a background task is allowed to spend tokens right now.

    Rules:
      - critical: ALWAYS allowed (compaction can't be deferred).
      - normal:   allowed while total usage < (hourly_limit × (1 - critical_reserve)).
      - low:      allowed while total usage < 80% of the non-critical budget.
      - unlimited mode (hourly_limit == 0): always returns True.

    Args:
        estimated_tokens: How many tokens the task expects to use.
        priority:         "critical", "normal", or "low".

    Returns:
        True if the task may proceed, False if it should be deferred.
    """
    # Unlimited mode — no cap, always allow.
    if _config.hourly_limit <= 0:
        return True

    # Critical work is never blocked — compaction prevents context overflow.
    if priority == "critical":
        return True

    used = _used_in_window()
    # The slice of the budget available to non-critical work.
    non_critical_budget = _config.hourly_limit * (1 - _config.critical_reserve)

    if priority == "normal":
        # Normal work can use the full non-critical slice.
        return used + estimated_tokens <= non_critical_budget

    # Low priority: only use up to 80% of the non-critical slice.
    # This leaves headroom for normal-priority work to run even when
    # low-priority background services are active.
    return used + estimated_tokens <= non_critical_budget * _LOW_PRIORITY_CUTOFF


def record_spend(tokens: int, source: str, priority: TokenPriority = "normal") -> None:
    """
    Record tokens spent by a background task.

    Call this immediately after an LLM call returns so the rolling window
    stays accurate for subsequent can_spend() checks.

    Args:
        tokens:   Actual tokens consumed (use estimated if actual is unavailable).
        source:   Which service is recording this (e.g. "linker", "dedup").
        priority: The priority tier used when spending was approved.
    """
    _ledger.append(_TokenRecord(
        timestamp=time.time(),
        tokens=tokens,
        source=source,
        priority=priority,
    ))


def get_budget_snapshot() -> dict:
    """
    Return a snapshot of current budget usage for logging and debugging.

    Not used in hot paths — safe to call at any time.

    Returns:
        Dict with hourly_limit, used_total, used_critical, used_normal,
        used_low, remaining, and records_count.
    """
    _prune()
    used = _used_in_window()
    remaining = (
        max(0, _config.hourly_limit - used)
        if _config.hourly_limit > 0
        else float("inf")
    )
    return {
        "hourly_limit": _config.hourly_limit,
        "used_total": used,
        "used_critical": _used_by_priority("critical"),
        "used_normal": _used_by_priority("normal"),
        "used_low": _used_by_priority("low"),
        "remaining": remaining,
        "records_count": len(_ledger),
    }
