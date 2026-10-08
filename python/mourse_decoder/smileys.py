"""Smiley sets: JSON files that map Morse codes to smileys.

    {"id": "standard", "name": "Standard", "description": "...",
     "codes": {"--..--..": "🙂"}}

The selected set sits under the active alphabet (see DictionaryManager), so on
the same code the alphabet wins and a smiley can never hide a letter. Built-in
sets ship in the package, user sets live in the user folder.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILTIN_SMILEY_DIR = Path(__file__).with_name("smiley_sets")

_CODE_PATTERN = re.compile(r"^[.-]{1,32}$")
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class SmileyError(ValueError):
    """Invalid or unloadable smiley set."""


@dataclass(frozen=True)
class SmileySetInfo:
    id: str
    name: str
    description: str
    path: Path
    user: bool = False


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_") or "smileys"


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SmileyError(f"doppelter Schlüssel {key!r}")
        result[key] = value
    return result


def parse_smiley_text(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise SmileyError(f"Kein gültiges JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SmileyError("Das Wurzelelement muss ein JSON-Objekt { … } sein.")
    return data


def validate_codes(data: dict[str, Any]) -> dict[str, str]:
    codes = data.get("codes")
    if not isinstance(codes, dict) or not codes:
        raise SmileyError("'codes' muss ein nicht-leeres Objekt sein.")
    for code, value in codes.items():
        if not _CODE_PATTERN.match(code):
            raise SmileyError(f"ungültiger Code {code!r} (nur '.' und '-', 1–32 Zeichen)")
        if not isinstance(value, str) or not value:
            raise SmileyError(f"Code {code!r} braucht ein nicht-leeres Zeichen")
    return dict(codes)


def _read(path: Path) -> tuple[dict[str, Any], SmileySetInfo]:
    try:
        data = parse_smiley_text(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SmileyError(f"{path.name}: nicht lesbar ({exc})") from exc
    except SmileyError as exc:
        raise SmileyError(f"{path.name}: {exc}") from None
    set_id = data.get("id")
    if not isinstance(set_id, str) or not _ID_PATTERN.match(set_id):
        raise SmileyError(f"{path.name}: ungültige oder fehlende 'id'")
    if set_id != path.stem:
        raise SmileyError(f"{path.name}: 'id' ({set_id}) muss dem Dateinamen entsprechen")
    info = SmileySetInfo(set_id, str(data.get("name") or set_id), str(data.get("description") or ""), path)
    return data, info


def _directories(builtin_dir: Path, user_dir: Path | None) -> list[tuple[Path, bool]]:
    return [(builtin_dir, False), *([(user_dir, True)] if user_dir else [])]


def list_smiley_sets(builtin_dir: Path = BUILTIN_SMILEY_DIR, user_dir: Path | None = None) -> list[SmileySetInfo]:
    """All valid sets sorted by name. Broken files are skipped."""
    found: dict[str, SmileySetInfo] = {}
    for directory, is_user in _directories(builtin_dir, user_dir):
        for path in sorted(directory.glob("*.json")):
            try:
                _, info = _read(path)
            except SmileyError:
                continue
            found.setdefault(info.id, SmileySetInfo(info.id, info.name, info.description, info.path, is_user))
    return sorted(found.values(), key=lambda i: i.name.casefold())


def load_smiley_codes(
    set_id: str, builtin_dir: Path = BUILTIN_SMILEY_DIR, user_dir: Path | None = None
) -> dict[str, str]:
    """Code table of a set; an empty id means no set and gives an empty table."""
    if not set_id:
        return {}
    for directory, _ in _directories(builtin_dir, user_dir):
        path = directory / f"{set_id}.json"
        if path.is_file():
            data, _ = _read(path)
            try:
                return validate_codes(data)
            except SmileyError as exc:
                raise SmileyError(f"{path.name}: {exc}") from None
    raise SmileyError(f"Smiley-Set '{set_id}' nicht gefunden")


def import_smiley_file(source: Path, builtin_dir: Path, user_dir: Path) -> SmileySetInfo:
    """Validate any JSON file and copy it into the user folder.

    A missing id is taken from the file name. Importing the same id again
    updates the user's set.
    """
    try:
        data = parse_smiley_text(Path(source).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise SmileyError(f"{Path(source).name}: nicht lesbar ({exc})") from exc
    data.setdefault("id", slugify(Path(source).stem))
    set_id = data["id"]
    if not isinstance(set_id, str) or not _ID_PATTERN.match(set_id):
        raise SmileyError("'id' ist ungültig (nur a-z, 0-9, '-' und '_').")
    if (builtin_dir / f"{set_id}.json").exists():
        raise SmileyError(f"Die ID '{set_id}' gehört zu einem mitgelieferten Set – bitte andere ID wählen.")
    validate_codes(data)
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / f"{set_id}.json"
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return SmileySetInfo(set_id, str(data.get("name") or set_id), str(data.get("description") or ""), target, True)


def delete_smiley_set(set_id: str, user_dir: Path) -> None:
    path = user_dir / f"{set_id}.json"
    if not path.is_file():
        raise SmileyError(f"'{set_id}' ist kein eigenes Set und kann nicht gelöscht werden.")
    path.unlink()
