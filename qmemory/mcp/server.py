"""
Qmemory MCP Server (Python)

FastMCP server that exposes 7 Qmemory tools for Claude Code and Claude.ai.
Each tool is a thin wrapper — all business logic lives in qmemory/core/.

7 tools:
  qmemory_bootstrap  — Load full memory context at conversation start
  qmemory_search     — 4-tier recall with graph connection hints
  qmemory_save       — Save a fact with evidence tracking + dedup
  qmemory_correct    — Fix, delete, update, or unlink a memory
  qmemory_link       — Create a relationship edge between any two nodes
  qmemory_person     — Create or find a person with linked identities
  qmemory_import     — Import a markdown file into the graph (stub for now)

Read-only tools (qmemory_bootstrap, qmemory_search) are annotated with
readOnlyHint=True so MCP clients know they don't modify any state.

Transport: stdio (Claude Code) or HTTP (Claude.ai)
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# ---------------------------------------------------------------------------
# Create the MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Qmemory",
    instructions=(
        "Graph memory for AI agents. "
        "Call qmemory_bootstrap first to load your full memory context. "
        "Then use qmemory_search to find specific memories, qmemory_save to "
        "record new facts, qmemory_correct to fix errors, qmemory_link to "
        "create relationships between knowledge nodes, and qmemory_person to "
        "manage person entities."
    ),
)

# ---------------------------------------------------------------------------
# Tool 1: qmemory_bootstrap
# Read-only — loads context, does not write anything.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_bootstrap(session_key: str = "default") -> str:
    """Load your full memory context for this session.

    Call this at the START of every conversation to remember who you are
    and what you know. Returns your self-model, cross-session memories
    grouped by category, graph map, and session info.

    Args:
        session_key: Identifies this session context. Use the channel/topic
                     name if available (e.g. "telegram/topic:7"), otherwise
                     leave as "default".

    Returns a formatted text block injected into your context window.
    """
    from qmemory.core.recall import assemble_context

    result = await assemble_context(session_key)
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 2: qmemory_search
# Read-only — retrieves memories without modifying any state.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_search(
    query: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    offset: int = 0,
    after: str | None = None,
    before: str | None = None,
    include_tool_calls: bool = False,
    source_type: str | None = None,
    entity_id: str | None = None,
) -> str:
    """Search cross-session memory by meaning, category, or scope.

    Returns memories from ALL past conversations, grouped by category,
    with graph context and structured next-step actions.

    Args:
        query:             Free-text search query (multi-leg BM25).
                           Leave empty to get recent memories without text search.
        category:          Filter to one category (HARD filter — excludes others):
                           self, style, preference, context, decision,
                           idea, feedback, domain
        scope:             Filter visibility: global, project:xxx, topic:xxx
        limit:             Max results to return (default 10, max 50).
        offset:            Skip first N results for pagination (default 0).
        after:             Only return memories created after this date.
                           ISO date string, e.g. "2026-04-01".
        before:            Only return memories created before this date.
        include_tool_calls: Also search past tool call history (default False).
        source_type:       Filter by relation type pointing to the memory.
                           E.g. "from_book" returns only memories extracted from books.
        entity_id:         Scope search to memories linked to this entity.
                           E.g. "entity:ent123abc" — only returns memories about that person/concept.

    Returns JSON with dynamic sections:
      entities_matched — matched people/concepts with actions
      pinned — high-salience memories (>= 0.9)
      memories.{category} — grouped by category, ranked by relevance
      book_insights — memories linked to books
      hypotheses — low-confidence memories needing verification
      actions — suggested next steps
      meta — counts, sections list, search leg breakdown
    """
    from qmemory.core.search import search_memories

    results = await search_memories(
        query_text=query,
        category=category,
        scope=scope,
        limit=limit,
        offset=offset,
        after=after,
        before=before,
        include_tool_calls=include_tool_calls,
        source_type=source_type,
        entity_id=entity_id,
    )
    return json.dumps(results, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 2b: qmemory_get
# Read-only — fetches memories/entities by ID with optional graph traversal.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_get(
    ids: list[str],
    include_neighbors: bool = False,
    neighbor_depth: int = 1,
) -> str:
    """Fetch memories or entities by ID with optional graph neighbor traversal.

    Use this to:
    - Retrieve specific memories when you have their IDs
    - Explore the graph by following connections from search results
    - Verify that saved memories exist

    Args:
        ids:                List of record IDs to fetch.
                            Examples: ["memory:mem123abc", "entity:ent456xyz"]
                            Max 20 IDs per call.
        include_neighbors:  If True, also fetch connected nodes for each result.
                            Shows what each memory is linked to in the graph.
        neighbor_depth:     How deep to traverse connections (1 or 2). Default 1.

    Returns JSON with {memories, not_found, actions, meta}.
    """
    from qmemory.core.get import get_memories

    result = await get_memories(
        ids=ids,
        include_neighbors=include_neighbors,
        neighbor_depth=neighbor_depth,
    )
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 3: qmemory_save
# Writes a new memory node (or updates an existing one via dedup).
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def qmemory_save(
    content: str,
    category: str,
    salience: float = 0.5,
    scope: str = "global",
    confidence: float = 0.8,
    source_person: str | None = None,
    evidence_type: str = "observed",
    context_mood: str | None = None,
) -> str:
    """Save a fact to cross-session memory with evidence tracking.

    Runs deduplication automatically — if a similar memory exists it will
    UPDATE or NOOP instead of creating a duplicate. Returns the action taken.

    Args:
        content:       The fact to remember. One clear statement.
                       Example: "Qusai prefers concise bullet points over paragraphs"
        category:      What type of fact this is:
                       self       — what the agent knows about itself
                       style      — communication preferences (tone, format)
                       preference — general user preferences
                       context    — facts about projects, orgs, situations
                       decision   — past decisions made, with rationale
                       idea       — future plans or proposals
                       feedback   — user corrections and error reports
                       domain     — sector/domain knowledge
        salience:      Importance 0.0-1.0. High-salience memories are recalled first.
                       0.9+ = critical, 0.7 = important, 0.5 = normal, 0.3 = low
        scope:         Who can see this: global | project:xxx | topic:xxx
        confidence:    How certain are you? 0.0-1.0. Use < 0.5 for hypotheses.
        source_person: Who said this? Pass entity ID (e.g. "ent1234abc") if known.
        evidence_type: How was this learned?
                       observed  — you witnessed it directly
                       reported  — someone told you
                       inferred  — you deduced it
                       self      — the agent learned this about itself
        context_mood:  Situation when this was learned:
                       calm_decision | heated_discussion | brainstorm |
                       correction | casual | urgent

    Returns JSON with action (ADD/UPDATE/NOOP), memory_id, and a nudge
    suggesting which nearby memories to link with qmemory_link.
    """
    from qmemory.core.save import save_memory

    result = await save_memory(
        content=content,
        category=category,
        salience=salience,
        scope=scope,
        confidence=confidence,
        source_person=source_person,
        evidence_type=evidence_type,
        context_mood=context_mood,
    )
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 4: qmemory_correct
# Modifies or removes an existing memory.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def qmemory_correct(
    memory_id: str,
    action: str,
    new_content: str | None = None,
    updates: dict | None = None,
    edge_id: str | None = None,
    reason: str | None = None,
) -> str:
    """Fix or delete a memory. Preserves full audit trail via soft-delete.

    We NEVER hard-delete memories — soft-delete only (is_active = false).
    The "correct" action creates a version chain (prev_version edge) so you
    can always trace back through a fact's history.

    Args:
        memory_id:   Full record ID, e.g. "memory:mem1710864000000abc".
                     Get this from qmemory_search results.
        action:      What to do:
                     correct — Replace content. Creates a new version, soft-deletes old.
                               Requires new_content.
                     delete  — Soft-delete only. Sets is_active=false. Fact stays in DB.
                     update  — Change metadata (salience, scope, confidence, etc.)
                               without creating a new version. Requires updates dict.
                     unlink  — Remove a relates edge (not the node). Requires edge_id.
        new_content: The corrected fact text. Required when action="correct".
        updates:     Metadata fields to change. Required when action="update".
                     Allowed keys: salience, scope, valid_until, category, confidence.
                     Example: {"salience": 0.9, "scope": "project:qmemory"}
        edge_id:     The relates edge ID to delete. Required when action="unlink".
        reason:      Optional note explaining why this change was made.

    Returns JSON with ok (bool) and details about what changed.
    """
    from qmemory.core.correct import correct_memory

    result = await correct_memory(
        memory_id=memory_id,
        action=action,
        new_content=new_content,
        updates=updates,
        edge_id=edge_id,
        reason=reason,
    )
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5: qmemory_link
# Creates relationship edges — the core of the knowledge graph.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_link(
    from_id: str,
    to_id: str,
    relationship_type: str,
    reason: str | None = None,
    confidence: float | None = None,
) -> str:
    """Create a relationship edge between any two nodes in the memory graph.

    This is what turns a flat list of facts into a connected knowledge graph.
    The relationship type can be ANYTHING meaningful — there is no fixed list.
    Choose a type that describes WHY these two things are connected.

    Args:
        from_id:           Source node ID. Examples:
                           "memory:mem1710864000000abc"
                           "entity:ent1710864000000xyz"
                           "session:ses1710864000000def"
        to_id:             Target node ID. Can be a different table type.
        relationship_type: Any string. Examples:
                           supports, contradicts, caused_by, depends_on,
                           belongs_to_topic, has_identity, related_to,
                           blocks, inspired_by, summarizes, expands_on
        reason:            Optional note explaining why this connection exists.
        confidence:        How confident are you in this connection? 0.0-1.0.

    Returns JSON with edge_id and an exploration nudge, or null if either
    node doesn't exist.
    """
    from qmemory.core.link import link_nodes

    result = await link_nodes(
        from_id=from_id,
        to_id=to_id,
        relationship_type=relationship_type,
        reason=reason,
        confidence=confidence,
    )
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 6: qmemory_person
# Creates person entities with multiple contact identities.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_person(
    name: str,
    aliases: list[str] | None = None,
    contacts: list[dict] | None = None,
) -> str:
    """Create or find a person entity with linked identities across systems.

    A person can have multiple contact identities — Telegram, WhatsApp,
    email, etc. — all linked via has_identity edges. If a person with this
    name already exists, returns the existing record (no duplicate created).

    Args:
        name:     The person's display name. Example: "Ahmed Al-Rashid"
        aliases:  Optional alternative names or nicknames.
                  Example: ["Ahmed", "Abu Omar"]
        contacts: Optional list of contact identities. Each dict needs:
                  - system:  "telegram", "whatsapp", "email", "smartsheet"
                  - handle:  The identifier in that system (username, email, ID)
                  Example: [
                    {"system": "telegram", "handle": "@ahmed_rashid"},
                    {"system": "email", "handle": "ahmed@example.com"}
                  ]

    Returns JSON with entity_id, contact_ids, links_created, and
    action ("created" or "found").
    """
    from qmemory.core.person import create_person

    result = await create_person(
        name=name,
        aliases=aliases,
        contacts=contacts,
    )
    return json.dumps(result, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 7: qmemory_import
# Stub — full implementation coming in Task 22.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
async def qmemory_import(file_path: str) -> str:
    """Import a markdown file into the memory graph.

    Parses a .md file and creates memory nodes from its content.
    Useful for migrating notes, documents, or existing knowledge bases.

    Args:
        file_path: Absolute path to the markdown file to import.
                   Example: "/Users/qusai/notes/project-context.md"

    Note: Full implementation coming in a future update.
    """
    # Stub — will be implemented in Task 22
    return json.dumps(
        {
            "status": "not_implemented",
            "message": "Import is coming in a future update (Task 22).",
            "file_path": file_path,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Tool 8: qmemory_books
# Read-only — browse books hierarchically.
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
async def qmemory_books(
    book_id: str | None = None,
    section: str | None = None,
    query: str | None = None,
) -> str:
    """Browse books in your knowledge library — 3 levels of detail.

    Level 1 — List all books:
        qmemory_books()
        qmemory_books(query="learning")    # search by name

    Level 2 — See a book's sections:
        qmemory_books(book_id="entity:ent123abc")

    Level 3 — Read a section's content:
        qmemory_books(book_id="entity:ent123abc", section="Chapter 1")

    Args:
        book_id:  Book entity ID. Omit to list all books.
        section:  Section name within a book. Requires book_id.
        query:    Search books by name (only used when book_id is omitted).

    Returns JSON with books, sections, or chunks depending on the level.
    """
    from qmemory.core.books import list_books, list_sections, read_section

    if book_id and section:
        result = await read_section(book_id=book_id, section=section)
    elif book_id:
        result = await list_sections(book_id=book_id)
    else:
        result = await list_books(query_text=query)

    return json.dumps(result, default=str, ensure_ascii=False)
