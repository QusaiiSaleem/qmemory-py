"""
Qmemory Auth Helpers — API token generation, hashing, and validation.

Tokens use the format: qm_ak_{32 random hex chars}
  - "qm_ak_" prefix identifies it as a Qmemory API key
  - Tokens are NEVER stored in plaintext — only SHA-256 hashes
  - The prefix (first 10 chars) is stored separately for display

Usage:
    token = generate_api_token()          # Generate new token
    hashed = hash_token(token)            # Hash for storage
    prefix = get_token_prefix(token)      # "qm_ak_abcd" for display
    is_valid = verify_token_format(token) # Check format
"""
from __future__ import annotations

import hashlib
import logging
import secrets

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "qm_ak_"
TOKEN_RANDOM_LENGTH = 32  # 32 hex chars = 16 bytes of randomness


def generate_api_token() -> str:
    """Generate a new API token: qm_ak_{32 random hex chars}."""
    token = f"{TOKEN_PREFIX}{secrets.token_hex(TOKEN_RANDOM_LENGTH // 2)}"
    logger.debug("Generated new API token with prefix %s", token[:10])
    return token


def hash_token(token: str) -> str:
    """SHA-256 hash of the token for secure storage. Never store plaintext."""
    return hashlib.sha256(token.encode()).hexdigest()


def get_token_prefix(token: str) -> str:
    """Extract the display prefix (first 10 chars) shown to users."""
    return token[:10]


def verify_token_format(token: str) -> bool:
    """Check if a string looks like a valid Qmemory API token."""
    if not isinstance(token, str):
        return False
    return (
        token.startswith(TOKEN_PREFIX)
        and len(token) == len(TOKEN_PREFIX) + TOKEN_RANDOM_LENGTH
    )
