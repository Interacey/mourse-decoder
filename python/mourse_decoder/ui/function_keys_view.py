"""What the function keys page shows, kept free of tkinter so tests can cover it."""

from __future__ import annotations

import re

from ..function_keys import FunctionKeys, Slot, describe_trigger

LAST_REAL_SLOT = 24  # F1..F24 are keys Windows knows; F25..F100 only exist as trigger combinations
PREVIEW_CHARS = 60
LINE_BREAK = "↵"  # shown for a line break in the list


def is_virtual(number: int) -> bool:
    return number > LAST_REAL_SLOT


def is_used(slot: Slot) -> bool:
    """A slot the user has filled in: it has a macro or a label."""
    return bool(slot.macro.strip() or slot.label.strip())


def status(slot: Slot) -> str:
    """"active", "off", "notrigger" or "empty"; the page translates these."""
    if not slot.enabled:
        return "off"
    if not slot.macro.strip():
        return "empty"
    if not slot.trigger.strip():
        return "notrigger"
    return "active"


def preview(slot: Slot, limit: int = PREVIEW_CHARS) -> str:
    """The label, or the macro on one line if there is no label; cut at ``limit`` characters."""
    text = slot.label.strip()
    if not text:
        text = re.sub(r"\r\n|\r|\n", LINE_BREAK, slot.macro.strip())
        text = text.replace("\t", " ")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def matches(slot: Slot, query: str) -> bool:
    """Case-insensitive search in the slot name, the label and the macro."""
    needle = query.strip().casefold()
    if not needle:
        return True
    return any(needle in text.casefold() for text in (slot.name, slot.label, slot.macro))


def filter_slots(keys: FunctionKeys, query: str = "", only_used: bool = False) -> list[Slot]:
    return [s for s in keys.slots if (not only_used or is_used(s)) and matches(s, query)]


def row(slot: Slot) -> tuple[str, str, str, str, str]:
    """(name, kind, trigger, preview, status) with kind and status as keys, not yet translated."""
    return (
        slot.name, "virtual" if is_virtual(slot.slot) else "real", describe_trigger(slot.trigger), preview(slot),
        status(slot),
    )


def errors_for_slot(errors: list[str], number: int) -> list[str]:
    """The entries of ``FunctionKeys.validate`` that name slot ``number`` (F5 does not match F57)."""
    pattern = re.compile(rf"(?<![0-9A-Za-z])F{number}(?![0-9])")
    return [e for e in errors if pattern.search(e)]
