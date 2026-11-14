"""User settings: data model, validation and persistence.

Settings are a frozen dataclass because the UI thread and the Rust worker share
them. A change always creates a new object that is swapped in atomically.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import logging
import os
import re
import shutil
import sys
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import _native, jsontext

log = logging.getLogger(__name__)

# Must match TriggerKey::ALL in src/trigger.rs (test_native checks it). It's duplicated
# so validation works without the compiled module.
TRIGGER_NAMES: tuple[str, ...] = (
    "ScrollLock", "Pause", "Insert", "ControlRight", "AltGr",
    "F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12",
    "Space", "MouseLeft", "MouseRight", "MouseMiddle",
)

THEME_MODES: tuple[str, ...] = ("system", "light", "dark")

# Any key (Windows virtual-key code) and extra mouse buttons from Mouse4 on.
# Keep in sync with TriggerKey::from_str in src/trigger.rs.
# [0-9] and \Z instead of \d and $: Unicode digits and a trailing newline must not pass (Rust rejects them).
# re.ASCII: without it IGNORECASE also matches the Kelvin sign (U+212A) for "k" and the long s (U+017F) for "s",
# which Rust (eq_ignore_ascii_case) does not.
_FLAGS = re.IGNORECASE | re.ASCII
_VK_PATTERN = re.compile(r"^VK([0-9]{1,3})\Z", _FLAGS)
_MOUSE_PATTERN = re.compile(r"^Mouse([0-9]{1,2})\Z", _FLAGS)
# F13..F24 are keys of their own (virtual-key codes 124..135); VK124 is the same key as F13
_EXTRA_F_PATTERN = re.compile(r"^F(1[3-9]|2[0-4])\Z", _FLAGS)
_F13_VK = 124
# What Python strips and lowercases in a binding is ASCII only. str.strip() also removes U+001C..U+001F and
# str.lower() maps U+212A to "k", Rust's trim() and eq_ignore_ascii_case do neither: a spec that only Python
# accepts would pass the settings check and then stop the program at every start (ValueError of the engine).
_ASCII_BLANKS = " \t\n\r\x0b\x0c"
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _lower(text: str) -> str:
    return text.translate(_ASCII_LOWER)


def _strip(text: str) -> str:
    return text.strip(_ASCII_BLANKS)


def is_valid_trigger(name: object) -> bool:
    """True for the standard triggers, F13..F24, VK1..VK255 and Mouse4..Mouse32."""
    if not isinstance(name, str):
        return False
    if name in TRIGGER_NAMES:
        return True
    if _EXTRA_F_PATTERN.match(name):
        return True
    if match := _VK_PATTERN.match(name):
        return 1 <= int(match.group(1)) <= 255
    if match := _MOUSE_PATTERN.match(name):
        return 4 <= int(match.group(1)) <= 32
    return False


# Keep in sync with modifier_group in src/trigger.rs.
_MODIFIERS = {
    "ctrl", "control", "strg", "alt", "shift", "umschalt", "win", "windows", "meta", "super",
    "capslock", "caps", "feststell",
}


def is_valid_binding(spec: object) -> bool:
    """A trigger, optionally after modifiers joined with "+", e.g. "F8" or "CapsLock+VK81"."""
    if not isinstance(spec, str) or not _strip(spec):
        return False
    *modifiers, key = (_strip(part) for part in spec.split("+"))
    return is_valid_trigger(key) and all(_lower(m) in _MODIFIERS for m in modifiers)


@functools.lru_cache(maxsize=512)
def native_binding_error(spec: str) -> str | None:
    """Why the Rust engine refuses ``spec`` as a key or shortcut, None if it takes it (or there is no Rust module).

    The engine's constructor only parses (no hook, no thread, nothing global), and it parses with the same
    ``KeyBinding`` the shortcut setters use. Asking it makes the settings check as strict as the code that has
    to live with the value: a difference between the two parsers cannot become a crash at the next start.
    """
    if not _native.NATIVE_AVAILABLE:
        return None
    try:
        _native.load_native().MorseEngine(trigger=spec)
    except ValueError as exc:
        return str(exc)
    return None


def is_valid_shortcut(spec: object) -> bool:
    """"" (no shortcut) or a key binding such as "Ctrl+Shift+Pause"."""
    return spec == "" or is_valid_binding(spec)


# Spellings the Rust side accepts for the same modifier, see modifier_group in src/trigger.rs.
_MODIFIER_ALIASES = {
    "control": "ctrl", "strg": "ctrl", "umschalt": "shift", "windows": "win", "meta": "win",
    "super": "win", "caps": "capslock", "feststell": "capslock",
}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "win", "capslock")
_MODIFIER_NAMES = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "capslock": "CapsLock"}
_TRIGGER_BY_LOWER = {name.lower(): name for name in TRIGGER_NAMES}


def _canonical_key(key: str) -> str:
    """Canonical spelling of a trigger name: ``f8`` -> ``F8``, ``vk124`` -> ``F13``, ``mouse5`` -> ``Mouse5``."""
    lower = _lower(key)
    if lower in _TRIGGER_BY_LOWER:
        return _TRIGGER_BY_LOWER[lower]
    if match := _VK_PATTERN.match(key):
        code = int(match.group(1))
        return f"F{code - _F13_VK + 13}" if _F13_VK <= code <= _F13_VK + 11 else f"VK{code}"
    if match := _MOUSE_PATTERN.match(key):
        return f"Mouse{int(match.group(1))}"
    if _EXTRA_F_PATTERN.match(key):
        return f"F{int(key[1:])}"
    return key


def normalize_binding(spec: str) -> str:
    """The form the Rust hook prints, e.g. ``strg+shift+vk124`` -> ``Ctrl+Shift+F13``.

    Modifiers in the order Ctrl, Alt, Shift, Win, CapsLock; an invalid spec comes back stripped
    but otherwise unchanged.
    """
    spec = _strip(spec)
    if not is_valid_binding(spec):
        return spec
    *modifiers, key = (_lower(_strip(part)) for part in spec.split("+"))
    wanted = {_MODIFIER_ALIASES.get(m, m) for m in modifiers}
    names = [_MODIFIER_NAMES[m] for m in _MODIFIER_ORDER if m in wanted]
    return "+".join([*names, _canonical_key(key)])


def same_binding(first: str, second: str) -> bool:
    """True if both specs name the same keys; case, blanks, modifier order and spellings don't matter."""
    return normalize_binding(first).lower() == normalize_binding(second).lower() or _loose(first) == _loose(second)


