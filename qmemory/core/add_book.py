"""
Core Add Book — Create book entities and add sections.

Two functions:
  1. create_book()  — Create a book entity (or return existing if duplicate)
  2. add_section()  — Add a section to an existing book as a memory + relates edge

Design decisions:
  - No chunking — the agent controls content size.
  - Duplicate detection: same title (case-insensitive) for books,
    same section name + book_id for sections.
  - Relates edge uses type='from_book' directly (NOT via link_nodes())
    because books.py queries WHERE type = 'from_book'.
  - Embedding generation is non-fatal (same as save.py).
"""
from __future__ import annotations

import logging
from typing import Any

from qmemory.core.embeddings import generate_embedding
from qmemory.db.client import generate_id, get_db, query

logger = logging.getLogger(__name__)


async def create_book(
    title: str,
    author: str | None = None,
    category: str = "domain",
    salience: float = 0.7,
    db: Any = None,
) -> dict:
    """
    Create a book entity. Returns existing if a book with the same
    title already exists (case-insensitive).

    Returns:
        {"action": "CREATED"|"EXISTS", "book_id": "entity:...", "title": "..."}
    """

    async def _run(conn: Any) -> dict:
        # --- Check for existing book with same title ---
        existing = await query(conn, """
            SELECT id, name FROM entity
            WHERE type = 'book'
                AND string::lowercase(name) = string::lowercase($title)
            LIMIT 1
        """, {"title": title})

        if existing and isinstance(existing, list) and len(existing) > 0:
            existing_book = existing[0]
            return {
                "action": "EXISTS",
                "book_id": str(existing_book["id"]),
                "title": existing_book.get("name", title),
            }

        # --- Generate entity ID ---
        ent_id = generate_id("ent")

        # --- Generate embedding for the title (non-fatal) ---
        embedding = await generate_embedding(title)

        # --- Build aliases ---
        aliases = [author] if author else []

        # --- Build CREATE query ---
        create_surql = f"""CREATE entity:`{ent_id}` SET
            name = $title,
            type = 'book',
            aliases = $aliases,
            created_at = time::now(),
            updated_at = time::now()"""

        params: dict[str, Any] = {
            "title": title,
            "aliases": aliases,
        }

        if embedding:
            create_surql += ",\n            embedding = $embedding"
            params["embedding"] = embedding

        create_surql += ";"

        await query(conn, create_surql, params)

        book_id = f"entity:{ent_id}"
        logger.info("Created book entity %s: %s", book_id, title)

        return {
            "action": "CREATED",
            "book_id": book_id,
            "title": title,
        }

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)


async def add_section(
    book_id: str,
    section: str,
    section_index: int,
    content: str,
    category: str = "domain",
    salience: float = 0.7,
    db: Any = None,
) -> dict:
    """
    Add a section to an existing book. Creates one memory record and
    one relates edge. Skips if section name already exists for this book.

    Args:
        book_id:       Entity ID (e.g. "entity:ent1712345678abc")
        section:       Section name (e.g. "Chapter 1: Start")
        section_index: Order position (1, 2, 3...)
        content:       Section content text
        category:      Memory category (default "domain")
        salience:      Importance weight (default 0.7)

    Returns:
        {"action": "ADDED"|"SKIPPED", "book_id": "...", ...}

    Raises:
        ValueError: If book_id doesn't exist
    """

    async def _run(conn: Any) -> dict:
        # --- Parse book ID ---
        ent_table, ent_suffix = book_id.split(":", 1)

        # --- Verify book exists ---
        book_rows = await query(conn, f"SELECT name FROM {ent_table}:`{ent_suffix}`")
        if not book_rows or not isinstance(book_rows, list) or len(book_rows) == 0:
            raise ValueError(f"book not found: {book_id}")

        book_name = book_rows[0].get("name", "")

        # --- Check for duplicate section ---
        # Get all memory IDs linked to this book
        mem_ids = await query(conn, f"""
            SELECT VALUE in FROM relates
            WHERE type = 'from_book' AND out = {ent_table}:`{ent_suffix}`
        """)

        if mem_ids:
            id_list = ", ".join(str(mid) for mid in mem_ids)
            existing_sections = await query(conn, f"""
                SELECT id FROM [{id_list}]
                WHERE section = $section AND is_active = true
            """, {"section": section})

            if existing_sections and len(existing_sections) > 0:
                return {
                    "action": "SKIPPED",
                    "reason": "section already exists",
                    "book_id": book_id,
                    "section": section,
                }

        # --- Prefix content with header ---
        prefixed_content = f"[{book_name} > {section}] {content}"

        # --- Generate embedding (non-fatal) ---
        embedding = await generate_embedding(prefixed_content)

        # --- Generate memory ID ---
        mem_id = generate_id("mem")

        # --- CREATE memory ---
        create_surql = f"""CREATE memory:`{mem_id}` SET
            content = $content,
            category = $category,
            salience = $salience,
            scope = 'global',
            confidence = 0.95,
            source_type = 'from_book',
            section = $section,
            section_index = $section_index,
            evidence_type = 'reported',
            linked = true,
            is_active = true,
            recall_count = 0,
            created_at = time::now(),
            updated_at = time::now()"""

        params: dict[str, Any] = {
            "content": prefixed_content,
            "category": category,
            "salience": salience,
            "section": section,
            "section_index": section_index,
        }

        if embedding:
            create_surql += ",\n            embedding = $embedding"
            params["embedding"] = embedding

        create_surql += ";"

        await query(conn, create_surql, params)

        # --- CREATE relates edge ---
        # MUST set type='from_book' directly — books.py queries WHERE type = 'from_book'
        edge_id = generate_id("rel")
        await query(conn, f"""
            RELATE memory:`{mem_id}`->relates->entity:`{ent_suffix}` SET
                id = relates:`{edge_id}`,
                type = 'from_book',
                relationship_type = 'from_book',
                confidence = 1.0,
                created_by = 'agent',
                created_at = time::now()
        """)

        full_mem_id = f"memory:{mem_id}"
        logger.info(
            "Added section '%s' (index=%d) to book %s as %s",
            section, section_index, book_id, full_mem_id,
        )

        return {
            "action": "ADDED",
            "book_id": book_id,
            "memory_id": full_mem_id,
            "section": section,
            "section_index": section_index,
        }

    if db is not None:
        return await _run(db)
    async with get_db() as conn:
        return await _run(conn)
