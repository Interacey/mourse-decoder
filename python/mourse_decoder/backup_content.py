"""What a backup brings, looked at with the loaders and the macro parser of the program itself.

Used by ``backup.py`` while it validates a bundle: commands inside alphabet and smiley codes (they are keyed like
function key macros), alphabet, smiley and language files read by the real loaders, and the references of
``settings.json`` to alphabets, smiley sets and languages after the import.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import i18n, jsontext, macros
from .dictionary import BUILTIN_ALPHABET_DIR, AlphabetError, DictionaryManager, load_alphabet
from .settings import Settings
from .smileys import BUILTIN_SMILEY_DIR, SmileyError, load_smiley_codes

TELECODE_ALPHABET = DictionaryManager.TELECODE_ID
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
# a value that has no brace and no control character (a line break, tab, backspace, Escape, ...) cannot hold a
# command: skip the parser for it. Cf (direction marks, zero-width joiners) is not listed: typing one changes how
# text looks, it does not press a key, and emoji and several scripts need the joiners.
_MAY_HOLD_COMMANDS = re.compile(r"[{\x00-\x1f\x7f-\x9f]")
# the list that is kept for one file; the file is risky from the first entry on, and what is looked at is not cut off
MAX_COMMANDS_LISTED = 5_000


@dataclass(frozen=True)
class Note:
    """Something the user should know before confirming: ``backup.warn.<code>`` in the language files."""

    code: str
    params: dict[str, str]

    def __post_init__(self) -> None:
        # the values may come from a file (a language id, an alphabet name): written out and cut, once, here.
        # Lists of field names are short and complete; a reason from a loader may be longer than a name
        limits = {"fields": None, "names": 300, "reason": 300}
        object.__setattr__(
            self, "params", {k: jsontext.display_text(str(v), limits.get(k, 80)) for k, v in self.params.items()}
        )


def _typed_texts(data: bytes) -> list[tuple[str, Any]]:
    """``macros.typed_texts`` of the bytes of a file; an unreadable file has none (importing it says so)."""
    try:
        return macros.typed_texts(jsontext.loads(data.decode("utf-8-sig"), dict))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return []


def commands_per_code(data: bytes) -> list[tuple[str, str, tuple[str, ...]]]:
    """(code, text, commands) of every typed text of an alphabet or smiley file that does something when keyed.

    Same judgement as the single-file import (``macros.commands_in_file``), one source for both. Every code is
    looked at, however many there are (the size of the file is limited): a limit here would let a command stand
    behind enough harmless codes. At most ``MAX_COMMANDS_LISTED`` entries are returned, see ``typed_commands``.
    """
    return typed_commands(data)[0]


def typed_commands(data: bytes) -> tuple[list[tuple[str, str, tuple[str, ...]]], int]:
    """``commands_per_code`` and the number of entries with commands there really are."""
    return commands_in_rows(_typed_texts(data))


def commands_in_rows(rows: Iterable[tuple[str, Any]]) -> tuple[list[tuple[str, str, tuple[str, ...]]], int]:
    """(label, text, commands) for the rows whose text does something when keyed, and how many such rows there are.

    The list keeps the first ``MAX_COMMANDS_LISTED``; the count goes on, so the dialog can say how many are left.
    """
    found: list[tuple[str, str, tuple[str, ...]]] = []
    total = 0
    for label, text in rows:
        if isinstance(text, str) and _MAY_HOLD_COMMANDS.search(text):
            if commands := macros.commands_in_texts([text]):
                total += 1
                if len(found) < MAX_COMMANDS_LISTED:
                    found.append((str(label), text, tuple(commands)))
    return found, total


def slot_commands(macro: str) -> tuple[str, ...]:
    return tuple(macros.commands_in_texts([macro]))


def trigger_risk(trigger: str) -> str:
    """A mouse button without Ctrl, Alt or Win starts a slot on every click: say so ("" if the trigger is fine)."""
    parts = [p.strip().lower() for p in str(trigger).split("+")]
    held = {"ctrl", "alt", "win", "control", "strg", "windows"}
    if parts and parts[-1].startswith("mouse") and not held & set(parts):
        return str(trigger)
    return ""


def alphabet_problem(stem: str, user_dir: Path) -> str | None:
    """Why the program could not load ``<stem>.json`` of ``user_dir`` (with what it extends), or None."""
    try:
        load_alphabet(stem, [user_dir, BUILTIN_ALPHABET_DIR])
    except AlphabetError as exc:
        return str(exc)
    return None


def smiley_problem(stem: str, user_dir: Path) -> str | None:
    try:
        load_smiley_codes(stem, user_dir / "_none_", user_dir)
    except SmileyError as exc:
        return str(exc)
    return None


def language_problem(path: Path) -> str | None:
    if i18n._read(path) is not None:
        return None
    return "keine gültige Sprachdatei (id und strings fehlen oder sind falsch)"


def has_loadable_id(stem: str) -> bool:
    """Files whose name is no id are skipped by the program, so nothing is checked for them."""
    return bool(_ID.match(stem))


def builtin_language_ids() -> set[str]:
    """The ids of the languages that ship with the program."""
    return _language_ids(i18n.BUILTIN_LOCALE_DIR)


def _builtin_ids(folder: Path) -> set[str]:
    return {p.stem for p in folder.glob("*.json")}


def _language_ids(folder: Path) -> set[str]:
    return {entry[0] for p in folder.glob("*.json") if (entry := i18n._read(p)) is not None}


def reference_notes(
    settings: Settings, alphabets: set[str], smileys: set[str], language_folder: Path, has_telecode: bool
) -> list[Note]:
    """Alphabet, smiley set and language that ``settings`` names but that will not exist after the import.

    The program falls back (latin, no smileys, English) and says so; here it is said before the import.
    ``alphabets`` and ``smileys`` are the ids of the user files after the import.
    """
    notes: list[Note] = []
    if settings.alphabet == TELECODE_ALPHABET:
        if not has_telecode:
            notes.append(Note("telecode_missing", {}))
    elif settings.alphabet not in alphabets | _builtin_ids(BUILTIN_ALPHABET_DIR):
        notes.append(Note("alphabet_missing", {"name": settings.alphabet}))
    if settings.smileys and settings.smileys not in smileys | _builtin_ids(BUILTIN_SMILEY_DIR):
        notes.append(Note("smileys_missing", {"name": settings.smileys}))
    known = _language_ids(i18n.BUILTIN_LOCALE_DIR)
    if language_folder.is_dir():
        known |= _language_ids(language_folder)
    if settings.language not in known:
        notes.append(Note("language_missing", {"name": settings.language}))
    return notes
