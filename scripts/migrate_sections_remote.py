"""Extract section names from book memory content headers on remote DB."""
from __future__ import annotations

import asyncio
import sys

from qmemory.db.client import get_db, query


def extract_section(content: str) -> str | None:
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


async def migrate():
    dry_run = "--dry-run" in sys.argv
    async with get_db() as db:
        print("Fetching book memory IDs...", flush=True)
        mem_ids = await query(db, "SELECT VALUE in FROM relates WHERE type = 'from_book'")
        unique_ids = list(dict.fromkeys(str(mid) for mid in mem_ids)) if mem_ids else []
        print(f"Found {len(unique_ids)} book memories", flush=True)

        updated = 0
        for i, mid in enumerate(unique_ids):
            table, suffix = mid.split(":", 1)
            rows = await query(db, f"SELECT content, section FROM {table}:`{suffix}`")
            if not rows or not isinstance(rows[0], dict):
                continue
            row = rows[0]
            if row.get("section"):
                continue

            section = extract_section(row.get("content", ""))
            if not section:
                continue

            if not dry_run:
                await query(db, f"UPDATE {table}:`{suffix}` SET section = $section", {"section": section})
            updated += 1

            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(unique_ids)} ({updated} updated)", flush=True)

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"\n{prefix}Done: {updated} sections extracted", flush=True)


if __name__ == "__main__":
    asyncio.run(migrate())
