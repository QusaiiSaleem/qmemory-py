# Design Spec: qmemory_add_book

**Date**: 2026-04-12
**Status**: Draft
**Author**: Qusai + Claude

## Problem

Qmemory has 71 books with 8,500+ linked ideas — but no way to add new
books. The existing `qmemory_books` tool is read-only (browse books,
list sections, read content). Books were imported from the TypeScript
version and there is no ingestion pipeline in the Python rebuild.

## Solution

A new MCP tool `qmemory_add_book` that lets any Claude agent add books
to the knowledge library in two phases:

1. **Create the book** — agent calls with `title` + optional `author`
2. **Add sections** — agent calls with `book_id` + `section` + `content`

The agent does the intelligent work: reading PDFs (10-20 pages at a
time), identifying sections, rewriting content clearly. The tool just
stores what the agent sends — no chunking, no OCR, no PDF processing.

## Why Agent-Driven (Not Server-Side Processing)

| Factor | Agent-driven | Server-side |
|--------|-------------|-------------|
| PDF reading | Claude reads natively (20 pages/call) | Need pymupdf + OCR libs |
| Arabic support | Claude understands Arabic | Need Arabic OCR pipeline |
| Content quality | Agent rewrites with understanding | Raw extraction with noise |
| Code complexity | ~150 lines (just save to DB) | ~500+ lines (chunker, OCR, parser) |
| Works on Claude.ai | Yes (paste text) | Need file upload pipeline |
| Resumable | Yes (check existing sections) | Need job queue + progress tracking |

## Tool Interface

### Mode 1 — Create Book

No `book_id` provided → creates a new book entity.

**Input:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `title` | string | Yes | — | Book title |
| `author` | string | No | None | Author name (stored as entity alias) |
| `category` | enum | No | "domain" | Memory category for all sections |
| `salience` | float | No | 0.7 | Importance weight (0.0–1.0) |

**Output:**
```json
{
  "action": "CREATED",
  "book_id": "entity:ent1712345678abc",
  "title": "The Lean Startup"
}
```

**Duplicate detection:** If an entity with the same name and
`type='book'` already exists, returns:
```json
{
  "action": "EXISTS",
  "book_id": "entity:ent...",
  "title": "The Lean Startup"
}
```

### Mode 2 — Add Section

`book_id` provided → adds a section to an existing book.

**Input:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `book_id` | string | Yes | — | Entity ID from Mode 1 |
| `section` | string | Yes | — | Section name (e.g. "Chapter 1: Start") |
| `section_index` | integer | Yes | — | Order position (1, 2, 3...) |
| `content` | string | Yes | — | Section content (max 16,000 chars) |

**Output:**
```json
{
  "action": "ADDED",
  "book_id": "entity:ent...",
  "memory_id": "memory:mem1712345679xyz",
  "section": "Chapter 1: Start",
  "section_index": 1
}
```

**Duplicate detection:** If a memory with the same `section` name
already exists linked to this `book_id`, returns:
```json
{
  "action": "SKIPPED",
  "reason": "section already exists",
  "book_id": "entity:ent...",
  "section": "Chapter 1: Start"
}
```

## Data Model

### Mode 1 creates:

**Entity record:**
```
id:              entity:ent{timestamp}{random}
name:            title
type:            "book"
aliases:         [author] if provided, else []
embedding:       Voyage AI vector of title (non-fatal if fails)
created_at:      now()
updated_at:      now()
```

### Mode 2 creates:

**Memory record:**
```
id:              memory:mem{timestamp}{random}
content:         "[{title} > {section}] {content}"
category:        book's category (default "domain")
salience:        book's salience (default 0.7)
source_type:     "from_book"
section:         section name
section_index:   integer order position
evidence_type:   "reported"
linked:          true
is_active:       true
embedding:       Voyage AI vector of content (non-fatal if fails)
created_at:      now()
updated_at:      now()
```

**Relates edge:**
```
memory:{id} -> relates -> entity:{book_id}
  type:              "from_book"   ← MUST be "from_book", NOT default "related"
  relationship_type: "from_book"   ← also set for consistency with link.py
  confidence:        1.0
  created_by:        "agent"
  created_at:        now()
```

**Important:** Do NOT use `link_nodes()` from `link.py` — it only sets
`relationship_type` and leaves `type = "related"` (default). The
`books.py` read queries filter by `WHERE type = 'from_book'`, so the
edge must have `type = "from_book"` explicitly. Write a direct RELATE
query in `add_book.py`.

## Schema Change

Add `section_index` field to the memory table in `schema.surql`:

