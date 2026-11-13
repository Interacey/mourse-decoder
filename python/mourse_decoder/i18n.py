"""UI languages from JSON files, English by default.

    {"id": "de", "name": "Deutsch", "strings": {"nav.settings": "Einstellungen", ...}}

Built-in files live in locales/, user files in the user's "languages" folder.
Missing keys fall back to English, so a partial translation still works. To add
a language, drop in another JSON file.
"""

from __future__ import annotations

import functools
import json
import string
from pathlib import Path

from . import jsontext

BUILTIN_LOCALE_DIR = Path(__file__).with_name("locales")
DEFAULT_LANGUAGE = "en"

_fallback: dict[str, str] = {}
_strings: dict[str, str] = {}
_current = DEFAULT_LANGUAGE
_user_dir: Path | None = None


def _read(path: Path) -> tuple[str, str, dict[str, str]] | None:
    try:
        text = path.read_text(encoding="utf-8-sig")
        jsontext.check_depth(text)  # the same fixed limit everywhere: one file cannot pass here and fail elsewhere
        data = json.loads(text)
        if jsontext.has_lone_surrogate(data):
            return None
        strings = data["strings"]
        if not isinstance(strings, dict):
            return None
        return str(data["id"]), str(data.get("name") or data["id"]), {str(k): str(v) for k, v in strings.items()}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
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


@functools.lru_cache(maxsize=16)
def builtin_strings(code: str) -> dict[str, str]:
    """The strings of a language that ships with the program (never a file of the user's folder)."""
    try:
        return json.loads((BUILTIN_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def tr_trusted(key: str, **values: object) -> str:
    """Like ``tr``, but only from the built-in language files (English where the language is not built in).

    For texts that a decision rests on (the labels of buttons, the warnings of an import): a language file that
    came with a backup could swap the meaning of "Yes" and "No" or drop a warning.
    """
    text = builtin_strings(current_language()).get(key) or builtin_strings(DEFAULT_LANGUAGE).get(key) or key
    if not values:
        return text
    try:
        return _formatter.vformat(text, (), values)
    except (KeyError, IndexError, ValueError):
        return text


def current_language() -> str:
    return _current


class _PlainFormatter(string.Formatter):
    """Only plain names as placeholders.

    ``str.format`` also follows ``{msg.__class__.__init__.__globals__}`` into any object. A language
    file from the user's folder is not trusted, so such a placeholder must not read anything.
    """

    def get_field(self, field_name: str, args: tuple, kwargs: dict) -> tuple[object, str]:
        if not field_name.isidentifier():
            raise ValueError(f"Platzhalter {field_name!r} nicht erlaubt")
        return kwargs[field_name], field_name

    def convert_field(self, value: object, conversion: str | None) -> object:
        if conversion:
            raise ValueError("Umwandlung im Platzhalter nicht erlaubt")
        return value

    def format_field(self, value: object, format_spec: str) -> str:
        # a spec such as {n:>999999999} would allocate gigabytes
        if format_spec:
            raise ValueError("Formatangabe im Platzhalter nicht erlaubt")
        return str(value)


_formatter = _PlainFormatter()


def tr(key: str, /, **values: object) -> str:
    """Translate ``key`` and fill {placeholders}; a bad placeholder returns the raw text."""
    text = _strings.get(key) or _fallback.get(key) or key
    if not values:
        return text
    try:
        return _formatter.vformat(text, (), values)
    except (KeyError, IndexError, ValueError):
        return text


set_language(DEFAULT_LANGUAGE)
