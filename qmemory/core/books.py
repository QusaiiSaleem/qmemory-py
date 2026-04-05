"""
Core Books — Hierarchical Book Browsing

Provides 3 levels of book browsing for agents:
  1. list_books()    — All books with chunk counts
  2. list_sections() — Sections of a specific book with chunk counts
  3. read_section()  — Full content of a specific section

All queries use the `relates` edge table (type = 'from_book') to find
memories linked to book entities, and the `section` field on memory
for grouping.
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.db.client import get_db, query

logger = logging.getLogger(__name__)


async def list_books(
    query_text: str | None = None,
    db: Any = None,
) -> dict:
    """
    List all books with their chunk counts.

    Returns:
        {"books": [{"id": "entity:xxx", "name": "...", "chunk_count": N}, ...]}
    """
    async def _run(conn):
        if query_text:
            books = await query(conn, """
                SELECT id, name,
                    count(<-relates<-memory) AS chunk_count
                FROM entity
                WHERE type = 'book'
                    AND string::lowercase(name) CONTAINS string::lowercase($q)
                ORDER BY name
            """, {"q": query_text})
        else:
            books = await query(conn, """
                SELECT id, name,
                    count(<-relates<-memory) AS chunk_count
                FROM entity
                WHERE type = 'book'
                ORDER BY name
            """)

        from qmemory.formatters.response import attach_meta

        if not books or not isinstance(books, list):
            return attach_meta({"books": []}, total_books=0, level="list")

        book_list = [
            {"id": str(b["id"]), "name": b.get("name", ""), "chunk_count": b.get("chunk_count", 0)}
            for b in books if isinstance(b, dict)
        ]
        return attach_meta(
            {"books": book_list},
            actions_context={"type": "books"},
            total_books=len(book_list),
            level="list",
        )

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


async def list_sections(
    book_id: str,
    db: Any = None,
) -> dict:
    """
    List all sections of a specific book with chunk counts.

    Args:
        book_id: The book entity ID, e.g. "entity:ent123abc"

    Returns:
        {"book": "...", "sections": [{"name": "Chapter 1", "chunk_count": 2}, ...]}
    """
    async def _run(conn):
        table, suffix = book_id.split(":", 1)

        # Get book name
        book_rows = await query(conn, f"SELECT name FROM {table}:`{suffix}`")
        book_name = book_rows[0]["name"] if book_rows and isinstance(book_rows[0], dict) else book_id

        # Two-step approach (SurrealDB v3 subqueries can be slow)
        # Step 1: get memory IDs linked to this book
        mem_ids = await query(conn, f"""
            SELECT VALUE in FROM relates
            WHERE type = 'from_book' AND out = {table}:`{suffix}`
        """)
        if not mem_ids:
            return {"book": book_name, "book_id": book_id, "sections": []}

        # Step 2: group by section using direct ID lookup
        id_list = ", ".join(str(mid) for mid in mem_ids)
        sections = await query(conn, f"""
            SELECT section, count() AS chunk_count
            FROM [{id_list}]
            WHERE section IS NOT NONE
            GROUP BY section
            ORDER BY section
        """)

        from qmemory.formatters.response import attach_meta

        if not sections or not isinstance(sections, list):
            return attach_meta(
                {"book": book_name, "book_id": book_id, "sections": []},
                total_sections=0, level="sections",
            )

        section_list = [
            {"name": s.get("section", "Unknown"), "chunk_count": s.get("chunk_count", 0)}
            for s in sections if isinstance(s, dict)
        ]
        return attach_meta(
            {"book": book_name, "book_id": book_id, "sections": section_list},
            actions_context={"type": "books", "book_id": book_id},
            total_sections=len(section_list),
            level="sections",
        )

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


async def read_section(
    book_id: str,
    section: str,
    db: Any = None,
) -> dict:
    """
    Read all chunks from a specific section of a book.

    Args:
        book_id: The book entity ID
        section:  The section name (e.g. "Chapter 1")

    Returns:
        {"book_id": "...", "section": "...", "chunks": [{id, content}, ...]}
    """
    async def _run(conn):
        table, suffix = book_id.split(":", 1)

        # Two-step approach (SurrealDB v3 subqueries can be slow)
        mem_ids = await query(conn, f"""
            SELECT VALUE in FROM relates
            WHERE type = 'from_book' AND out = {table}:`{suffix}`
        """)
        from qmemory.formatters.response import attach_meta

        if not mem_ids:
            return attach_meta(
                {"book_id": book_id, "section": section, "chunks": []},
                total_chunks=0, level="chunks",
            )

        id_list = ", ".join(str(mid) for mid in mem_ids)
        chunks = await query(conn, f"""
            SELECT id, content, salience, created_at
            FROM [{id_list}]
            WHERE is_active = true AND section = $section
            ORDER BY created_at ASC
        """, {"section": section})

        from qmemory.formatters.response import attach_meta

        if not chunks or not isinstance(chunks, list):
            chunks = []

        chunk_list = [
            {"id": str(c["id"]), "content": c.get("content", ""), "salience": c.get("salience", 0)}
            for c in chunks if isinstance(c, dict)
        ]
        return attach_meta(
            {"book_id": book_id, "section": section, "chunks": chunk_list},
            actions_context={"type": "books", "book_id": book_id, "section": section},
            total_chunks=len(chunk_list),
            level="chunks",
        )

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)
