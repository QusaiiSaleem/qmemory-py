"""
Memory formatting for context injection.

Transforms raw memory dicts into the structured text the agent sees
in its system prompt. Groups by category, adds evidence markers.

These are pure functions — no DB, no async, no side effects.
"""

from __future__ import annotations

from .budget import get_age


# ---------------------------------------------------------------------------
# Category metadata
# ---------------------------------------------------------------------------

# Display order and human-readable labels for each memory category.
# Self-model is handled separately (always injected first, no IDs shown).
CATEGORY_ORDER: list[tuple[str, str]] = [
    ("decision",   "Decisions & Rules"),
    ("preference", "Preferences"),
    ("style",      "Communication Style"),
    ("feedback",   "Corrections"),
    ("context",    "Key Facts"),
    ("idea",       "Plans & Ideas"),
    ("domain",     "Domain Knowledge"),
]


# ---------------------------------------------------------------------------
# Single memory line formatter
# ---------------------------------------------------------------------------

def _format_memory_line(m: dict, *, show_id: bool = True) -> str:
    """
    Format one memory as a single line with evidence markers.

    Layout for normal memories:
        - [mem123] !content [project:alpha] — Qusai reported ⚑0.8 (2d ago)

    Layout for self-model memories (show_id=False):
        - content — self-learned (5d ago), 4× recalled

    Evidence markers:
        - "!" prefix  → salience >= 0.8 (high importance)
        - "⚠︎" prefix  → is_contradicted is True
        - "— name verb" → source attribution (who said it)
        - "⚑0.8"       → confidence (shown when < 1.0)
        - "4× recalled" → recall reinforcement (self memories only)
    """
    content = m.get("content", "")
    category = m.get("category", "context")
    salience = m.get("salience", 0.5)
    scope = m.get("scope", "global")
    confidence = m.get("confidence")
    source_person = m.get("source_person")
    evidence_type = m.get("evidence_type", "")
    recall_count = m.get("recall_count", 0) or 0
    is_contradicted = m.get("is_contradicted", False)
    created_at = m.get("created_at", "")

    # --- Build prefix parts ---

    # Contradiction warning
    contradict_mark = "⚠︎" if is_contradicted else ""

    # High-salience marker
    importance_mark = "!" if salience >= 0.8 else ""

    # --- Build ID + scope prefix (for non-self memories) ---
    id_part = ""
    if show_id:
        raw_id = str(m.get("id", ""))
        short_id = raw_id.replace("memory:", "")
        # Scope: only show when it's not global
        scope_str = f" [{scope}]" if scope and scope != "global" else ""
        id_part = f"[{short_id}]{scope_str} "

    # --- Source attribution ---
    source_mark = ""
    if source_person:
        # Strip SurrealDB record prefix: "entity:p_qusai" → "qusai"
        person_name = str(source_person).replace("entity:", "").lstrip("p_")
        verb = {
            "reported": "reported",
            "inferred": "inferred",
            "self": "stated",
        }.get(evidence_type, "stated")
        source_mark = f" — {person_name} {verb}"
    elif evidence_type == "inferred":
        source_mark = " — inferred"
    elif evidence_type == "self":
        source_mark = " — self-learned"

    # --- Confidence marker (only when < 1.0 and defined) ---
    conf_mark = ""
    if confidence is not None and confidence < 1.0:
        conf_mark = f" ⚑{confidence:.1f}"

    # --- Age ---
    age = get_age(created_at) if created_at else ""

    # --- Recall count (self memories only, when > 1) ---
    recall_mark = ""
    if category == "self" and recall_count > 1:
        recall_mark = f", {recall_count}× recalled"

    return (
        f"- {id_part}{contradict_mark}{importance_mark}{content}"
        f"{source_mark}{conf_mark}{age}{recall_mark}"
    )


# ---------------------------------------------------------------------------
# Main formatter
# ---------------------------------------------------------------------------

def format_memories(
    memories: list[dict],
    include_hypotheses: bool = True,
    include_tools_guide: bool = False,
) -> str:
    """
    Format a list of memories for system prompt injection.

    Layout (in this order):
        1. Agent Self-Model — self-category memories, no IDs, always first
        2. Cross-Session Memory — grouped by category with ## headers
        3. Hypotheses — confidence < 0.5, listed separately at the end
        4. Memory Tools guide (optional, shown on first message only)

    Args:
        memories:             List of memory dicts from the DB.
        include_hypotheses:   Whether to append the Hypotheses section.
        include_tools_guide:  Whether to append the tools reference guide.

    Returns:
        A formatted multi-line string ready for system prompt injection.
        Returns "" if there are no memories and no tools guide requested.
    """
    if not memories and not include_tools_guide:
        return ""

    sections: list[str] = []

    # ---- 1. Self-Model section (always first) ----
    self_memories = [m for m in memories if m.get("category") == "self"]
    if self_memories:
        sections.append("## Agent Self-Model")
        sections.append("_How I work best with this user_")
        for m in self_memories:
            # Self memories shown without IDs — they are identity, not facts
            sections.append(_format_memory_line(m, show_id=False))

    # ---- 2. Cross-Session Memory grouped by category ----
    # Exclude self and hypotheses (confidence < 0.5) from the main section
    main_memories = [
        m for m in memories
        if m.get("category") != "self"
        and not (
            m.get("confidence") is not None
            and m.get("confidence", 1.0) < 0.5
        )
    ]

    # Group by category
    groups: dict[str, list[dict]] = {}
    for m in main_memories:
        cat = m.get("category") or "context"
        groups.setdefault(cat, []).append(m)

    sections.append("")
    sections.append("## Cross-Session Memory")
    sections.append(f"_{len(memories)} memories from all sessions_")

    for cat_key, cat_label in CATEGORY_ORDER:
        items = groups.get(cat_key)
        if not items:
            continue
        sections.append("")
        sections.append(f"### {cat_label}")
        for m in items:
            sections.append(_format_memory_line(m, show_id=True))

    # Any category not in the predefined order (future-proofing)
    known_cats = {k for k, _ in CATEGORY_ORDER} | {"self"}
    for cat_key, items in groups.items():
        if cat_key in known_cats or not items:
            continue
        sections.append("")
        # Capitalise unknown categories for display
        sections.append(f"### {cat_key.capitalize()}")
        for m in items:
            sections.append(_format_memory_line(m, show_id=True))

    # ---- 3. Hypotheses (confidence < 0.5, not self) ----
    if include_hypotheses:
        hypotheses = [
            m for m in memories
            if m.get("category") != "self"
            and m.get("confidence") is not None
            and m.get("confidence", 1.0) < 0.5
        ]
        if hypotheses:
            sections.append("")
            sections.append("### Hypotheses (unconfirmed)")
            for m in hypotheses:
                sections.append(_format_memory_line(m, show_id=True))

    # ---- 4. Tools guide (first message only) ----
    if include_tools_guide:
        sections.extend([
            "",
            "### Memory Tools",
            "- `qmemory_save` — Save facts/decisions/corrections/self-knowledge (auto-dedup)",
            "- `qmemory_search` — Deep search by meaning, category, or scope",
            "- `qmemory_link` — Create relationships between any two things",
            "- `qmemory_correct` — Fix, update, delete, or unlink",
            "- `qmemory_person` — Create/find people with linked contacts",
            "- `qmemory_import` — Import a markdown file into the graph",
        ])

    return "\n".join(sections)
