"""Text to Morse code with an alphabet (no tkinter).

It inverts the decoder table. Words are joined with " / ", line breaks stay, and
characters the alphabet doesn't know become "?" and are reported. Composed
characters (ガ in Wabun) are split into base + combining mark.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from .dictionary import Alphabet

WORD_SEPARATOR = " / "
UNKNOWN = "?"


@dataclass(frozen=True)
class Encoded:
    morse: str
    unknown: list[str] = field(default_factory=list)  # characters that couldn't be translated, deduplicated


def reverse_table(alphabet: Alphabet) -> dict[str, str]:
    """Character to code; if there are several codes, the shortest wins."""
    table: dict[str, str] = {}
    for source in (alphabet.codes, alphabet.combining):
        for code, char in source.items():
            if char not in table or len(code) < len(table[char]):
                table[char] = code
    return table


def _encode_char(char: str, table: dict[str, str]) -> list[str] | None:
    for candidate in (char, char.upper(), char.lower()):
        if candidate in table:
            return [table[candidate]]
    parts = unicodedata.normalize("NFD", char)
    if len(parts) > 1 and all(p in table for p in parts):
        return [table[p] for p in parts]
    return None


def encode_text(alphabet: Alphabet, text: str) -> Encoded:
    table = reverse_table(alphabet)
    unknown: list[str] = []
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        words: list[str] = []
        for word in line.split():
            codes: list[str] = []
            for char in word:
                found = _encode_char(char, table)
                if found is None:
                    codes.append(UNKNOWN)
                    if char not in unknown:
                        unknown.append(char)
                else:
                    codes.extend(found)
            words.append(" ".join(codes))
        lines.append(WORD_SEPARATOR.join(words))
    return Encoded("\n".join(lines), unknown)
