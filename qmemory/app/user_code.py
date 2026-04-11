"""
User code generator for /mcp/u/{code}/ URLs.

Format: {word}-{5 lowercase alphanumeric chars}
Example: abacus-k7m3p
"""

from __future__ import annotations

import secrets
import string

from qmemory.app.wordlist import WORDLIST

_SUFFIX_CHARS = string.ascii_lowercase + string.digits


def generate_user_code() -> str:
    """Return a new random user code. Does NOT check uniqueness.

    The suffix's FIRST character is always a lowercase letter. This avoids
    a SurrealQL tokenizer edge case: `DEFINE DATABASE user_name-1abcd` is
    parsed as `user_name - 1abcd` (number token followed by identifier)
    which is a syntax error. A letter-first suffix keeps every user code
    safe to use bare in DB statements.
    """
    word = secrets.choice(WORDLIST)
    first = secrets.choice(string.ascii_lowercase)
    rest = "".join(secrets.choice(_SUFFIX_CHARS) for _ in range(4))
    return f"{word}-{first}{rest}"


async def generate_unique_user_code(max_attempts: int = 10) -> str:
    """Generate a code that does not collide with an existing user row."""
    from qmemory.db.client import get_admin_db, query

    for _ in range(max_attempts):
        code = generate_user_code()
        async with get_admin_db() as db:
            rows = await query(
                db,
                "SELECT id FROM user WHERE user_code = $code",
                {"code": code},
            )
        if not rows:
            return code
    raise RuntimeError(
        f"Could not generate unique user code after {max_attempts} attempts"
    )
