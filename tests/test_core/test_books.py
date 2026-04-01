"""Tests for book browsing — list books, list sections, read section."""
from __future__ import annotations

import pytest

from qmemory.core.books import list_books, list_sections, read_section
from qmemory.db.client import generate_id, query


async def _seed_book(db):
    """Create a book entity with 3 chunks in 2 sections."""
    book_id = generate_id("ent")
    mem1_id = generate_id("mem")
    mem2_id = generate_id("mem")
    mem3_id = generate_id("mem")

    await query(db, f"""
        CREATE entity:`{book_id}` SET
            name = 'The Art of Learning',
            type = 'book',
            created_at = time::now(),
            updated_at = time::now(),
            aliases = []
    """)

    for mem_id, section, content in [
        (mem1_id, "Chapter 1", "[ The Art of Learning > Chapter 1] First chunk of chapter 1."),
        (mem2_id, "Chapter 1", "[ The Art of Learning > Chapter 1] Second chunk of chapter 1."),
        (mem3_id, "Chapter 2", "[ The Art of Learning > Chapter 2] Content about practice."),
    ]:
        await query(db, f"""
            CREATE memory:`{mem_id}` SET
                content = $content,
                section = $section,
                category = 'domain',
                salience = 0.35,
                scope = 'global',
                confidence = 0.95,
                is_active = true,
                linked = true,
                evidence_type = 'observed',
                recall_count = 0,
                embedding = NONE,
                created_at = time::now(),
                updated_at = time::now()
        """, {"content": content, "section": section})

        edge_id = generate_id("rel")
        await query(db, f"""
            RELATE memory:`{mem_id}`->relates->entity:`{book_id}` SET
                id = relates:`{edge_id}`,
                type = 'from_book',
                confidence = 1.0,
                created_by = 'import',
                created_at = time::now()
        """)

    return f"entity:{book_id}"


class TestListBooks:
    async def test_returns_books_with_chunk_counts(self, db):
        book_id = await _seed_book(db)
        result = await list_books(db=db)
        assert len(result["books"]) == 1
        book = result["books"][0]
        assert book["name"] == "The Art of Learning"
        assert book["chunk_count"] == 3
        assert book["id"] == book_id

    async def test_search_filters_by_name(self, db):
        await _seed_book(db)
        result = await list_books(query_text="Art of Learning", db=db)
        assert len(result["books"]) == 1

        result2 = await list_books(query_text="Quantum Physics", db=db)
        assert len(result2["books"]) == 0


class TestListSections:
    async def test_returns_sections_with_chunk_counts(self, db):
        book_id = await _seed_book(db)
        result = await list_sections(book_id=book_id, db=db)
        assert len(result["sections"]) == 2
        names = {s["name"] for s in result["sections"]}
        assert names == {"Chapter 1", "Chapter 2"}
        ch1 = next(s for s in result["sections"] if s["name"] == "Chapter 1")
        assert ch1["chunk_count"] == 2


class TestReadSection:
    async def test_returns_section_content(self, db):
        book_id = await _seed_book(db)
        result = await read_section(book_id=book_id, section="Chapter 1", db=db)
        assert len(result["chunks"]) == 2
        assert all("Chapter 1" in c["content"] for c in result["chunks"])

    async def test_empty_for_nonexistent_section(self, db):
        book_id = await _seed_book(db)
        result = await read_section(book_id=book_id, section="Chapter 99", db=db)
        assert len(result["chunks"]) == 0
