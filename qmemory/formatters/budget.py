"""
Token budget utilities.

Functions for estimating tokens and fitting memories within a context budget.
These are pure functions — no DB, no async, no side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """
    Rough token count using ~4 chars per token.

    This is a conservative heuristic — good enough for budget decisions.
    No need to load a full tokenizer for memory trimming.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Budget trimming
# ---------------------------------------------------------------------------

def apply_budget(memories: list[dict], max_tokens: int) -> list[dict]:
    """
    Trim memories list to fit within max_tokens.

    Keeps highest-salience memories first (assumes list is already sorted
    by salience descending, or sorts internally by salience).

    Also skips noisy memories:
    - Very short content (< 15 chars)
    - Duplicate content (first 60 chars used as key)

    Args:
        memories:   List of memory dicts. Each should have "content" and
                    optionally "salience" (float 0-1).
        max_tokens: Maximum token budget.

    Returns:
        A trimmed list of memories that fits within the budget.
    """
    # Sort by salience descending so highest-value memories survive trimming.
    # Memories without a salience field default to 0.5 (neutral).
    sorted_mems = sorted(memories, key=lambda m: m.get("salience", 0.5), reverse=True)

    result: list[dict] = []
    seen: set[str] = set()  # Dedup by first 60 chars of content
    tokens_used = 0

    for mem in sorted_mems:
        content = mem.get("content", "")

        # Skip noise: very short content
        if len(content) < 15:
            continue

        # Skip noise: bare date strings (e.g. "2026-01-01")
        if len(content) < 30 and content[:4].isdigit() and "-" in content[:10]:
            continue

        # Dedup: skip near-identical content
        normalized = content.lower().split()
        short_key = " ".join(normalized)[:60]
        if short_key in seen:
            continue
        seen.add(short_key)

        # Check if this memory fits in the remaining budget.
        # Add 20 tokens overhead per memory for formatting (ID, age, markers).
        mem_tokens = estimate_tokens(content) + 20
        if tokens_used + mem_tokens > max_tokens:
            break

        result.append(mem)
        tokens_used += mem_tokens

    return result


# ---------------------------------------------------------------------------
# Age formatting (shared by memories.py)
# ---------------------------------------------------------------------------

def get_age(iso_date: str) -> str:
    """
    Convert an ISO date string into a human-readable age string.

    Examples:
        "2026-03-24T10:00:00Z"  →  " (just now)"   if < 1 hour ago
        "2026-03-24T08:00:00Z"  →  " (2h ago)"
        "2026-03-22T10:00:00Z"  →  " (2d ago)"
        "2026-03-10T10:00:00Z"  →  " (2w ago)"
    """
    try:
        # Parse ISO 8601 — handle both "Z" suffix and "+00:00" offset
        date_str = iso_date.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str)
        now = datetime.now(tz=timezone.utc)
        delta_seconds = (now - dt).total_seconds()

        if delta_seconds < 0:
            return ""

        hours = int(delta_seconds // 3600)
        if hours < 1:
            return " (just now)"
        if hours < 24:
            return f" ({hours}h ago)"
        days = hours // 24
        if days < 7:
            return f" ({days}d ago)"
        weeks = days // 7
        return f" ({weeks}w ago)"
    except Exception:
        return ""
