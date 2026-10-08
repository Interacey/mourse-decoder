"""UI languages from JSON files, English by default.

    {"id": "de", "name": "Deutsch", "strings": {"nav.settings": "Einstellungen", ...}}

Built-in files live in locales/, user files in the user's "languages" folder.
Missing keys fall back to English, so a partial translation still works. To add
a language, drop in another JSON file.
"""

from __future__ import annotations

import json
from pathlib import Path

BUILTIN_LOCALE_DIR = Path(__file__).with_name("locales")
DEFAULT_LANGUAGE = "en"

_fallback: dict[str, str] = {}
_strings: dict[str, str] = {}
_current = DEFAULT_LANGUAGE
_user_dir: Path | None = None


def _read(path: Path) -> tuple[str, str, dict[str, str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        strings = data["strings"]
        if not isinstance(strings, dict):
            return None
        return str(data["id"]), str(data.get("name") or data["id"]), {str(k): str(v) for k, v in strings.items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _files(user_dir: Path | None) -> list[Path]:
    dirs = [BUILTIN_LOCALE_DIR, *([user_dir] if user_dir else [])]
    return [p for d in dirs if d.is_dir() for p in sorted(d.glob("*.json"))]


def available_languages(user_dir: Path | None = None) -> list[tuple[str, str]]:
    """(id, display name) pairs, English first, the rest sorted by name."""
    found: dict[str, str] = {}
    for path in _files(user_dir or _user_dir):
        if (entry := _read(path)) is not None:
            found.setdefault(entry[0], entry[1])
    rest = sorted((i for i in found.items() if i[0] != DEFAULT_LANGUAGE), key=lambda i: i[1].casefold())
    return ([(DEFAULT_LANGUAGE, found[DEFAULT_LANGUAGE])] if DEFAULT_LANGUAGE in found else []) + rest


def set_language(code: str, user_dir: Path | None = None) -> str:
    """Switch language, falling back to English. Returns the id that is now active."""
    global _fallback, _strings, _current, _user_dir
    _user_dir = user_dir or _user_dir
    user_dir = _user_dir
    loaded: dict[str, dict[str, str]] = {}
    for path in _files(user_dir):
        if (entry := _read(path)) is not None:
            loaded.setdefault(entry[0], entry[2])
    _fallback = loaded.get(DEFAULT_LANGUAGE, {})
    _current = code if code in loaded else DEFAULT_LANGUAGE
    _strings = loaded.get(_current, _fallback)
    return _current


def current_language() -> str:
    return _current


def tr(key: str, /, **values: object) -> str:
    """Translate ``key`` and fill {placeholders}; a bad placeholder returns the raw text."""
    text = _strings.get(key) or _fallback.get(key) or key
    if not values:
        return text
    try:
        return text.format(**values)
    except (KeyError, IndexError, ValueError):
        return text


set_language(DEFAULT_LANGUAGE)
