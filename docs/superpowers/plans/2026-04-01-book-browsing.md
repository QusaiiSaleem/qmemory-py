# Book Browsing System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give agents a hierarchical way to browse books (books → sections → content) instead of flat chunk search, plus fix `linked: false` and ugly book names.

**Architecture:** New `section` field on memory table extracted from existing `[Book > Section]` content headers. New `qmemory_books` MCP tool with 3 levels of detail. New `qmemory/core/books.py` for all book queries. Migration script to backfill section field, fix linked flags, and clean book entity names.

**Tech Stack:** Python 3.11+, SurrealDB 3.0, FastMCP, pytest-asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `schema.surql` | Modify | Add `section` field to memory table |
| `qmemory/db/schema.surql` | Modify | Same schema change (kept in sync) |
| `qmemory/core/books.py` | Create | `list_books()`, `list_sections()`, `read_section()` |
| `qmemory/mcp/server.py` | Modify | Add `qmemory_books` tool (Tool 8) |
| `qmemory/app/main.py` | Modify | Add `qmemory_books` tool (HTTP transport) |
| `scripts/migrate_books.py` | Create | One-time migration: extract sections, fix linked, clean names |
| `tests/test_core/test_books.py` | Create | Tests for all 3 browse levels |

---

### Task 1: Add `section` field to schema

**Files:**
- Modify: `schema.surql:53-71` (memory table fields)
- Modify: `qmemory/db/schema.surql` (same change)

- [ ] **Step 1: Add section field to schema.surql**

After line 68 (`context_mood`), add:

```sql
DEFINE FIELD IF NOT EXISTS section      ON memory TYPE option<string>;
```

And add an index after the existing indexes (after line 84):

```sql
DEFINE INDEX IF NOT EXISTS idx_memory_section ON memory FIELDS section;
```

Apply the same two lines to `qmemory/db/schema.surql` at the matching positions.

- [ ] **Step 2: Apply schema to local DB**

Run: `uv run qmemory schema`
Expected: Schema applied successfully (no errors). The `section` field now exists on memory table.

- [ ] **Step 3: Commit**

```bash
git add schema.surql qmemory/db/schema.surql
git commit -m "schema: add section field to memory table for book browsing"
```

---

### Task 2: Core book browsing logic

**Files:**
- Create: `qmemory/core/books.py`
- Test: `tests/test_core/test_books.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_core/test_books.py`:

```python
"""Tests for book browsing — list books, list sections, read section."""
from __future__ import annotations

import pytest

from qmemory.core.books import list_books, list_sections, read_section
from qmemory.db.client import generate_id, query


# ---------------------------------------------------------------------------
# Helper: seed a book entity + 3 memory chunks with sections
# ---------------------------------------------------------------------------

async def _seed_book(db):
    """Create a book entity with 3 chunks in 2 sections."""
    book_id = generate_id("ent")
    mem1_id = generate_id("mem")
    mem2_id = generate_id("mem")
    mem3_id = generate_id("mem")

    # Create book entity
    await query(db, f"""
        CREATE entity:`{book_id}` SET
            name = 'The Art of Learning',
            type = 'book',
            created_at = time::now(),
            updated_at = time::now(),
            aliases = []
    """)

    # Create 3 memory chunks with section headers
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

        # Create from_book edge: memory -> entity
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

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
        result = await list_books(query="Art of Learning", db=db)
        assert len(result["books"]) == 1

        result2 = await list_books(query="Nonexistent Book", db=db)
        assert len(result2["books"]) == 0


class TestListSections:
    async def test_returns_sections_with_chunk_counts(self, db):
        book_id = await _seed_book(db)
        result = await list_sections(book_id=book_id, db=db)
        assert len(result["sections"]) == 2
        # Sections should have names and chunk counts
        names = {s["name"] for s in result["sections"]}
        assert names == {"Chapter 1", "Chapter 2"}
        # Chapter 1 has 2 chunks
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core/test_books.py -v`
Expected: ImportError — `qmemory.core.books` does not exist yet.

