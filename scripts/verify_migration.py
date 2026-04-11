#!/usr/bin/env python3
"""Compare row counts between qmemory.main and qmemory.user_qusai.

Exits 0 if all counts match, 1 otherwise. Phase 3 gate before
flipping the admin pointer.
"""

from __future__ import annotations

import asyncio
import sys

from qmemory.db.client import get_db, query

TABLES = ["memory", "entity", "relates", "session", "message", "scratchpad"]


async def counts(db_name: str) -> dict[str, int]:
    out: dict[str, int] = {}
    async with get_db(database=db_name) as conn:
        for table in TABLES:
            rows = await query(conn, f"SELECT count() FROM {table} GROUP ALL")
            out[table] = (rows[0]["count"] if rows else 0) if rows else 0
    return out


async def main() -> int:
    print("Counting rows in main...")
    main_counts = await counts("main")
    print("Counting rows in user_qusai...")
    new_counts = await counts("user_qusai")

    ok = True
    for table in TABLES:
        m = main_counts[table]
        n = new_counts[table]
        tag = "OK " if m == n else "FAIL"
        print(f"  {tag}  {table}: main={m}  user_qusai={n}")
        if m != n:
            ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
