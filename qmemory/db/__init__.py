"""
Qmemory Database Layer

Re-exports the key functions so other modules can import from qmemory.db directly:

    from qmemory.db import get_db, query, is_healthy, generate_id, apply_schema
"""

from qmemory.db.client import (
    apply_schema,
    generate_id,
    get_db,
    is_healthy,
    normalize_ids,
    query,
    query_multi,
)

__all__ = [
    "apply_schema",
    "generate_id",
    "get_db",
    "is_healthy",
    "normalize_ids",
    "query",
    "query_multi",
]