- [ ] **Step 3: Implement books.py**

Create `qmemory/core/books.py`:

```python
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
        # Find all book entities and count their linked memories
        # Edge direction: memory ->relates[from_book]-> entity
        # So entity is the `out` side, memory is the `in` side
        if query_text:
            books = await query(conn, """
                SELECT id, name,
                    count(<-relates<-memory) AS chunk_count
                FROM entity
                WHERE type = 'book' AND name @@ $q
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

        # Find all memory chunks linked to this book, grouped by section
        sections = await query(conn, f"""
            SELECT section, count() AS chunk_count
            FROM memory
            WHERE id IN (
                SELECT VALUE in FROM relates
                WHERE type = 'from_book' AND out = {table}:`{suffix}`
            )
            AND is_active = true
            AND section IS NOT NONE
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

        chunks = await query(conn, f"""
            SELECT id, content, salience, created_at
            FROM memory
            WHERE id IN (
                SELECT VALUE in FROM relates
                WHERE type = 'from_book' AND out = {table}:`{suffix}`
            )
            AND is_active = true
            AND section = $section
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core/test_books.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add qmemory/core/books.py tests/test_core/test_books.py
git commit -m "feat: add book browsing core — list_books, list_sections, read_section"
```

---

### Task 3: Add `qmemory_books` MCP tool (both transports)

**Files:**
- Modify: `qmemory/mcp/server.py` (add Tool 8 after qmemory_import)
- Modify: `qmemory/app/main.py` (add matching Tool 8)

- [ ] **Step 1: Add tool to stdio server (mcp/server.py)**

Add after the `qmemory_import` tool (after line 408):

```python
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
    import json

    from qmemory.core.books import list_books, list_sections, read_section

    if book_id and section:
        result = await read_section(book_id=book_id, section=section)
    elif book_id:
        result = await list_sections(book_id=book_id)
    else:
        result = await list_books(query_text=query)

    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 2: Add matching tool to HTTP server (app/main.py)**

Add after the `qmemory_import` tool (after line 421), same logic but with logging:

```python
# ---------------------------------------------------------------------------
# Tool 8: qmemory_books (read-only)
# ---------------------------------------------------------------------------


@mcp.tool()
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
    start = time.monotonic()
    logger.info(
        "Tool call: qmemory_books(book_id=%s, section=%s, query=%s)",
        book_id, section, query,
    )

    from qmemory.core.books import list_books, list_sections, read_section

    if book_id and section:
        result = await read_section(book_id=book_id, section=section)
    elif book_id:
        result = await list_sections(book_id=book_id)
    else:
        result = await list_books(query_text=query)

    elapsed = time.monotonic() - start
    logger.info("qmemory_books completed in %.2fs", elapsed)
    return json.dumps(result, default=str, ensure_ascii=False)
```

- [ ] **Step 3: Run existing tests to make sure nothing broke**

Run: `uv run pytest tests/ -x -q`
Expected: All existing tests still pass.

- [ ] **Step 4: Commit**

```bash
git add qmemory/mcp/server.py qmemory/app/main.py
git commit -m "feat: add qmemory_books MCP tool — hierarchical book browsing"
```

---

### Task 4: Migration script — extract sections, fix linked, clean names

**Files:**
- Create: `scripts/migrate_books.py`

This is the one-time script that backfills the `section` field on existing 8,568 book memories, fixes `linked: false`, and cleans up ugly book entity names.

- [ ] **Step 1: Create the migration script**

Create `scripts/migrate_books.py`:

```python
"""
One-time migration: backfill book data for hierarchical browsing.

Three fixes:
  1. Extract section names from content headers → memory.section field
  2. Fix linked=false → true for all book-linked memories
  3. Clean book entity names (remove .pdf, trim whitespace)

Run:
    uv run python scripts/migrate_books.py
    uv run python scripts/migrate_books.py --dry-run   # preview only
"""
from __future__ import annotations

