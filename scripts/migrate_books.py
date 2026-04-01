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
    Clean a raw book entity name — conservative approach.

    Only removes .pdf extensions and libgenli suffixes.
    Does NOT try to rearrange author/title (too error-prone).
    """
    name = raw.strip()

    # Remove file extensions
    name = re.sub(r'\.pdf$', '', name, flags=re.IGNORECASE)
    # Remove libgen download suffixes
    name = re.sub(r'\s*-\s*libgenli$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*-\s*libgen\.li$', '', name, flags=re.IGNORECASE)
    # Remove leading/trailing whitespace and dashes
    name = name.strip().strip('-').strip()

    return name


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

    header = content[1:bracket_end]
    if ">" not in header:
        return None

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
            params: dict = {}

            # Extract section if not already set
            if not row.get("section"):
                section = extract_section(row.get("content", ""))
                if section:
                    updates.append("section = $section")
                    params["section"] = section
                    section_count += 1

            # Fix linked flag
            if not row.get("linked"):
                updates.append("linked = true")
                linked_count += 1

            if updates and not dry_run:
                set_clause = ", ".join(updates)
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
