"""
Qmemory MCP operations table — single source of truth.

All 9 tools are declared here once. Both transports mount these
via registry.mount_operations(). Core business logic lives in
qmemory/core/*; handlers here are thin lambdas that call core
functions and return dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.types import ToolAnnotations
from pydantic import BaseModel

from qmemory.mcp import schemas


# ---------------------------------------------------------------------------
# Server-level instructions sent to clients on initialize.
#
# This is the SINGLE place to encode behavioral rules — both transports
# (stdio and HTTP) import QMEMORY_INSTRUCTIONS from here. Treats memory
# as an active habit, not a passive lookup. Sent ONCE per session at
# `initialize` time, not per tool call, so verbosity is cheap.
#
# When you change this, the change ships with `git push` — every user
# (Qusai, friends, future signups) gets the update on their next session.
# ---------------------------------------------------------------------------

QMEMORY_INSTRUCTIONS = """\
Persistent graph memory shared across every Claude session and platform
(Claude Code, Claude.ai, web, mobile). Memories saved in one chat are
immediately visible in the next. Treat this like a real brain — not a
retrieval cache. The discipline below is non-negotiable.

## Non-negotiable behaviors

1. BOOTSTRAP FIRST. Your first action in EVERY new conversation is
   qmemory_bootstrap(session_key="..."). Do not greet, answer, or call
   any other tool until bootstrap completes. The user will not see this
   call — bootstrap runs silently.

2. SEARCH BEFORE ANSWERING. The moment the user mentions a project,
   person, decision, preference, or past topic — call qmemory_search
   BEFORE responding. Never guess what was discussed; the answer is one
   tool call away. If results are sparse, follow rule 7.

3. SAVE AS YOU GO. After learning ANY decision, preference, fact,
   correction, idea, or domain knowledge — call qmemory_save
   immediately. One fact per memory, one sentence, calibrated salience
   (0.9+ critical / 0.7 important / 0.5 normal / 0.3 low). Dedup is
   automatic. The cost of saving is near zero; the cost of NOT saving
   is permanent forgetting. Stop asking "should I save this?" — save it.

4. LINK WHAT'S CONNECTED. After every save, ask yourself: does this new
   memory support, contradict, expand, or depend on a memory you just
   retrieved? If yes, call qmemory_link with a meaningful
   relationship_type (supports, contradicts, caused_by, depends_on,
   expands_on, blocks, inspired_by, etc.). Raw facts are cheap; graph
   edges are what turn them into a brain.

5. CORRECT, DON'T DUPLICATE. When the user updates or contradicts a
   prior fact, use qmemory_correct (action=correct for content changes,
   action=update for metadata). NEVER save a new conflicting memory
   next to the old one — that leaves the graph with two mutually
   exclusive facts.

6. CREATE PERSON ENTITIES. The first mention of any named human in
   conversation → qmemory_person immediately. This lets future memories
   be scoped to that person via source_person and graph links.

7. FOLLOW THE GRAPH WHEN RESULTS ARE THIN. If qmemory_search returns
   fewer results than expected, OR the top results don't directly
   answer the question, look at each result's graph.related[] and
   graph.entities[] hints. Pick the most relevant linked node and call
   qmemory_get(ids=[that_id], include_neighbors=true, neighbor_depth=2).
   Memory is a network — single-shot search rarely finds everything,
   but two hops almost always do.

## Style

- Operate silently. Never announce "I'm checking memory..." or "I'll
  save that." Just do it. The user can read the diff.
- Never ask permission to save, link, or correct. Decide and act.
- Save memories in whichever language the user used (Arabic, English,
  whatever). Don't translate unless asked.
- For Arabic queries, use Arabic search terms; for English topics, use
  English. BM25 doesn't translate across languages.
- Memories should be one clear fact per row. "I prefer bullet points
  AND I use JetBrains fonts" = two memories.

## Tools (9 total)

- qmemory_bootstrap — load full context (rule 1, every session)
- qmemory_search    — find by free-text query (rule 2)
- qmemory_get       — fetch by ID + traverse graph (rule 7)
- qmemory_save      — record new fact (rule 3)
- qmemory_correct   — fix or supersede existing fact (rule 5)
- qmemory_link      — relate two nodes with a typed edge (rule 4)
- qmemory_person    — create or find a person entity (rule 6)
- qmemory_books     — browse the user's book library hierarchically
- qmemory_health    — read the latest worker health report

