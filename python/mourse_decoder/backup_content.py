"""What a backup brings, looked at with the loaders and the macro parser of the program itself.

Used by ``backup.py`` while it validates a bundle: commands inside alphabet and smiley codes (they are keyed like
function key macros), alphabet, smiley and language files read by the real loaders, and the references of
``settings.json`` to alphabets, smiley sets and languages after the import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import i18n, jsontext, macros
from .dictionary import BUILTIN_ALPHABET_DIR, AlphabetError, DictionaryManager, load_alphabet
from .settings import Settings
from .smileys import BUILTIN_SMILEY_DIR, SmileyError, load_smiley_codes

TELECODE_ALPHABET = DictionaryManager.TELECODE_ID
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
# a value that has no brace, line break or tab cannot hold a command: skip the parser for it
_MAY_HOLD_COMMANDS = re.compile(r"[{\n\r\t]")
MAX_CODES_LOOKED_AT = 20_000


@dataclass(frozen=True)
class Note:
    """Something the user should know before confirming: ``backup.warn.<code>`` in the language files."""

    code: str
    params: dict[str, str]


def _codes(data: bytes) -> dict[str, Any]:
    try:
        parsed = jsontext.loads(data.decode("utf-8-sig"), dict)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    codes = parsed.get("codes") if isinstance(parsed, dict) else None
    return codes if isinstance(codes, dict) else {}


def commands_per_code(data: bytes) -> list[tuple[str, str, tuple[str, ...]]]:
    """(code, text, commands) of every code of an alphabet or smiley file whose text does something when keyed.

    Same judgement as the single-file import (``macros.commands_in_file``), one source for both.
    """
    found: list[tuple[str, str, tuple[str, ...]]] = []
    for index, (code, text) in enumerate(_codes(data).items()):
        if index >= MAX_CODES_LOOKED_AT:
            break
        if isinstance(text, str) and _MAY_HOLD_COMMANDS.search(text):
            if commands := macros.commands_in_texts([text], windows=True):
                found.append((str(code), text, tuple(commands)))
    return found


def slot_commands(macro: str) -> tuple[str, ...]:
    return tuple(macros.commands_in_texts([macro], windows=True))


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
