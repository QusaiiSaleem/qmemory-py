"""
Shared response helper — ensures every response has actions + meta.

The universal contract: every qmemory tool response includes:
  - "actions": list of structured next-step tool calls (always present)
  - "meta": dict with operation metadata (always present)
"""
from __future__ import annotations

from typing import Any

from qmemory.formatters.actions import build_actions


def attach_meta(
    response: dict,
    *,
    actions_context: dict | None = None,
    **meta_fields: Any,
) -> dict:
    """Ensure a response dict has 'actions' and 'meta' keys.

    Args:
        response:        The tool's response dict (modified in place and returned).
        actions_context: If provided, passed to build_actions() to generate suggestions.
                         If None, actions will be an empty list.
        **meta_fields:   Key-value pairs added to the meta dict.

    Returns:
        The same response dict with 'actions' and 'meta' guaranteed present.
    """
    # Build actions from context, or use empty list
    if "actions" not in response:
        if actions_context:
            response["actions"] = build_actions(actions_context)
        else:
            response["actions"] = []

    # Build meta from kwargs, preserving any existing meta
    if "meta" not in response:
        response["meta"] = {}
    response["meta"].update(meta_fields)

    return response