import asyncio
import re
import sys

from qmemory.db.client import get_db, query


def clean_book_name(raw: str) -> str:
    """
    Clean a raw book entity name.

    Examples:
        " Peter Hollins - The Science of Rapid Skill Acquisition 2019 Pkcs Media Inc - libgenli.pdf"
        → "The Science of Rapid Skill Acquisition — Peter Hollins (2019)"

        "115.pdf" → "115" (can't parse further)

        "Transforming Teaching and Learning Through Data-Driven Decision Making"
        → unchanged (already clean)
    """
    name = raw.strip()

    # Remove file extensions
    name = re.sub(r'\.pdf$', '', name, flags=re.IGNORECASE)
    name = re.sub(r' - libgenli$', '', name, flags=re.IGNORECASE)
    name = re.sub(r' - libgen\.li$', '', name, flags=re.IGNORECASE)

    # Try to parse "Author - Title Year Publisher" pattern
    # Pattern: "Author Name - Book Title YYYY Publisher"
    match = re.match(
        r'^(.+?)\s*-\s*(.+?)\s+((?:19|20)\d{2})\s+(.+)$',
        name,
    )
    if match:
        author, title, year, _publisher = match.groups()
        return f"{title.strip()} — {author.strip()} ({year})"

    # Try simpler "Author - Title" pattern
    match2 = re.match(r'^(.+?)\s*-\s*(.+)$', name)
    if match2:
        author, title = match2.groups()
        return f"{title.strip()} — {author.strip()}"

    return name.strip()


def extract_section(content: str) -> str | None:
    """
    Extract section name from content header.

    Content format: "[ Book Name > Section Title] actual content..."
    Returns: "Section Title" or None if no header found.
    """
    if not content.startswith("["):
        return None

    bracket_end = content.find("]")
    if bracket_end == -1:
        return None

    header = content[1:bracket_end]  # Remove [ and ]
    if ">" not in header:
        return None

    # Section is everything after the last >
    section = header.split(">")[-1].strip()
    return section if section else None


