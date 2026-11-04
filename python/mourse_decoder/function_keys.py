"""Function keys F1..F100: slots that type a macro when their trigger is pressed.

Public interface for the UI
---------------------------
Data (frozen dataclasses, change by building new objects)::

    keys, warnings = FunctionKeysStore().load(check_shortcut)   # warnings: list[str], German
    keys.slot(57)                          # Slot(slot=57, trigger="Ctrl+Alt+F13", macro="", ...)
    keys = keys.replace_slot(keys.slot(57).replace(macro="Hello{Enter}", trigger="Ctrl+F5"))
    errors = keys.validate(settings, check_shortcut)             # list[str], German, [] = ok
    FunctionKeysStore().save(keys)         # ValueError("; ".join(errors)) if invalid
    keys.reset_slot(57)                    # slot back to its default trigger, empty macro

Helpers for labels and defaults::

    slot_name(57)                          # "F57"
    default_trigger(57)                    # "Ctrl+Alt+Shift+Win+F13" ... "" for F1..F12
    describe_trigger("Ctrl+Alt+F13")       # "Ctrl + Alt + F13" (current language)

Running (see ``MorseController``)::

    controller.apply_function_keys(keys)   # ValueError if invalid; installs the slots in the hook
    controller.function_keys               # the applied FunctionKeys
    controller.run_slot(57)                # test button; None = started, else the reason (str)
    controller.poll_function_keys()        # the tray calls this every tick, starts fired macros

Errors while a macro runs reach the UI through ``ControllerListener.on_error(message)``, the
same hook that reports errors of Morse macros; the message starts with the slot name.

Slots
-----
F1..F12 have no trigger until the user sets one. F13..F24 are real Windows keys and default to the
bare key. F25..F100 do not exist on a keyboard; their default triggers are very long
combinations of Ctrl, Alt, Shift and Win with F13..F24 that nobody presses by accident, meant for
a Stream Deck (see ``default_trigger``). Triggers can be changed per slot. A trigger is a
``KeyBinding`` string such as ``Ctrl+Shift+F13``; the modifiers must match exactly, so ``F13``
does not fire under ``Ctrl+F13``.

File
----
``function_keys.json`` next to ``settings.json``::

    {"schema_version": 1, "slots": [{"slot": 57, "trigger": "...", "macro": "...",
                                     "enabled": true, "swallow": true, "label": ""}]}

Only slots that differ from their default are written, so a new default does not need a
migration. Unknown fields in a slot (or at the top) are kept and written back. A file that cannot
be read is moved to ``function_keys.json.bak`` before defaults are used; a file from a newer
version is read as far as possible and never overwritten.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import jsontext, macros
from .settings import Settings, default_config_dir, fires_with, is_valid_binding, normalize_binding, same_binding
from .triggers import binding_label

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SLOT_COUNT = 100
MAX_MACRO_LENGTH = 10_000
MAX_LABEL_LENGTH = 100
FILE_NAME = "function_keys.json"

# real keys, virtual-key codes 124..135
_REAL_KEYS = tuple(f"F{n}" for n in range(13, 25))
_MODIFIERS = ("Ctrl", "Alt", "Shift", "Win")
# slots 13..24 are the real keys, so the virtual slots start after them
_FIRST_VIRTUAL = 25


def slot_name(number: int) -> str:
    """``57`` -> ``"F57"``."""
    return f"F{number}"


def _virtual_triggers() -> list[str]:
    """Triggers for slots F25..F100, longest combination first.

    Four modifiers with F13..F24 (12), then every set of three modifiers (4 x 12), then sets of two
    (6 x 12, only the first 16 are needed). Within a size the modifier sets follow the order of
    ``itertools.combinations(("Ctrl", "Alt", "Shift", "Win"), size)`` and F13..F24 run fastest.
    """
    out: list[str] = []
    for size in (4, 3, 2):
        for modifiers in itertools.combinations(_MODIFIERS, size):
            out.extend("+".join([*modifiers, key]) for key in _REAL_KEYS)
    return out[: SLOT_COUNT - _FIRST_VIRTUAL + 1]


_VIRTUAL_TRIGGERS = _virtual_triggers()


def default_trigger(number: int) -> str:
    """Default trigger of a slot; "" for F1..F12 (the user chooses)."""
    if not 1 <= number <= SLOT_COUNT:
        raise ValueError(f"Slot {number} liegt nicht zwischen 1 und {SLOT_COUNT}.")
    if number <= 12:
        return ""
    if number < _FIRST_VIRTUAL:
        return _REAL_KEYS[number - 13]
    return _VIRTUAL_TRIGGERS[number - _FIRST_VIRTUAL]


def describe_trigger(binding: str) -> str:
    """Readable label of a trigger in the current language, "" for none."""
    return binding_label(binding)


@dataclass(frozen=True)
class Slot:
    slot: int
    trigger: str = ""        # "" = no trigger, the slot can only be run from the UI
    macro: str = ""          # text in the macro language of alphabet codes (see macros.py)
    enabled: bool = True     # off = the slot does nothing, whatever trigger and macro it has
    swallow: bool = True     # hide the trigger key from the focused app (Windows only)
    label: str = ""          # free text the user sees in the list
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)  # unknown fields of the file

    @property
    def name(self) -> str:
        return slot_name(self.slot)

    @property
    def active(self) -> bool:
        """Enabled, has a trigger and a macro: what the hook gets."""
        return self.enabled and bool(self.trigger.strip()) and bool(self.macro.strip())

    def replace(self, **changes: Any) -> Slot:
        return dataclasses.replace(self, **changes)

    def is_default(self) -> bool:
        return self.same_content(default_slot(self.slot)) and not self.extra

    def same_content(self, other: Slot) -> bool:
        """Equal apart from the spelling of the trigger (``strg+f13`` is ``Ctrl+F13``)."""

        def canonical(slot: Slot) -> Slot:
            return slot.replace(trigger=normalize_binding(slot.trigger)) if isinstance(slot.trigger, str) else slot

        return canonical(self) == canonical(other)


def default_slot(number: int) -> Slot:
    return Slot(number, trigger=default_trigger(number))


@dataclass(frozen=True)
class FunctionKeys:
    slots: tuple[Slot, ...] = field(default_factory=lambda: tuple(default_slot(n) for n in range(1, SLOT_COUNT + 1)))
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)  # unknown top-level fields

    def slot(self, number: int) -> Slot:
        if not 1 <= number <= len(self.slots):
            raise ValueError(f"Slot {number} liegt nicht zwischen 1 und {SLOT_COUNT}.")
        return self.slots[number - 1]

    def replace_slot(self, new: Slot) -> FunctionKeys:
        self.slot(new.slot)  # range check
        slots = list(self.slots)
        slots[new.slot - 1] = new
        return dataclasses.replace(self, slots=tuple(slots))

    def reset_slot(self, number: int) -> FunctionKeys:
        return self.replace_slot(default_slot(number))

    def active_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.active]

    def validate(
        self, settings: Settings | None = None, check_shortcut: Callable[[str], None] | None = None
    ) -> list[str]:
        """German error texts; [] if the slots can be installed.

        Without ``settings`` the clash with the Morse key and the shortcuts is not checked, without
        ``check_shortcut`` the macros are not parsed. Only active slots take part in duplicate and
        clash checks, so an empty slot with a default trigger never gets in the way.
        """
        errors: list[str] = []
        if len(self.slots) != SLOT_COUNT or [s.slot for s in self.slots] != list(range(1, SLOT_COUNT + 1)):
            return [f"Es müssen genau {SLOT_COUNT} Slots F1 bis F{SLOT_COUNT} vorhanden sein."]
        for slot in self.slots:
            errors.extend(f"{slot.name}: {text}" for text in _slot_errors(slot, check_shortcut))
        taken: dict[str, Slot] = {}
        for slot in self.active_slots():
            if not is_valid_binding(slot.trigger):
                continue  # reported above
            for other in taken.values():
                if same_binding(slot.trigger, other.trigger):
                    errors.append(f"{other.name} und {slot.name} haben denselben Auslöser ({slot.trigger}).")
            taken[slot.name] = slot
        if settings is not None:
            errors.extend(self.clashes(settings))
        return errors

    def clashes(self, settings: Settings) -> list[str]:
        """Slots whose trigger the Morse keys or the shortcuts would also react to.

        The Rust side matches a Morse key with extra modifiers held, so ``F13`` as Morse key also
        reacts to ``Ctrl+F13``; a slot clashes if its modifiers include all of the other key's.
        """
        reserved = [
            ("der Morse-Taste", settings.trigger),
            ("der Strich-Taste", settings.dash_trigger if settings.separate_dash_key else ""),
            ("dem Ein/Aus-Kürzel", settings.block_shortcut),
            ("dem Umschalt-Kürzel", settings.cycle_shortcut),
        ]
        errors = []
        for slot in self.active_slots():
            if not is_valid_binding(slot.trigger):
                continue
            for what, spec in reserved:
                if spec and is_valid_binding(spec) and fires_with(spec, slot.trigger):
                    errors.append(f"{slot.name} ({slot.trigger}) kollidiert mit {what} ({spec}).")
        return errors

    def to_dict(self) -> dict[str, Any]:
        changed = []
        for slot in self.slots:
            if not slot.is_default():
                changed.append(
                    {**slot.extra, "slot": slot.slot, "trigger": slot.trigger, "macro": slot.macro,
                     "enabled": slot.enabled, "swallow": slot.swallow, "label": slot.label}
                )
        return {**self.extra, "schema_version": SCHEMA_VERSION, "slots": changed}


# Virtual-key codes: Shift, Ctrl, Alt, Caps Lock, both Windows keys, left/right Shift, Ctrl, Alt.
_MODIFIER_VKS = frozenset({16, 17, 18, 20, 91, 92, 160, 161, 162, 163, 164, 165})
# Backspace, Tab, Enter, digits, letters, numpad, punctuation
_TYPING_VKS = frozenset(
    {8, 9, 13, 32, *range(48, 58), *range(65, 91), *range(96, 112), *range(186, 193), *range(219, 223), 226}
)


def _unusable_trigger(trigger: str, swallow: bool) -> str:
    """Why ``trigger`` must not start a slot ("" if it may). Same rules as ``unusable_reason`` in src/function_keys.rs.

    The hook sees every key of the user: a slot on Esc, a modifier or a typing key without Ctrl, Alt or Win
    would fire (and, swallowing, hide) normal typing, and Esc stops a running macro.
    """
    *modifiers, key = normalize_binding(trigger).split("+")
    shortcut = bool({"Ctrl", "Alt", "Win"} & set(modifiers))
    code = int(key[2:]) if key.startswith("VK") else 0
    if key in ("ControlRight", "AltGr") or code in _MODIFIER_VKS:
        return "die Modifikatortasten"
    if code == 27:
        return "Esc und das Abbrechen mit Esc"
    if (key == "Space" or code in _TYPING_VKS) and not shortcut:
        return "normales Tippen"
    if key in ("MouseRight", "MouseMiddle") and swallow and not shortcut and "Shift" not in modifiers:
        return "normales Klicken"
    return ""


def _slot_errors(slot: Slot, check_shortcut: Callable[[str], None] | None) -> list[str]:
    errors: list[str] = []
    # a switched-off slot never reaches the hook, so its trigger and macro length must not block the table
    off = slot.enabled is False
    if not isinstance(slot.trigger, str):
        errors.append(f"Ungültiger Auslöser '{slot.trigger}'.")
    elif off:
        pass
    elif slot.trigger.strip() and not is_valid_binding(slot.trigger):
        errors.append(f"Ungültiger Auslöser '{slot.trigger}'.")
    elif slot.trigger.strip() and (why := _unusable_trigger(slot.trigger, slot.swallow is not False)):
        errors.append(f"{normalize_binding(slot.trigger)} ist als Auslöser nicht erlaubt (stört {why}).")
    if not isinstance(slot.macro, str):
        return [*errors, "Das Makro muss ein Text sein."]
    if off:
        pass
    elif len(slot.macro) > MAX_MACRO_LENGTH:
        errors.append(f"Das Makro ist länger als {MAX_MACRO_LENGTH} Zeichen.")
    elif check_shortcut is not None and slot.macro.strip() and slot.enabled:
        try:
            macros.parse(slot.macro, check_shortcut)
        except macros.MacroError as exc:
            errors.append(str(exc))
    for name in ("enabled", "swallow"):
        if not isinstance(getattr(slot, name), bool):
            errors.append(f"'{name}' muss true oder false sein.")
    if not isinstance(slot.label, str) or len(slot.label) > MAX_LABEL_LENGTH:
        errors.append(f"Die Beschriftung muss ein Text mit höchstens {MAX_LABEL_LENGTH} Zeichen sein.")
    return errors


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"Feld doppelt vorhanden: {sorted({k for k in keys if keys.count(k) > 1})[0]}")
    return dict(pairs)


_SLOT_FIELDS = {"slot", "trigger", "macro", "enabled", "swallow", "label"}


class FunctionKeysStore:
    """Reads and writes ``function_keys.json``; same directory and write method as ``SettingsStore``."""

    FILE_NAME = FILE_NAME

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory else default_config_dir()
        self.path = self.directory / self.FILE_NAME
        self.read_only = False  # set by load() when the file comes from a newer version

    def load(self, check_shortcut: Callable[[str], None] | None = None) -> tuple[FunctionKeys, list[str]]:
        """The slots and the warnings to show, empty if the file was fine or does not exist.

        A file that cannot be parsed goes to ``function_keys.json.bak`` and the defaults are used. A
        broken entry (wrong type, unknown slot number, macro that does not parse) is reported; a
        broken macro is kept but switched off so the user can fix it. When anything was dropped or
        switched off the file is first copied to ``.bak``.
        """
        self.read_only = False
        if not self.path.is_file():
            return FunctionKeys(), []
        try:
            data = jsontext.loads(self.path.read_text(encoding="utf-8-sig"), _reject_duplicate_keys)
            if not isinstance(data, dict):
                raise ValueError("Wurzelelement ist kein Objekt")
            version = data.get("schema_version", SCHEMA_VERSION)
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError("schema_version ist keine positive ganze Zahl")
            entries = data.get("slots", [])
            if not isinstance(entries, list):
                raise ValueError("'slots' ist keine Liste")
        except (OSError, ValueError) as exc:
            self._backup(move=True)
            log.warning("Funktionstasten ungültig (%s) – verwende Standardwerte", exc)
            return FunctionKeys(), [
                f"{self.FILE_NAME} war nicht lesbar ({exc}). Die Datei wurde als {self.FILE_NAME}.bak gesichert, "
                "es gelten die Standardwerte."
            ]
        warnings: list[str] = []
        if version > SCHEMA_VERSION:
            self.read_only = True
            warnings.append(
                f"{self.FILE_NAME} stammt aus einer neueren Version (schema_version {version}). "
                "Die Funktionstasten werden nicht gespeichert, damit die Datei unverändert bleibt."
            )
        slots = [default_slot(n) for n in range(1, SLOT_COUNT + 1)]
        seen: set[int] = set()
        for index, entry in enumerate(entries[: SLOT_COUNT * 2]):
            slot, problem = self._read_slot(entry, check_shortcut)
            if slot is None:
                warnings.append(f"Eintrag {index + 1} ignoriert: {problem}")
            elif slot.slot in seen:
                warnings.append(f"{slot.name} steht mehrfach in der Datei, der erste Eintrag gilt.")
            else:
                seen.add(slot.slot)
                slots[slot.slot - 1] = slot
                if problem:
                    warnings.append(f"{slot.name}: {problem}")
        if len(entries) > SLOT_COUNT * 2:
            warnings.append(f"Mehr als {SLOT_COUNT * 2} Einträge, der Rest wurde ignoriert.")
        extra = {k: v for k, v in data.items() if k not in ("schema_version", "slots")}
        if warnings and not self.read_only:
            self._backup(move=False)
        return FunctionKeys(tuple(slots), extra), warnings

    @staticmethod
    def _read_slot(entry: Any, check_shortcut: Callable[[str], None] | None) -> tuple[Slot | None, str]:
        """(slot, problem). No slot if the entry is unusable; a problem with a slot means it was switched off."""
        if not isinstance(entry, dict):
            return None, "kein Objekt."
        number = entry.get("slot")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= SLOT_COUNT:
            return None, f"'slot' muss eine Zahl von 1 bis {SLOT_COUNT} sein."
        base = default_slot(number)
        values: dict[str, Any] = {name: entry.get(name, getattr(base, name)) for name in _SLOT_FIELDS - {"slot"}}
        if isinstance(values["trigger"], str):
            values["trigger"] = normalize_binding(values["trigger"])
        slot = Slot(number, **values, extra={k: v for k, v in entry.items() if k not in _SLOT_FIELDS})
        errors = _slot_errors(slot, check_shortcut)
        if not errors:
            return slot, ""
        if not isinstance(slot.macro, str) or not isinstance(slot.trigger, str):
            return None, f"{slot.name}: Auslöser und Makro müssen Texte sein."
        # keep what the user wrote, but never run it
        bad_types = not all(isinstance(getattr(slot, n), bool) for n in ("enabled", "swallow"))
        if bad_types or not isinstance(slot.label, str):
            return None, "; ".join(errors)
        return slot.replace(enabled=False), "ausgeschaltet. " + "; ".join(errors)

    def save(self, keys: FunctionKeys) -> None:
        """Atomic write. ValueError if the slots are invalid or the file belongs to a newer version."""
        if self.read_only:
            raise ValueError(f"{self.FILE_NAME} stammt aus einer neueren Version und wird nicht überschrieben.")
        errors = keys.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.directory.mkdir(parents=True, exist_ok=True)
        jsontext.write_atomic(self.path, json.dumps(keys.to_dict(), indent=2, ensure_ascii=False))

    def _backup(self, move: bool) -> None:
        jsontext.backup_file(self.path, move)
