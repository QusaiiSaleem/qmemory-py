"""Tests for user code generation."""

from __future__ import annotations

import re

from qmemory.app.user_code import generate_user_code
from qmemory.app.wordlist import WORDLIST


def test_wordlist_loaded_with_thousands_of_words():
    assert len(WORDLIST) >= 6500
    assert len(WORDLIST) <= 7800


def test_wordlist_excludes_negative_words():
    for bad in ("abrasive", "abrupt", "doom", "zombie"):
        assert bad not in WORDLIST


def test_generate_user_code_matches_pattern():
    for _ in range(50):
        code = generate_user_code()
        assert re.match(r"^[a-z]+-[a-z0-9]{5}$", code), f"bad code: {code}"


def test_generate_user_code_has_varied_words():
    codes = {generate_user_code().split("-")[0] for _ in range(50)}
    assert len(codes) >= 20
