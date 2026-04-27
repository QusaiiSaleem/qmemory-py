"""One-shot migration: rd7ix + 2 user_qusai memories → user_sloped-z7kb4.

Zero-exception cleanup migration.

Source A: qmemory.user_rd7ixusq8902547uz4d2  (8,804 memories, 132 entities, 8,675 relates)
Source B: qmemory.user_qusai                  (cherry-pick 2 real memories from yesterday)
Target:   qmemory.user_sloped-z7kb4           (fresh signup with modern schema)

Migrates only memory + entity + relates. Sessions/messages/tool_calls/metrics
start fresh (same as any normal new signup).
"""

from __future__ import annotations

import asyncio
import os

from surrealdb import AsyncSurreal


SRC_DB = "user_rd7ixusq8902547uz4d2"
SRC_DB_QUSAI = "user_qusai"
DST_DB = "user_sloped-z7kb4"

QUSAI_KEEP_IDS = [
    "memory:mem1777183055108etq",  # VCL Business Case PPTX
    "memory:mem1777182579077vrh",  # qmemory link.py bug report
]

BATCH_MEMORIES = 200
BATCH_ENTITIES = 500
BATCH_RELATES = 500


async def _connect(database: str) -> AsyncSurreal:
    db = AsyncSurreal(os.environ["QMEMORY_SURREAL_URL"])
    await db.signin({
        "username": os.environ["QMEMORY_SURREAL_USER"],
        "password": os.environ["QMEMORY_SURREAL_PASS"],
    })
    await db.use("qmemory", database)
    return db


def _chunked(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


async def _migrate_table(src_db: str, dst_db: str, table: str, batch_size: int, where: str | None = None, is_relation: bool = False) -> int:
    """Read table from src, batch-INSERT into dst preserving record IDs.

    For RELATION-typed tables (like `relates`), pass is_relation=True so
    we use `INSERT RELATION INTO ...` — plain INSERT is rejected with
    'Found record ... which is not a relation' in modern schemas.
    """
    src = await _connect(src_db)
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    rows = await src.query(sql) or []
    await src.close()

    if not rows:
        print(f"  [{table}] source has 0 rows — skipping")
        return 0

    print(f"  [{table}] read {len(rows)} rows from {src_db}, inserting into {dst_db}...")

    insert_kw = "INSERT RELATION INTO" if is_relation else "INSERT INTO"
    dst = await _connect(dst_db)
    inserted = 0
    for chunk in _chunked(rows, batch_size):
        # Preserves the `id` field on each row
        await dst.query(f"{insert_kw} {table} $rows", {"rows": chunk})
        inserted += len(chunk)
        print(f"    inserted {inserted}/{len(rows)}")
    # Verify actual count
    actual = await dst.query(f"SELECT count() AS c FROM {table} GROUP ALL")
    actual_count = actual[0]["c"] if actual else 0
    await dst.close()
    print(f"    table {table} now contains {actual_count} rows in destination")
    return inserted


async def _verify(database: str) -> dict:
    db = await _connect(database)
    out = {}
    for tbl in ["memory", "entity", "relates"]:
        try:
            rows = await db.query(f"SELECT count() AS c FROM {tbl} GROUP ALL")
            out[tbl] = rows[0]["c"] if rows else 0
        except Exception as ex:
            out[tbl] = f"ERR:{ex}"
    await db.close()
    return out


async def main():
    print("=" * 70)
    print(f"Migrating: {SRC_DB} + 2 from {SRC_DB_QUSAI}")
    print(f"   -> {DST_DB}")
    print("=" * 70)

    print("\n--- Step A: rd7ix → new DB (memory, entity, relates) ---")
    mem_a = await _migrate_table(SRC_DB, DST_DB, "memory", BATCH_MEMORIES)
    ent_a = await _migrate_table(SRC_DB, DST_DB, "entity", BATCH_ENTITIES)
    rel_a = await _migrate_table(SRC_DB, DST_DB, "relates", BATCH_RELATES, is_relation=True)

    print("\n--- Step B: cherry-pick 2 real memories from user_qusai ---")
    where_clause = "id IN [" + ", ".join(QUSAI_KEEP_IDS) + "]"
    mem_b = await _migrate_table(
        SRC_DB_QUSAI, DST_DB, "memory", BATCH_MEMORIES, where=where_clause
    )

    print("\n--- Step C: verify counts in destination ---")
    counts = await _verify(DST_DB)
    print(f"  Final counts in {DST_DB}:")
    for tbl, c in counts.items():
        print(f"    {tbl:10s}  {c}")

    print()
    print("=" * 70)
    print("Migration summary:")
    print(f"  memory  inserted: {mem_a + mem_b}  (rd7ix: {mem_a}, user_qusai: {mem_b})")
    print(f"  entity  inserted: {ent_a}")
    print(f"  relates inserted: {rel_a}")
    print(f"  Final memory count: {counts['memory']}")
    expected = mem_a + mem_b
    if counts['memory'] == expected:
        print(f"  ✅ EXPECTED {expected} memories — match")
    else:
        print(f"  ❌ EXPECTED {expected}, GOT {counts['memory']} — investigate")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
