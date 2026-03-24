"""
LLM Provider Package

Entry point for getting an LLM provider instance.

Usage:
    from qmemory.llm import get_llm

    llm = get_llm()           # Default: Haiku (cheapest, fastest)
    llm = get_llm("sonnet")   # Higher quality for complex tasks

The factory pattern lets us:
  - Swap providers without changing calling code
  - Use shorthand names ("haiku", "sonnet") instead of full model IDs
  - Pass custom model strings directly if needed (e.g. get_llm("claude-opus-4-5"))
"""

from qmemory.llm.anthropic_provider import ClaudeProvider


def get_llm(model: str = "haiku") -> ClaudeProvider:
    """
    Create and return an LLM provider instance.

    Args:
        model: Either a shorthand name ("haiku", "sonnet") or a full
               Anthropic model ID string. Shorthands are mapped to the
               latest stable model IDs. Unrecognized strings are passed
               directly to the provider (useful for testing specific versions).

    Returns:
        A ClaudeProvider instance ready to call .complete().

    Examples:
        get_llm()           → ClaudeProvider("claude-haiku-4-5-20251001")
        get_llm("haiku")    → ClaudeProvider("claude-haiku-4-5-20251001")
        get_llm("sonnet")   → ClaudeProvider("claude-sonnet-4-5-20250514")
        get_llm("claude-opus-4-5")  → ClaudeProvider("claude-opus-4-5")
    """
    # Map shorthand names to full Anthropic model IDs.
    # Using a dict makes it easy to update these when new models ship.
    model_map = {
        "haiku":  "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-5-20250514",
    }

    # Look up the shorthand, or use the string directly as a model ID
    resolved_model = model_map.get(model, model)

    return ClaudeProvider(model=resolved_model)
