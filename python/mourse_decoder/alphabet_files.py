"""Create, import, edit and delete user alphabet files (no tkinter).

The UI only hands in text and shows errors. Everything error-prone (checking the
JSON, writing atomically, rolling back) lives here so tests cover it. User
alphabets go in ``DictionaryManager.user_directory``, built-in ones stay read-only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .dictionary import (
    _ID_PATTERN,
    AlphabetError,
    AlphabetInfo,
    DictionaryManager,
    _reject_duplicate_keys,
    load_alphabet,
)

NEW_ALPHABET_TEMPLATE: dict[str, object] = {
    "id": "mein-alphabet",
    "name": "Mein Alphabet",
    "description": "",
    "extends": "common",
    "codes": {".-": "A", "-...": "B"},
}


def new_alphabet_text(alphabet_id: str = "mein-alphabet") -> str:
    """Starting point for a new alphabet; it inherits digits and punctuation from common."""
    data = {**NEW_ALPHABET_TEMPLATE, "id": alphabet_id}
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def slugify(name: str) -> str:
    """File-name-safe id from any text."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return slug or "alphabet"


def parse_alphabet_text(text: str) -> dict:
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AlphabetError(f"Kein gültiges JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AlphabetError("Das Wurzelelement muss ein JSON-Objekt { … } sein.")
    return data


def read_alphabet_text(info: AlphabetInfo) -> str:
    return info.path.read_text(encoding="utf-8")


def _require_user_directory(manager: DictionaryManager) -> Path:
    if manager.user_directory is None:
        raise AlphabetError("Es ist kein Ordner für eigene Alphabete konfiguriert.")
    return manager.user_directory


def save_alphabet_text(manager: DictionaryManager, text: str, replacing: str | None = None) -> AlphabetInfo:
    """Save ``text`` as a user alphabet and validate it completely.

    ``replacing`` is the old id when editing; if the id changed, the old file is
    removed (a rename). The file is written first, then fully validated with
    load_alphabet (extends included), and rolled back on failure so a broken
    file never stays behind.
    """
    directory = _require_user_directory(manager)
    data = parse_alphabet_text(text)
    alphabet_id = data.get("id")
    if not isinstance(alphabet_id, str) or not _ID_PATTERN.match(alphabet_id):
        raise AlphabetError("'id' fehlt oder ist ungültig (nur a-z, 0-9, '-' und '_', Anfang Buchstabe/Ziffer).")
    if (manager.directory / f"{alphabet_id}.json").exists():
        raise AlphabetError(f"Die ID '{alphabet_id}' gehört zu einem mitgelieferten Alphabet – bitte andere ID wählen.")
    if alphabet_id != replacing and (directory / f"{alphabet_id}.json").exists():
        raise AlphabetError(f"Es gibt bereits ein eigenes Alphabet mit der ID '{alphabet_id}'.")

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{alphabet_id}.json"
    previous = target.read_bytes() if target.exists() else None
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        load_alphabet(alphabet_id, [manager.directory, directory])
    except AlphabetError:
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
        raise

    if replacing and replacing != alphabet_id:
        _remove_user_file(manager, replacing)
    manager.invalidate()
    return AlphabetInfo(
        id=alphabet_id,
        name=str(data.get("name") or alphabet_id),
        description=str(data.get("description") or ""),
        path=target,
        user=True,
    )


def import_alphabet_file(manager: DictionaryManager, source: Path) -> AlphabetInfo:
    """Copy any JSON file into the user folder, taking a missing id from the file name."""
    try:
        text = Path(source).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise AlphabetError(f"{Path(source).name}: nicht lesbar ({exc})") from exc
    data = parse_alphabet_text(text)
    if "id" not in data:
        data = {"id": slugify(Path(source).stem), **data}
        text = json.dumps(data, ensure_ascii=False)
    return save_alphabet_text(manager, text)


def export_alphabet_file(info: AlphabetInfo, destination: Path) -> None:
    Path(destination).write_text(read_alphabet_text(info), encoding="utf-8")


def _remove_user_file(manager: DictionaryManager, alphabet_id: str) -> None:
    directory = _require_user_directory(manager)
    (directory / f"{alphabet_id}.json").unlink(missing_ok=True)


def delete_alphabet(manager: DictionaryManager, alphabet_id: str) -> None:
    """Delete a user alphabet, unless another one still extends it."""
    directory = _require_user_directory(manager)
    path = directory / f"{alphabet_id}.json"
    if not path.is_file():
        raise AlphabetError(f"'{alphabet_id}' ist kein eigenes Alphabet und kann nicht gelöscht werden.")
    for other in sorted(directory.glob("*.json")):
        if other == path:
            continue
        try:
            extends = parse_alphabet_text(other.read_text(encoding="utf-8")).get("extends")
        except (OSError, AlphabetError):
            continue  # a broken neighbour doesn't block deleting
        if extends == alphabet_id:
            raise AlphabetError(f"Kann nicht gelöscht werden: '{other.stem}' erweitert dieses Alphabet.")
    os.remove(path)
    manager.invalidate()
