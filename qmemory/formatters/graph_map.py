"""
Knowledge graph formatting for context injection.

Transforms entity + edge data into a readable "world map" the agent
can use to navigate the graph. Shows nodes grouped by type with their
most important relationships.

These are pure functions — no DB, no async, no side effects.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Type display config
# ---------------------------------------------------------------------------

# Display order for entity types — channels/topics first for navigation.
# Maps internal type name → human-readable section label.
TYPE_LABELS: list[tuple[str, str]] = [
    ("channel",  "Channels"),
    ("topic",    "Topics"),
    ("person",   "People"),
    ("project",  "Projects"),
    ("org",      "Organizations"),
    ("system",   "Systems"),
    ("concept",  "Concepts"),
    ("contact",  "Contacts"),
]

# Max entities shown per type section (keeps the map scannable)
MAX_PER_TYPE = 10

# Max relationship hints shown per entity
MAX_REL_HINTS = 3

# Max books shown in the Library section
MAX_BOOKS = 5


# ---------------------------------------------------------------------------
# Main formatter
# ---------------------------------------------------------------------------

def format_graph_map(
    entities: list[dict],
    edges: list[dict],
    stats: dict | None = None,
) -> str:
    """
    Format entity + edge data as a navigable knowledge graph text.

    Args:
        entities: List of entity dicts. Each should have:
                  - "id"          (str) SurrealDB record ID
                  - "name"        (str) display name
                  - "type"        (str) entity type, e.g. "person", "project"
                  - "total_links" (int, optional) pre-computed connection count
                  - "external_source" (str, optional) external system name
        edges:    List of relationship dicts. Each should have:
                  - "from_node"   (str) record ID of the source node
                  - "to_node"     (str) record ID of the target node
                  - "type"        (str) relationship type label
        stats:    Optional summary dict with keys:
                  - "entities"    (int)
                  - "edges"       (int)
                  - "memories"    (int)
                  - "orphans"     (int) unlinked memory count

    Returns:
        A formatted multi-line string. Returns "" if both inputs are empty.
    """
    if not entities and not edges:
        return ""

    sections: list[str] = ["### Knowledge Graph"]

    # Stats header line (if provided)
    if stats:
        n_entities = stats.get("entities", len(entities))
        n_edges = stats.get("edges", len(edges))
        n_memories = stats.get("memories", 0)
        sections.append(
            f"_{n_entities} entities, {n_edges} relationships, {n_memories} memories_"
        )

    # Group entities by type for section rendering
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        t = e.get("type") or "other"
        by_type.setdefault(t, []).append(e)

    # ---- Books: special compact Library section ----
    books = by_type.get("book", [])
    if books:
        # Sort by connection count descending, show top MAX_BOOKS
        sorted_books = sorted(books, key=lambda b: b.get("total_links", 0), reverse=True)
        sections.append("")
        sections.append(f"**Library ({len(books)} books)**")
        for b in sorted_books[:MAX_BOOKS]:
            links = b.get("total_links", 0)
            name = b.get("name", "Unknown")
            # Truncate long titles
            if len(name) > 60:
                name = name[:57] + "..."
            sections.append(f"- {name} ({links} connections)")
        if len(books) > MAX_BOOKS:
            sections.append(f"- _...and {len(books) - MAX_BOOKS} more_")
        sections.append(
            '_Search book content: qmemory_search({"categories": ["domain"], "query": "book title or topic"})_'
        )

    # ---- Standard entity type sections ----
    known_types = {t for t, _ in TYPE_LABELS} | {"book"}

    for type_key, type_label in TYPE_LABELS:
        items = by_type.get(type_key)
        if not items:
            continue

        sections.append("")
        sections.append(f"**{type_label}**")

        for e in items[:MAX_PER_TYPE]:
            entity_id = str(e.get("id", ""))
            name = e.get("name", "Unknown")
            ext = e.get("external_source", "")
            ext_str = f" ({ext})" if ext else ""

            # Find edges that touch this entity
            related_edges = [
                r for r in edges
                if str(r.get("from_node", "")) == entity_id
                or str(r.get("to_node", "")) == entity_id
            ]

            # Build relationship hints (up to MAX_REL_HINTS)
            rel_hints: list[str] = []
            for r in related_edges[:MAX_REL_HINTS]:
                from_node = str(r.get("from_node", ""))
                to_node = str(r.get("to_node", ""))
                rel_type = r.get("type", "relates")
                # The "other" node is whichever side isn't this entity
                other = to_node if from_node == entity_id else from_node
                # Extract just the name portion from record IDs like "entity:abc"
                other_short = other.split(":", 1)[-1] if ":" in other else other
                rel_hints.append(f"{rel_type} → {other_short}")

            rel_str = ", ".join(rel_hints)
            connections = f" | {rel_str}" if rel_str else ""

            sections.append(f"- {name}{ext_str}{connections}")

    # ---- Fallback: any type not in the predefined list ----
    for type_key, items in by_type.items():
        if type_key in known_types or not items:
            continue
        sections.append("")
        sections.append(f"**{type_key.capitalize()}**")
        for e in items[:5]:
            sections.append(f"- {e.get('name', 'Unknown')}")

    # ---- Orphan nudge ----
    if stats and stats.get("orphans", 0) > 0:
        orphans = stats["orphans"]
        sections.append("")
        sections.append(
            f"_{orphans} memories have no relationships yet. "
            "Use qmemory_link to connect them._"
        )

    return "\n".join(sections)
