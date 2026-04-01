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

        if not books or not isinstance(books, list):
            return {"books": [], "_nudge": "No books found."}

        return {
            "books": [
                {
                    "id": str(b["id"]),
                    "name": b.get("name", ""),
                    "chunk_count": b.get("chunk_count", 0),
                }
                for b in books
                if isinstance(b, dict)
            ],
            "_nudge": (
                f"Found {len(books)} book(s). "
                "Pick one and call qmemory_books(book_id='entity:xxx') to see its sections."
            ),
        }

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

        if not sections or not isinstance(sections, list):
            return {"book": book_name, "book_id": book_id, "sections": []}

        return {
            "book": book_name,
            "book_id": book_id,
            "sections": [
                {
                    "name": s.get("section", "Unknown"),
                    "chunk_count": s.get("chunk_count", 0),
                }
                for s in sections
                if isinstance(s, dict)
            ],
            "_nudge": (
                f"Book '{book_name}' has {len(sections)} section(s). "
                "Call qmemory_books(book_id='...', section='Chapter 1') to read a section."
            ),
        }

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
        if not mem_ids:
            return {
                "book_id": book_id, "section": section,
                "chunks": [], "_nudge": "No chunks found.",
            }

        id_list = ", ".join(str(mid) for mid in mem_ids)
        chunks = await query(conn, f"""
            SELECT id, content, salience, created_at
            FROM [{id_list}]
            WHERE is_active = true AND section = $section
            ORDER BY created_at ASC
        """, {"section": section})

        if not chunks or not isinstance(chunks, list):
            chunks = []

        return {
            "book_id": book_id,
            "section": section,
            "chunks": [
                {
                    "id": str(c["id"]),
                    "content": c.get("content", ""),
                    "salience": c.get("salience", 0),
                }
                for c in chunks
                if isinstance(c, dict)
            ],
            "_nudge": (
                f"Found {len(chunks)} chunk(s). "
                "Link useful insights: qmemory_link(from_id='memory:xxx', to_id='memory:yyy', type='supports')"
            ),
        }

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)