def fires_with(reserved: str, other: str) -> bool:
    """True if ``reserved`` (extra modifiers allowed) also fires when ``other`` is pressed.

    Same key, and ``reserved`` needs no modifier that ``other`` lacks; see ``KeyBinding::fires_with``
    in src/trigger.rs. Both specs must be valid.
    """
    *r_mods, r_key = normalize_binding(reserved).split("+")
    *o_mods, o_key = normalize_binding(other).split("+")
    return r_key == o_key and set(r_mods) <= set(o_mods)


def keys_clash(first: str, second: str) -> bool:
    """True if one of two valid bindings also fires when the other is pressed."""
    return fires_with(first, second) or fires_with(second, first)


# Esc aborts typing and a running macro, so it can't be a Morse key (with any modifiers)
_ESC_KEYS = ("VK27",)


def _is_esc(spec: str) -> bool:
    return normalize_binding(spec).split("+")[-1] in _ESC_KEYS


def _loose(spec: str) -> tuple[frozenset[str], str]:
    *modifiers, key = (_lower(_strip(part)) for part in spec.split("+"))
    return frozenset(modifiers), key


# same shape as an alphabet id (which is also its file name), see dictionary.py
_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*\Z")
MAX_CYCLE_ALPHABETS = 64

# UI limits in ms: below 50 nobody can tell dot from dash on purpose, above 5 s it isn't morsing
DOT_THRESHOLD_RANGE = (50, 2000)
LETTER_GAP_RANGE = (100, 5000)
WORD_GAP_RANGE = (0, 15000)
# pause after each typed character, in ms. Keep in sync with CHAR_DELAY_RANGE_MS and DEFAULT_CHAR_DELAY_MS in
# src/injector.rs: below 15 ms Windows Notepad scrambled the text, so 20 is the default
DEFAULT_TYPING_DELAY_MS = 20
TYPING_DELAY_RANGE = (0, 10000)


