"""
Shared action builder — generates structured next-step suggestions for agents.

Every qmemory tool response includes an "actions" list. Each action is a
ready-to-use tool call: {"tool": "...", "args": {...}, "reason": "..."}.
The agent can copy-paste these directly instead of parsing a text nudge.
"""
from __future__ import annotations


def build_actions(context: dict) -> list[dict]:
    """Build suggested next-step tool calls based on what just happened.

    Args:
        context: A dict describing the operation result. Keys used:
            - "type": operation type ("search", "save", "correct", "link",
              "person", "get", "books", "bootstrap")
            - "memory_id": ID of the memory just created/modified
            - "entity_id": ID of an entity involved
            - "neighbor_count": how many connections exist
            - "dedup_similar_id": ID of a similar memory found during dedup
            - "from_id" / "to_id": link endpoints
            - "edge_count_from" / "edge_count_to": edge counts per endpoint
            - "book_id": book entity for browsing
            - "section": section name for book browsing
            - "total_memories": count for bootstrap
            - "ids": list of IDs for batch get
            - "include_neighbors": whether neighbors were already fetched
            - "content_preview": short content string for search suggestions

    Returns:
        List of action dicts: [{"tool": "...", "args": {...}, "reason": "..."}]
    """
    actions: list[dict] = []
    op_type = context.get("type", "")

    if op_type == "search":
        # Suggest exploring entities that matched
        if context.get("entity_id"):
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [context["entity_id"]], "include_neighbors": True},
                "reason": "Entity matched your query — explore its memory graph",
            })
        # Suggest fetching neighbors for connected results
        if context.get("memory_id") and context.get("neighbor_count", 0) > 0:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [context["memory_id"]], "include_neighbors": True},
                "reason": f"{context['neighbor_count']} connection(s) to explore",
            })

    elif op_type == "save":
        mid = context.get("memory_id", "")
        if context.get("dedup_similar_id"):
            actions.append({
                "tool": "qmemory_link",
                "args": {
                    "from_id": mid,
                    "to_id": context["dedup_similar_id"],
                    "relationship_type": "related_to",
                },
                "reason": "Similar memory found during dedup — consider linking",
            })
        elif mid:
            actions.append({
                "tool": "qmemory_search",
                "args": {"query": (context.get("content_preview") or "")[:50]},
                "reason": "Find related memories to link with this one",
            })

    elif op_type == "correct":
        new_id = context.get("new_memory_id") or context.get("memory_id", "")
        if new_id:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [new_id], "include_neighbors": True},
                "reason": "Verify corrected memory and its connections",
            })

    elif op_type == "link":
        for endpoint_key, label in [("from_id", "from"), ("to_id", "to")]:
            nid = context.get(endpoint_key)
            count = context.get(f"edge_count_{label}", 0)
            if nid and count > 0:
                actions.append({
                    "tool": "qmemory_get",
                    "args": {"ids": [nid], "include_neighbors": True},
                    "reason": f"Now has {count} connection(s) — explore",
                })

    elif op_type == "person":
        eid = context.get("entity_id", "")
        mem_count = context.get("memory_count", 0)
        if eid:
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": [eid], "include_neighbors": True},
                "reason": (
                    f"{mem_count} memories linked to this person"
                    if mem_count
                    else "Explore person graph"
                ),
            })

    elif op_type == "get":
        # Suggest getting neighbors if not already included
        if context.get("ids") and not context.get("include_neighbors"):
            actions.append({
                "tool": "qmemory_get",
                "args": {"ids": context["ids"], "include_neighbors": True},
                "reason": "Fetch graph neighbors for these nodes",
            })

    elif op_type == "books":
        if context.get("book_id") and not context.get("section"):
            actions.append({
                "tool": "qmemory_books",
                "args": {"book_id": context["book_id"]},
                "reason": "Browse this book's sections",
            })
        elif context.get("book_id") and context.get("section"):
            actions.append({
                "tool": "qmemory_search",
                "args": {
                    "source_type": "from_book",
                    "query": context.get("section", ""),
                },
                "reason": "Find insights from this section",
            })

    elif op_type == "bootstrap":
        total = context.get("total_memories", 0)
        if total > 20:
            actions.append({
                "tool": "qmemory_search",
                "args": {"query": ""},
                "reason": f"{total} memories available — search for specifics",
            })

    return actions