Bootstrap. Then act.
"""


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_model: type[BaseModel]
    annotations: ToolAnnotations
    handler: Callable[[Any], Awaitable[dict]]


async def _bootstrap(i: schemas.BootstrapInput) -> dict:
    from qmemory.core.recall import assemble_context
    return await assemble_context(i.session_key)


async def _search(i: schemas.SearchInput) -> dict:
    from qmemory.core.search import search_memories
    return await search_memories(
        query_text=i.query,
        category=i.category,
        scope=i.scope,
        limit=i.limit,
        offset=i.offset,
        after=i.after,
        before=i.before,
        include_tool_calls=i.include_tool_calls,
        source_type=i.source_type,
        entity_id=i.entity_id,
    )


async def _get(i: schemas.GetInput) -> dict:
    from qmemory.core.get import get_memories
    return await get_memories(
        ids=i.ids,
        include_neighbors=i.include_neighbors,
        neighbor_depth=i.neighbor_depth,
    )


async def _save(i: schemas.SaveInput) -> dict:
    from qmemory.core.save import save_memory
    return await save_memory(
        content=i.content,
        category=i.category,
        salience=i.salience,
        scope=i.scope,
        confidence=i.confidence,
        source_person=i.source_person,
        evidence_type=i.evidence_type,
        context_mood=i.context_mood,
    )


async def _correct(i: schemas.CorrectInput) -> dict:
    from qmemory.core.correct import correct_memory
    return await correct_memory(
        memory_id=i.memory_id,
        action=i.action,
        new_content=i.new_content,
        updates=i.updates,
        edge_id=i.edge_id,
        reason=i.reason,
    )


async def _link(i: schemas.LinkInput) -> dict:
    from qmemory.core.link import link_nodes
    return await link_nodes(
        from_id=i.from_id,
        to_id=i.to_id,
        relationship_type=i.relationship_type,
        reason=i.reason,
        confidence=i.confidence,
    )


async def _person(i: schemas.PersonInput) -> dict:
    from qmemory.core.person import create_person
    return await create_person(
        name=i.name,
        aliases=i.aliases,
        contacts=i.contacts,
    )


async def _books(i: schemas.BooksInput) -> dict:
    from qmemory.core.books import list_books, list_sections, read_section

    if i.book_id and i.section:
        return await read_section(book_id=i.book_id, section=i.section)
    if i.book_id:
        return await list_sections(book_id=i.book_id)
    return await list_books(query_text=i.query)


async def _health(i: schemas.HealthInput) -> dict:
    from qmemory.core.health import get_latest_report

    result = await get_latest_report(check=i.check)
    if result is None:
        return {
            "status": "no_report",
            "message": "No health report found. Worker must run first.",
            "actions": [
                {
                    "tool": "shell",
                    "command": "qmemory worker --once",
                    "description": "Generate a health report",
                }
            ],
        }
    return result


OPERATIONS: list[Operation] = [
    Operation(
        name="qmemory_bootstrap",
        description=(
            "Load your full memory context at conversation start. "
            "Returns self-model, cross-session memories grouped by category, "
            "graph map, and session info. Call once at the START of every conversation."
        ),
        input_model=schemas.BootstrapInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_bootstrap,
    ),
    Operation(
        name="qmemory_search",
        description=(
            "Search cross-session memory by meaning, category, or scope. "
            "Returns memories grouped by category with graph context. "
            "Use qmemory_get if you already have memory IDs. "
            "Use qmemory_books to browse book knowledge hierarchically."
        ),
        input_model=schemas.SearchInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_search,
    ),
    Operation(
        name="qmemory_get",
        description=(
            "Fetch memories or entities by ID with optional graph neighbor traversal. "
            "Use qmemory_search if you only have a query text, not IDs."
        ),
        input_model=schemas.GetInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_get,
    ),
    Operation(
        name="qmemory_save",
        description=(
            "Save a fact to cross-session memory with evidence tracking. "
            "Runs deduplication automatically - returns ADD/UPDATE/NOOP action."
        ),
        input_model=schemas.SaveInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=_save,
    ),
    Operation(
        name="qmemory_correct",
        description=(
            "Fix or delete a memory. Soft-delete only - preserves audit trail. "
            "Actions: correct (new version), delete (soft), update (metadata), unlink (remove edge)."
        ),
        input_model=schemas.CorrectInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        handler=_correct,
    ),
    Operation(
        name="qmemory_link",
        description=(
            "Create a relationship edge between any two nodes in the memory graph. "
            "Relationship type is free-form (supports, contradicts, caused_by, etc.)."
        ),
        input_model=schemas.LinkInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_link,
    ),
    Operation(
        name="qmemory_person",
        description=(
            "Create or find a person entity with linked identities across systems "
            "(Telegram, email, WhatsApp, etc.). Returns existing if found."
        ),
        input_model=schemas.PersonInput,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_person,
    ),
    Operation(
        name="qmemory_books",
        description=(
            "Browse books in your knowledge library hierarchically: "
            "list books, see sections, read section. "
            "Use qmemory_search(source_type='from_book') for keyword search across all books."
        ),
        input_model=schemas.BooksInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_books,
    ),
    Operation(
        name="qmemory_health",
        description=(
            "Read the latest worker health report: orphans, stale facts, "
            "missing links, quality issues, coverage gaps. Worker must run first."
        ),
        input_model=schemas.HealthInput,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        handler=_health,
    ),
]
