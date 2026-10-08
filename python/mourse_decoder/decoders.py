"""Turn a finished Morse sequence into the text that gets typed.

The Rust core only reports sequences like ".-". A decoder turns that into an
Emission: the text to type and how many characters to delete first. Two cases
need state across sequences (Wabun dakuten, Chinese telecodes), and the
``erase`` field keeps the controller language-agnostic.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .dictionary import Alphabet, AlphabetError, DictionaryManager
from .telecode import TelecodeStore


@dataclass(frozen=True)
class Emission:
    """What the injector should do: press backspace ``erase`` times, then type ``text``."""

    text: str
    erase: int = 0


class Decoder(Protocol):
    def decode(self, sequence: str) -> Emission | None:
        """None means unknown sequence, type nothing."""

    def word_gap(self) -> Emission | None: ...

    def reset(self) -> None: ...


class AlphabetDecoder:
    """Decoder for JSON alphabets, including combining characters."""

    def __init__(self, alphabet: Alphabet) -> None:
        self.alphabet = alphabet
        self._last_char: str | None = None  # base for dakuten and friends

    def decode(self, sequence: str) -> Emission | None:
        combining = self.alphabet.combining.get(sequence)
        if combining is not None:
            return self._combine(combining)

        char = self.alphabet.lookup(sequence)
        if char is None:
            return None
        self._last_char = char if len(char) == 1 else None
        return Emission(char)

    def _combine(self, mark: str) -> Emission | None:
        base = self._last_char
        if base is None:
            return None
        # NFC merges base + mark into one codepoint, e.g. カ + U+3099 -> ガ
        composed = unicodedata.normalize("NFC", base + mark)
        if len(composed) != 1:
            # invalid pair (ア + dakuten): don't type the loose mark, many apps show a box
            return None
        self._last_char = composed
        return Emission(composed, erase=1)

    def word_gap(self) -> Emission | None:
        self._last_char = None
        separator = self.alphabet.word_separator
        return Emission(separator) if separator else None

    def reset(self) -> None:
        self._last_char = None


class TelecodeDecoder:
    """Decoder for four-digit Chinese telegraph codes."""

    CODE_LENGTH = 4

    def __init__(self, store: TelecodeStore, digits: Mapping[str, str]) -> None:
        self._store = store
        # digits only, punctuation would mess up the 4-digit buffer
        self._digits = {code: char for code, char in digits.items() if char.isdigit()}
        self._buffer = ""

    @property
    def pending_digits(self) -> str:
        return self._buffer

    def decode(self, sequence: str) -> Emission | None:
        digit = self._digits.get(sequence)
        if digit is None:
            return None
        self._buffer += digit
        if len(self._buffer) < self.CODE_LENGTH:
            return Emission(digit)  # show digits right away
        code, self._buffer = self._buffer, ""
        char = self._store.lookup(code)
        if char is None:
            return Emission(digit)  # unknown code: leave the digits so the user sees them
        # the fourth digit was never typed, so only three need erasing
        return Emission(char, erase=self.CODE_LENGTH - 1)

    def word_gap(self) -> Emission | None:
        self._buffer = ""  # drop a half-entered code, Chinese needs no spaces
        return None

    def reset(self) -> None:
        self._buffer = ""


def create_decoder(manager: DictionaryManager, alphabet_id: str) -> Decoder:
    if alphabet_id == DictionaryManager.TELECODE_ID:
        if manager.telecode_db is None:
            raise AlphabetError("Keine Telecode-Datenbank konfiguriert")
        store = TelecodeStore(manager.telecode_db)
        return TelecodeDecoder(store, manager.load("common").codes)
    return AlphabetDecoder(manager.select(alphabet_id))