def effective_typing_delay(global_ms: int, macro_ms: int | None) -> int:
    """The pause a macro starts with: the slot's own value if it has one, else the global setting.

    A {Delay N} inside the macro text still wins over both from that point on.
    """
    return global_ms if macro_ms is None else macro_ms


def typing_delay_error(value: object) -> str | None:
    """German error text if ``value`` is no whole number of ms in ``TYPING_DELAY_RANGE``, else None."""
    low, high = TYPING_DELAY_RANGE
    if not isinstance(value, int) or isinstance(value, bool):
        return "Die Tastenanschlag-Verzögerung muss eine ganze Zahl sein."
    if not low <= value <= high:
        return f"Die Tastenanschlag-Verzögerung muss zwischen {low} und {high} ms liegen."
    return None


def is_valid_id(value: object) -> bool:
    """True for an alphabet or smiley set id: the file name pattern, so an id can never be a path (``//host/x``)."""
    return isinstance(value, str) and bool(_ID_PATTERN.match(value))


@dataclass(frozen=True)
class Settings:
    dot_threshold_ms: int = 200  # shorter press is a dot
    letter_gap_ms: int = 600     # silence that ends a letter
    word_gap_ms: int = 1400      # ITU ratio 7:3 -> 600 * 7/3, 0 = no spaces
    typing_delay_ms: int = DEFAULT_TYPING_DELAY_MS  # pause after each typed character; a macro may set its own
    trigger: str = "ScrollLock"  # key or combination; with a separate dash key it gives the dots
    separate_dash_key: bool = False  # off: press duration decides, on: dash_trigger gives dashes
    dash_trigger: str = "Pause"
    alphabet: str = "latin"
    smileys: str = ""            # smiley set id, "" = none
    enabled: bool = True
    language: str = "en"         # id of a file in locales/
    theme: str = "system"        # "system" follows Windows, else "light" or "dark"
    sidebar_collapsed: bool = False
    notifications: bool = True   # small message when the shortcut turns Morse input on or off
    block_input: bool = False    # hide the trigger's normal function so only decoded text arrives
    block_shortcut: str = "Ctrl+Shift+Pause"  # toggles block_input from anywhere, "" = none
    cycle_shortcut: str = ""     # switches to the next alphabet of cycle_alphabets, "" = none
    cycle_alphabets: tuple[str, ...] = ()  # the user's own pick of alphabet ids to switch through
    undo_shortcut: str = ""      # deletes the last typed output, "" = none
    trigger_enabled: bool = False  # other programs may start macros of slots marked "triggerable"

    def validate(self) -> list[str]:
        """List of error messages, empty if valid."""
        errors: list[str] = []

        def check_range(name: str, label: str, value: Any, bounds: tuple[int, int]) -> None:
            # bool is an int subtype, exclude it explicitly
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{label} muss eine ganze Zahl sein.")
            elif not bounds[0] <= value <= bounds[1]:
                errors.append(f"{label} muss zwischen {bounds[0]} und {bounds[1]} ms liegen.")

        check_range("dot_threshold_ms", "Punkt/Strich-Schwelle", self.dot_threshold_ms, DOT_THRESHOLD_RANGE)
        check_range("letter_gap_ms", "Buchstabenpause", self.letter_gap_ms, LETTER_GAP_RANGE)
        check_range("word_gap_ms", "Wortpause", self.word_gap_ms, WORD_GAP_RANGE)

        if (error := typing_delay_error(self.typing_delay_ms)) is not None:
            errors.append(error)

        if not errors:
            if self.word_gap_ms and self.word_gap_ms <= self.letter_gap_ms:
                errors.append("Die Wortpause muss länger als die Buchstabenpause sein (oder 0 = aus).")
        errors.extend(self._key_errors())
        if not isinstance(self.alphabet, str) or not self.alphabet:
            errors.append("Es muss ein Alphabet gewählt sein.")
        elif not is_valid_id(self.alphabet):
            errors.append(f"Ungültige Alphabet-ID '{self.alphabet}' (nur a-z, 0-9, '-' und '_').")
        if not isinstance(self.smileys, str):
            errors.append("'smileys' muss ein Text sein (ID des Smiley-Sets oder leer).")
        elif self.smileys and not is_valid_id(self.smileys):
            errors.append(f"Ungültige Smiley-Set-ID '{self.smileys}' (nur a-z, 0-9, '-' und '_').")
        if not isinstance(self.enabled, bool):
            errors.append("'enabled' muss true oder false sein.")
        if not isinstance(self.language, str) or not self.language:
            errors.append("Es muss eine Sprache gewählt sein.")
        if self.theme not in THEME_MODES:
            errors.append(f"Unbekanntes Erscheinungsbild '{self.theme}'.")
        if not isinstance(self.sidebar_collapsed, bool):
            errors.append("'sidebar_collapsed' muss true oder false sein.")
        if not isinstance(self.notifications, bool):
            errors.append("'notifications' muss true oder false sein.")
        if not isinstance(self.block_input, bool):
            errors.append("'block_input' muss true oder false sein.")
        if not isinstance(self.trigger_enabled, bool):
            errors.append("'trigger_enabled' muss true oder false sein.")
        for spec in (self.block_shortcut, self.cycle_shortcut, self.undo_shortcut):
            if not is_valid_shortcut(spec):
                errors.append(f"Ungültiges Tastenkürzel '{spec}'.")
            elif spec and native_binding_error(spec):  # type: ignore[arg-type]
                errors.append(f"Tastenkürzel '{spec}' wird von der Tastatur-Bibliothek abgelehnt.")
        if self.block_shortcut and self.cycle_shortcut and same_binding(self.block_shortcut, self.cycle_shortcut):
            errors.append("Die beiden Kürzel (Ein/Aus und Umschalten) müssen verschieden sein.")
        if self.undo_shortcut and any(
            same_binding(self.undo_shortcut, other) for other in (self.block_shortcut, self.cycle_shortcut) if other
        ):
            errors.append("Das Rückgängig-Kürzel muss sich von den anderen Kürzeln unterscheiden.")
        errors.extend(self._shortcut_clash_errors())
        errors.extend(self._cycle_errors())
        return errors

    def _cycle_errors(self) -> list[str]:
        items = self.cycle_alphabets
        if not isinstance(items, tuple) or not all(isinstance(i, str) and _ID_PATTERN.match(i) for i in items):
            return ["'cycle_alphabets' muss eine Liste von Alphabet-IDs sein."]
        if len(items) > MAX_CYCLE_ALPHABETS or len(set(items)) != len(items):
            return [f"Die Umschalt-Liste darf höchstens {MAX_CYCLE_ALPHABETS} verschiedene Alphabete enthalten."]
        return []

    def _key_errors(self) -> list[str]:
        if not is_valid_binding(self.trigger):
            return [f"Unbekannter Taster '{self.trigger}'."]
        if native_binding_error(self.trigger):
            return [f"Taster '{self.trigger}' wird von der Tastatur-Bibliothek abgelehnt."]
        if not isinstance(self.separate_dash_key, bool):
            return ["'separate_dash_key' muss true oder false sein."]
        if not is_valid_binding(self.dash_trigger):
            return [f"Unbekannte Strich-Taste '{self.dash_trigger}'."]
        if self.separate_dash_key and native_binding_error(self.dash_trigger):  # only this one goes to the engine
            return [f"Strich-Taste '{self.dash_trigger}' wird von der Tastatur-Bibliothek abgelehnt."]
        if _is_esc(self.trigger) or (self.separate_dash_key and _is_esc(self.dash_trigger)):
            return ["Esc bricht die Eingabe ab und kann keine Morse-Taste sein."]
        if self.separate_dash_key and same_binding(self.trigger, self.dash_trigger):
            return ["Punkt- und Strich-Taste müssen verschieden sein."]
        if self.separate_dash_key and keys_clash(self.trigger, self.dash_trigger):
            return [
                f"Punkt-Taste ({self.trigger}) und Strich-Taste ({self.dash_trigger}) stören sich: "
                "eine löst auch die andere aus."
            ]
        return []

    def _shortcut_clash_errors(self) -> list[str]:
        """Shortcuts that a Morse key also fires on, or that fire on a Morse key press."""
        morse = [("Punkt-Taste", self.trigger)]
        if self.separate_dash_key:
            morse.append(("Strich-Taste", self.dash_trigger))
        errors = []
        shortcuts = (
            ("Ein/Aus-Kürzel", self.block_shortcut),
            ("Umschalt-Kürzel", self.cycle_shortcut),
            ("Rückgängig-Kürzel", self.undo_shortcut),
        )
        for shortcut_label, shortcut in shortcuts:
            if not shortcut or not is_valid_binding(shortcut):
                continue
            for key_label, key in morse:
                if is_valid_binding(key) and keys_clash(shortcut, key):
                    errors.append(
                        f"{key_label} ({key}) und {shortcut_label} ({shortcut}) stören sich: "
                        "eine Taste löst auch die andere aus."
                    )
        return errors

    def without_clashing_shortcuts(self) -> Settings:
        """Copy with every shortcut cleared that clashes with a Morse key."""
        keys = [self.trigger] + ([self.dash_trigger] if self.separate_dash_key else [])

        def keep(shortcut: str) -> str:
            clash = shortcut and is_valid_binding(shortcut) and any(
                is_valid_binding(k) and keys_clash(shortcut, k) for k in keys
            )
            return "" if clash else shortcut

        return self.replace(
            block_shortcut=keep(self.block_shortcut),
            cycle_shortcut=keep(self.cycle_shortcut),
            undo_shortcut=keep(self.undo_shortcut),
        )

    @property
    def engine_dash_trigger(self) -> str:
        """Dash key as the engine expects it: "" while the press duration decides."""
        return self.dash_trigger if self.separate_dash_key else ""

    def replace(self, **changes: Any) -> Settings:
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Ignore unknown keys, default the missing ones, so versions can read each other's files."""
        known = {f.name for f in dataclasses.fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        if isinstance(values.get("cycle_alphabets"), list):  # JSON has no tuples
            values["cycle_alphabets"] = tuple(values["cycle_alphabets"])
        return cls(**values)

    def cycle_target(self, available: Collection[str], steps: int = 1) -> tuple[str, int, int] | None:
        """(alphabet id, position, list length) after ``steps`` presses of the cycle shortcut.

        Alphabets that were deleted meanwhile are skipped. If the active alphabet is not in the
        list, the first press goes to its first entry. None if no entry is usable.
        """
        usable = [a for a in self.cycle_alphabets if a in available]
        if not usable:
            return None
        current = usable.index(self.alphabet) if self.alphabet in usable else -1
        position = (current + steps) % len(usable)
        return usable[position], position + 1, len(usable)


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "MourseDecoder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MourseDecoder"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mourse-decoder"


SHOWN_ID_LENGTH = 16  # the notice must stay under the 256 UTF-16 units of a tray balloon, in every language


def _shown(value: str) -> str:
    """An id from a file for a notice: control and direction-changing characters written out, cut short."""
    return jsontext.display_text(value, SHOWN_ID_LENGTH)


def _canonical_keys(settings: Settings) -> Settings:
    """Trigger and shortcuts in the spelling the Rust hook prints (``strg+shift+vk124`` -> ``Ctrl+Shift+F13``).

    Only values that are valid are touched; an invalid one stays as it is and fails the check with its own text.
    """
    changes = {}
    for name in ("trigger", "dash_trigger", "block_shortcut", "cycle_shortcut", "undo_shortcut"):
        value = getattr(settings, name)
        if isinstance(value, str) and value and is_valid_binding(value):
            canonical = normalize_binding(value)
            if canonical != value:
                changes[name] = canonical
    return settings.replace(**changes) if changes else settings


def _without_bad_ids(settings: Settings) -> tuple[Settings, list[tuple[str, dict[str, str]]]]:
    """An alphabet or smiley id that is not a valid id (a hand-edit, ``Latin``, ``//host/share/x``) went to the
    loaders as a file name and could never be loaded. The field goes back to its default and the rest of the
    file is kept, instead of the whole file being thrown away.

    Also returns what to tell the user: (language key, values) per reset field, see ``SettingsStore.warnings``.
    """
    changes: dict[str, str] = {}
    notes: list[tuple[str, dict[str, str]]] = []
    if isinstance(settings.alphabet, str) and settings.alphabet and not is_valid_id(settings.alphabet):
        changes["alphabet"] = Settings.alphabet
        notes.append(("startup.bad_alphabet_id", {"value": _shown(settings.alphabet), "default": Settings.alphabet}))
    if isinstance(settings.smileys, str) and settings.smileys and not is_valid_id(settings.smileys):
        changes["smileys"] = Settings.smileys
        notes.append(("startup.bad_smiley_id", {"value": _shown(settings.smileys)}))
    if changes:
        log.warning("Ungültige ID in den Einstellungen, Standardwert verwendet: %s", ", ".join(changes))
        return settings.replace(**changes), notes
    return settings, notes


class SettingsStore:
    FILE_NAME = "settings.json"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_config_dir()
        self.path = self.directory / self.FILE_NAME
        # what the last ``load`` changed on its own and the user has to be told: (language key, values)
        self.warnings: list[tuple[str, dict[str, str]]] = []
        if directory is None:
            self._migrate_legacy_directory()

    def _migrate_legacy_directory(self) -> None:
        """Copy settings and alphabets over from the old PolyglotMorse folder."""
        legacy = self.directory.with_name(self.directory.name.replace("MourseDecoder", "PolyglotMorse"))
        if legacy != self.directory and legacy.is_dir() and not self.directory.exists():
            try:
                shutil.copytree(legacy, self.directory)
            except OSError as exc:
                log.warning("Alter Konfigurationsordner nicht übernommen: %s", exc)

    def load(self) -> Settings:
        """Load settings, falling back to defaults.

        A broken file is moved to settings.json.bak rather than deleted, so the
        user can still recover values by hand.
        """
        self.warnings = []
        if not self.path.is_file():
            return Settings()
        try:
            data = jsontext.loads(self.path.read_text(encoding="utf-8-sig"), dict)
            if not isinstance(data, dict):
                raise ValueError("Wurzelelement ist kein Objekt")
            settings, self.warnings = _without_bad_ids(Settings.from_dict(data))
            settings = _canonical_keys(settings)
            errors = settings.validate()
            if errors:
                # files saved before the key/shortcut clash check: keep the rest, drop the clashing shortcut
                relaxed = settings.without_clashing_shortcuts()
                if relaxed == settings or relaxed.validate():
                    raise ValueError("; ".join(errors))
                log.warning("Tastenkürzel gelöscht, weil es mit einer Morse-Taste kollidiert (%s)", "; ".join(errors))
                settings = relaxed
            return settings
        except (OSError, ValueError, TypeError) as exc:
            # the file is thrown away: what it said about its ids no longer matters, but the user is told
            self.warnings = [("startup.settings_discarded", {"reason": jsontext.display_text(str(exc), 40)})]
            log.warning("Einstellungen ungültig (%s) – verwende Standardwerte", exc)
            self._backup_broken_file()
            return Settings()

    def save(self, settings: Settings) -> None:
        errors = settings.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.directory.mkdir(parents=True, exist_ok=True)
        # write to a temp file and rename so a crash can't leave half a JSON file
        jsontext.write_atomic(self.path, json.dumps(settings.to_dict(), indent=2, ensure_ascii=False))

    def _backup_broken_file(self) -> None:
        jsontext.backup_file(self.path, move=True)
