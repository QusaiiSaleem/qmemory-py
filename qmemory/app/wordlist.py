"""Word list loader for user code generation."""

from __future__ import annotations

from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_EFF_PATH = _DATA_DIR / "eff_large_wordlist.txt"
_EXCLUDED_PATH = _DATA_DIR / "excluded_words.txt"


def _load_wordlist() -> list[str]:
    excluded = {
        line.strip()
        for line in _EXCLUDED_PATH.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    words: list[str] = []
    for line in _EFF_PATH.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            word = parts[1].strip()
            if word and word not in excluded:
                words.append(word)
    return words


WORDLIST: list[str] = _load_wordlist()