async def migrate(dry_run: bool = False):
    """Run the full migration."""
    async with get_db() as db:
        # --- Step 1: Get all book-linked memory IDs ---
        print("Fetching book-linked memories...")
        mem_ids = await query(db, "SELECT VALUE in FROM relates WHERE type = 'from_book'")
        if not mem_ids:
            print("No book memories found.")
            return

        unique_ids = list(dict.fromkeys(str(mid) for mid in mem_ids))
        print(f"Found {len(unique_ids)} unique book memories.")

        # --- Step 2: Extract sections and update memories ---
        section_count = 0
        linked_count = 0

        for i, mem_id in enumerate(unique_ids):
            table, suffix = mem_id.split(":", 1)
            rows = await query(db, f"SELECT content, section, linked FROM {table}:`{suffix}`")
            if not rows or not isinstance(rows[0], dict):
                continue

            row = rows[0]
            updates = []

            # Extract section if not already set
            if not row.get("section"):
                section = extract_section(row.get("content", ""))
                if section:
                    updates.append(f"section = $section")
                    section_count += 1

            # Fix linked flag
            if not row.get("linked"):
                updates.append("linked = true")
                linked_count += 1

            if updates and not dry_run:
                set_clause = ", ".join(updates)
                params = {}
                section = extract_section(row.get("content", ""))
                if section:
                    params["section"] = section
                await query(db, f"UPDATE {table}:`{suffix}` SET {set_clause}", params)

            if (i + 1) % 500 == 0:
                print(f"  Processed {i + 1}/{len(unique_ids)} memories...")

        print(f"Sections extracted: {section_count}")
        print(f"Linked flags fixed: {linked_count}")

        # --- Step 3: Clean book entity names ---
        print("\nCleaning book entity names...")
        books = await query(db, "SELECT id, name FROM entity WHERE type = 'book'")
        name_count = 0

        if books:
            for book in books:
                if not isinstance(book, dict):
                    continue
                old_name = book.get("name", "")
                new_name = clean_book_name(old_name)
                if new_name != old_name:
                    bid = str(book["id"])
                    btable, bsuffix = bid.split(":", 1)
                    print(f"  '{old_name[:60]}' → '{new_name[:60]}'")
                    if not dry_run:
                        await query(
                            db,
                            f"UPDATE {btable}:`{bsuffix}` SET name = $name",
                            {"name": new_name},
                        )
                    name_count += 1

        print(f"Book names cleaned: {name_count}")

        # --- Summary ---
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"\n{prefix}Migration complete:")
        print(f"  Sections extracted: {section_count}")
        print(f"  Linked flags fixed: {linked_count}")
        print(f"  Book names cleaned: {name_count}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    asyncio.run(migrate(dry_run=dry_run))
```

- [ ] **Step 2: Test with dry run**

Run: `uv run python scripts/migrate_books.py --dry-run`
Expected: Shows counts of what WOULD change, no actual DB modifications.

- [ ] **Step 3: Run the actual migration**

Run: `uv run python scripts/migrate_books.py`
Expected: Updates ~8,568 memories with section names, fixes linked flags, cleans book names.

- [ ] **Step 4: Verify migration worked**

Run a quick check:
```bash
uv run python3 -c "
import asyncio
from qmemory.db.client import get_db, query
async def check():
    async with get_db() as db:
        # Check sections exist
        r = await query(db, 'SELECT count() FROM memory WHERE section IS NOT NONE GROUP ALL')
        print(f'Memories with section: {r}')
        # Check linked fixed
        r2 = await query(db, \"\"\"
            SELECT count() FROM memory
            WHERE linked = true
            AND id IN (SELECT VALUE in FROM relates WHERE type = 'from_book')
            GROUP ALL
        \"\"\")
        print(f'Book memories with linked=true: {r2}')
        # Check clean names
        r3 = await query(db, 'SELECT name FROM entity WHERE type = \"book\" LIMIT 3')
        print(f'Sample book names: {[b[\"name\"] for b in r3]}')
asyncio.run(check())
"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/migrate_books.py
git commit -m "feat: migration script — extract sections, fix linked, clean book names"
```

---

### Task 5: Update CLAUDE.md with new tool

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add qmemory_books to the MCP tools table**

In CLAUDE.md, update the MCP Tools table (add row after qmemory_import):

```markdown
| `qmemory_books` | Yes | Browse books: list books → sections → content |
```

Change "7 tools" references to "8 tools" in CLAUDE.md and in the MCP server docstrings.

- [ ] **Step 2: Add Book Browsing section after Book Knowledge section**

```markdown
## Book Browsing

Agents browse books hierarchically instead of flat search:

- **List all books**: `qmemory_books()` or `qmemory_books(query="learning")`
- **See sections**: `qmemory_books(book_id="entity:xxx")`
- **Read section**: `qmemory_books(book_id="entity:xxx", section="Chapter 1")`
- **Link to memory**: `qmemory_link(from_id="memory:chunk", to_id="memory:note", type="supports")`
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add qmemory_books tool and book browsing docs"
```

---

### Task 6: Run full test suite and verify

- [ ] **Step 1: Run all tests**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (existing 139 + new 5 = 144). The 9 known-failing tests may still fail (SurrealDB v3 edge syntax issue — unrelated to this change).

- [ ] **Step 2: Manual smoke test via MCP**

Test the 3 levels by calling the MCP tool directly:

```
qmemory_books()                              # Level 1: list all books
qmemory_books(book_id="entity:ent...")        # Level 2: sections
qmemory_books(book_id="entity:ent...", section="Chapter 1")  # Level 3: content
```

- [ ] **Step 3: Deploy schema to Railway**

```bash
surreal import -e "https://surrealdb-production-d9ea.up.railway.app" \
  -u root -p "$SURREAL_PASS" --namespace qmemory --database main schema.surql
```

Then run the migration against Railway DB (update env vars to point to Railway first).
