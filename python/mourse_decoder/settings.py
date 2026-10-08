"""User settings: data model, validation and persistence.

Settings are a frozen dataclass because the UI thread and the Rust worker share
them. A change always creates a new object that is swapped in atomically.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
_VK_PATTERN = re.compile(r"^VK(\d{1,3})$", re.IGNORECASE)
_MOUSE_PATTERN = re.compile(r"^Mouse(\d{1,2})$", re.IGNORECASE)


def is_valid_trigger(name: object) -> bool:
    """True for the standard triggers, VK1..VK255 and Mouse4..Mouse32."""
    if not isinstance(name, str):
        return False
    if name in TRIGGER_NAMES:
        return True
    if match := _VK_PATTERN.match(name):
        return 1 <= int(match.group(1)) <= 255
    if match := _MOUSE_PATTERN.match(name):
        return 4 <= int(match.group(1)) <= 32
    return False


_MODIFIERS = {"ctrl", "control", "strg", "alt", "shift", "win", "meta", "super"}


def is_valid_shortcut(spec: object) -> bool:
    """"" (no shortcut) or modifiers joined with "+" and a trigger, e.g. "Ctrl+Shift+Pause"."""
    if not isinstance(spec, str):
        return False
    if not spec.strip():
        return True
    *modifiers, key = (part.strip() for part in spec.split("+"))
    return is_valid_trigger(key) and all(m.lower() in _MODIFIERS for m in modifiers)


# UI limits in ms: below 50 nobody can tell dot from dash on purpose, above 5 s it isn't morsing
DOT_THRESHOLD_RANGE = (50, 2000)
LETTER_GAP_RANGE = (100, 5000)
WORD_GAP_RANGE = (0, 15000)


@dataclass(frozen=True)
class Settings:
    dot_threshold_ms: int = 200  # shorter press is a dot
    letter_gap_ms: int = 600     # silence that ends a letter
    word_gap_ms: int = 1400      # ITU ratio 7:3 -> 600 * 7/3, 0 = no spaces
    trigger: str = "ScrollLock"
    alphabet: str = "latin"
    smileys: str = ""            # smiley set id, "" = none
    enabled: bool = True
    language: str = "en"         # id of a file in locales/
    theme: str = "system"        # "system" follows Windows, else "light" or "dark"
    sidebar_collapsed: bool = False
    notifications: bool = True   # small message when the shortcut turns Morse input on or off
    block_input: bool = False    # hide the trigger's normal function so only decoded text arrives
    block_shortcut: str = "Ctrl+Shift+Pause"  # toggles block_input from anywhere, "" = none

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

        if not errors:
            if self.word_gap_ms and self.word_gap_ms <= self.letter_gap_ms:
                errors.append("Die Wortpause muss länger als die Buchstabenpause sein (oder 0 = aus).")
        if not is_valid_trigger(self.trigger):
            errors.append(f"Unbekannter Taster '{self.trigger}'.")
        if not isinstance(self.alphabet, str) or not self.alphabet:
            errors.append("Es muss ein Alphabet gewählt sein.")
        if not isinstance(self.smileys, str):
            errors.append("'smileys' muss ein Text sein (ID des Smiley-Sets oder leer).")
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
        if not is_valid_shortcut(self.block_shortcut):
            errors.append(f"Ungültiges Tastenkürzel '{self.block_shortcut}'.")
        return errors

    def replace(self, **changes: Any) -> Settings:
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        """Ignore unknown keys, default the missing ones, so versions can read each other's files."""
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "MourseDecoder"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MourseDecoder"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "mourse-decoder"


class SettingsStore:
    FILE_NAME = "settings.json"

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_config_dir()
        self.path = self.directory / self.FILE_NAME
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
        if not self.path.is_file():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Wurzelelement ist kein Objekt")
            settings = Settings.from_dict(data)
            errors = settings.validate()
            if errors:
                raise ValueError("; ".join(errors))
            return settings
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Einstellungen ungültig (%s) – verwende Standardwerte", exc)
            self._backup_broken_file()
            return Settings()

    def save(self, settings: Settings) -> None:
        errors = settings.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.directory.mkdir(parents=True, exist_ok=True)
        # write to a temp file and rename so a crash can't leave half a JSON file
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _backup_broken_file(self) -> None:
        try:
            os.replace(self.path, self.path.with_suffix(".json.bak"))
        except OSError:
            pass