```sql
DEFINE FIELD section_index ON memory TYPE option<int>;
DEFINE INDEX idx_memory_section_index ON memory FIELDS section_index;
```

Safe to apply — optional field, no impact on existing records.

## Change to qmemory_books (Read Tool)

`list_sections()` in `qmemory/core/books.py` currently orders sections
alphabetically:

```sql
ORDER BY section
```

Change to:

```sql
ORDER BY section_index, section
```

Falls back to alphabetical if `section_index` is NULL (all existing
books). New books get proper ordering.

## Error Handling

| Scenario | Response |
|----------|----------|
| Missing `title` in create mode | Validation error from Pydantic |
| Missing `content` or `section` in add mode | Validation error from Pydantic |
| `book_id` doesn't exist | Error: "book not found: entity:..." |
| Content over 16,000 chars | Validation error: "content too long" |
| Voyage AI embedding fails | Non-fatal: memory saved without embedding |
| DB connection fails | Error propagated through `safe_tool()` |
| Duplicate section name + book | action="SKIPPED", no error |
| Duplicate book title | action="EXISTS", returns existing book_id |

## Tool Description (Agent-Facing)

```
Add a book to your knowledge library in two steps:

STEP 1 — Create the book (once):
  qmemory_add_book(title="Book Name", author="Author Name")
  → Returns book_id. Save this for Step 2.

STEP 2 — Add sections (repeat per section):
  qmemory_add_book(book_id="entity:...", section="Chapter 1",
                    section_index=1, content="...")
  → Saves content as a memory linked to the book.

WORKFLOW for processing a PDF:
  1. Read 10-20 pages at a time
  2. Identify the section/chapter name
  3. Rewrite the content clearly (clean up OCR noise, fix formatting)
  4. Call this tool with book_id + section + section_index + content
  5. Repeat until done

RESUMING after interruption:
  Call qmemory_books(book_id="entity:...") to see which sections
  already exist. Skip those and continue from the next section_index.

DUPLICATE PROTECTION:
  If you add a section with the same name twice, the tool returns
  action="SKIPPED". Safe to retry.

CONTENT GUIDELINES:
  - One section per call (a chapter, a major heading, or ~10 pages)
  - Max 16,000 characters per call
  - Write in the book's original language (Arabic stays Arabic)
  - Include key ideas, not word-for-word transcription
  - The agent's job is to READ and REWRITE — not copy-paste raw text
```

## QMEMORY_INSTRUCTIONS Addition

Add rule #8 to `QMEMORY_INSTRUCTIONS` in `operations.py`:

```
8. ADD BOOKS PROPERLY — use qmemory_add_book in two phases: create
   book first, then add sections one at a time. Read 10-20 pages,
   rewrite clearly, save. Check qmemory_books(book_id) to resume.
```

## Files Changed

| File | Change |
|------|--------|
| `qmemory/core/add_book.py` | **NEW** — create_book() + add_section() |
| `qmemory/mcp/schemas.py` | Add `AddBookInput` Pydantic model |
| `qmemory/mcp/operations.py` | Add `qmemory_add_book` Operation + update QMEMORY_INSTRUCTIONS |
| `qmemory/core/books.py` | Change section ORDER BY to use section_index |
| `schema.surql` | Add section_index field + index |
| `qmemory/db/schema.surql` | Same schema change (mirror) |
| `tests/test_core/test_add_book.py` | **NEW** — tests for create + add + dedup + errors |

## Agent Workflow Example

```
Agent: (reads PDF pages 1-15)
Agent: qmemory_add_book(title="كتاب الأوقاف", author="ابن قدامة")
  → { action: "CREATED", book_id: "entity:ent..." }

Agent: (identifies: Introduction, pages 1-5)
Agent: qmemory_add_book(
  book_id="entity:ent...",
  section="مقدمة",
  section_index=1,
  content="يتناول هذا الكتاب أحكام الوقف في الفقه الإسلامي..."
)
  → { action: "ADDED", memory_id: "memory:mem..." }

Agent: (identifies: Chapter 1, pages 6-15)
Agent: qmemory_add_book(
  book_id="entity:ent...",
  section="الباب الأول: تعريف الوقف",
  section_index=2,
  content="الوقف لغة: الحبس والمنع..."
)
  → { action: "ADDED", memory_id: "memory:mem..." }

--- conversation interrupted ---

Agent: (new session, wants to continue)
Agent: qmemory_books(book_id="entity:ent...")
  → { sections: [
       { name: "مقدمة", section_index: 1 },
       { name: "الباب الأول: تعريف الوقف", section_index: 2 }
     ]}
Agent: (sees sections 1-2 done, reads pages 16-35, continues from section_index=3)
```
