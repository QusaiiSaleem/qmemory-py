"""Tests for add_book — create book entity + add sections."""
from __future__ import annotations

import pytest

from qmemory.core.add_book import add_section, create_book
from qmemory.db.client import generate_id, query


class TestCreateBook:
    async def test_creates_book_entity(self, db):
        result = await create_book(title="Test Book", db=db)
        assert result["action"] == "CREATED"
        assert result["book_id"].startswith("entity:")
        assert result["title"] == "Test Book"

    async def test_stores_author_as_alias(self, db):
        result = await create_book(title="Test Book", author="Jane Doe", db=db)
        book_id = result["book_id"]
        table, suffix = book_id.split(":", 1)
        rows = await query(db, f"SELECT aliases FROM {table}:`{suffix}`")
        assert "Jane Doe" in rows[0]["aliases"]

    async def test_duplicate_title_returns_exists(self, db):
        await create_book(title="Duplicate Book", db=db)
        result = await create_book(title="Duplicate Book", db=db)
        assert result["action"] == "EXISTS"
        assert result["book_id"].startswith("entity:")

    async def test_duplicate_check_is_case_insensitive(self, db):
        await create_book(title="My Book", db=db)
        result = await create_book(title="my book", db=db)
        assert result["action"] == "EXISTS"


class TestAddSection:
    async def _make_book(self, db) -> str:
        result = await create_book(title="Section Test Book", db=db)
        return result["book_id"]

    async def test_adds_section_to_book(self, db):
        book_id = await self._make_book(db)
        result = await add_section(
            book_id=book_id,
            section="Chapter 1",
            section_index=1,
            content="This is the first chapter.",
            db=db,
        )
        assert result["action"] == "ADDED"
        assert result["memory_id"].startswith("memory:")
        assert result["section"] == "Chapter 1"
        assert result["section_index"] == 1

    async def test_content_prefixed_with_header(self, db):
        book_id = await self._make_book(db)
        result = await add_section(
            book_id=book_id,
            section="Intro",
            section_index=1,
            content="Hello world.",
            db=db,
        )
        mem_id = result["memory_id"]
        table, suffix = mem_id.split(":", 1)
        rows = await query(db, f"SELECT content FROM {table}:`{suffix}`")
        assert rows[0]["content"].startswith("[Section Test Book > Intro]")
        assert "Hello world." in rows[0]["content"]

    async def test_creates_relates_edge(self, db):
        book_id = await self._make_book(db)
        await add_section(
            book_id=book_id,
            section="Ch1",
            section_index=1,
            content="Content.",
            db=db,
        )
        table, suffix = book_id.split(":", 1)
        edges = await query(db, f"""
            SELECT * FROM relates
            WHERE type = 'from_book' AND out = {table}:`{suffix}`
        """)
        assert len(edges) == 1

    async def test_duplicate_section_returns_skipped(self, db):
        book_id = await self._make_book(db)
        await add_section(
            book_id=book_id,
            section="Chapter 1",
            section_index=1,
            content="First.",
            db=db,
        )
        result = await add_section(
            book_id=book_id,
            section="Chapter 1",
            section_index=1,
            content="Second attempt.",
            db=db,
        )
        assert result["action"] == "SKIPPED"

    async def test_nonexistent_book_raises_error(self, db):
        with pytest.raises(ValueError, match="book not found"):
            await add_section(
                book_id="entity:ent_nonexistent",
                section="Ch1",
                section_index=1,
                content="Content.",
                db=db,
            )

    async def test_memory_has_correct_fields(self, db):
        book_id = await self._make_book(db)
        result = await add_section(
            book_id=book_id,
            section="Ch1",
            section_index=1,
            content="Important idea.",
            category="domain",
            salience=0.8,
            db=db,
        )
        mem_id = result["memory_id"]
        table, suffix = mem_id.split(":", 1)
        rows = await query(db, f"""
            SELECT source_type, section, section_index, linked,
                   evidence_type, category, salience
            FROM {table}:`{suffix}`
        """)
        row = rows[0]
        assert row["source_type"] == "from_book"
        assert row["section"] == "Ch1"
        assert row["section_index"] == 1
        assert row["linked"] is True
        assert row["evidence_type"] == "reported"
        assert row["category"] == "domain"
        assert row["salience"] == 0.8
