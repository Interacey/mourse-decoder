"""Macros: output texts with {commands} in braces.

    "Hallo{Enter}"                       text, then a key
    "{Win+R}{WaitWindow Run}notepad"     wait until the Run window is in front
    "{Loop 3}Hi{Wait 500}{EndLoop}"      repeat, may be nested

The whole macro is checked before anything happens, so a typo like {Crtl+C} stops it
with a message instead of typing half a macro into some window. German command names
work too ({Warte 500}, {Schleife 3}…{SchleifenEnde}).
"""

from __future__ import annotations

import re
from collections.abc import Callable

_I = re.IGNORECASE
_TOKEN = re.compile(r"\{([^{}]+)\}")
_WAIT = re.compile(r"\s*(?:wait|warte|warten)\s*(\d{1,5})\s*", _I)
_WAIT_WINDOW = re.compile(r"\s*(?:waitwindow|wartefenster)\s+(.+?)\s*", _I)
_LOOP = r"\{\s*(?:loop|schleife)\s+(\d{1,5})\s*\}"
_END_LOOP = r"\{\s*(?:endloop|schleifenende)\s*\}"
_ANY_LOOP = re.compile(rf"{_LOOP}|{_END_LOOP}", _I)
# an innermost loop: no other loop token between start and end
_INNER_LOOP = re.compile(rf"{_LOOP}((?:(?!{_LOOP}|{_END_LOOP}).)*?){_END_LOOP}", _I | re.DOTALL)

MAX_LOOP_COUNT = 1000
# nested loops multiply, so cap the result before it eats the memory
MAX_EXPANDED_LENGTH = 1_000_000

# ("text", str) | ("keys", combo) | ("wait", seconds) | ("window", title part)
Step = tuple[str, object]


class MacroError(RuntimeError):
    """The macro is broken or was stopped; the message is shown to the user."""


def expand_loops(text: str) -> str:
    """Write out every {Loop n}…{EndLoop}, innermost first."""

    def repeat(match: re.Match[str]) -> str:
        count = int(match[1])
        if count > MAX_LOOP_COUNT:
            raise MacroError(f"Schleife mit {count} Durchläufen – höchstens {MAX_LOOP_COUNT} erlaubt. Nichts getippt.")
        if len(match[2]) * count > MAX_EXPANDED_LENGTH:
            raise MacroError("Die Schleifen ergeben zu viel Text. Nichts getippt.")
        return match[2] * count

    while True:
        text, found = _INNER_LOOP.subn(repeat, text)
        if len(text) > MAX_EXPANDED_LENGTH:
            raise MacroError("Die Schleifen ergeben zu viel Text. Nichts getippt.")
        if not found:
            break
    if _ANY_LOOP.search(text):
        raise MacroError("Zu jedem {Loop …} gehört genau ein {EndLoop}. Nichts getippt.")
    return text


def parse(text: str, check_shortcut: Callable[[str], None]) -> list[Step]:
    """Turn a macro into steps; MacroError if any part of it is broken.

    ``check_shortcut`` raises ValueError for anything that is no key combination.
    """
    steps: list[Step] = []
    # split() alternates text and the contents of {…}
    for i, part in enumerate(_TOKEN.split(expand_loops(text))):
        if not i % 2:
            if part:
                steps.append(("text", part))
        elif wait := _WAIT.fullmatch(part):
            steps.append(("wait", int(wait[1]) / 1000))
        elif window := _WAIT_WINDOW.fullmatch(part):
            steps.append(("window", window[1]))
        elif not any(c.isalnum() for c in part):
            steps.append(("text", f"{{{part}}}"))  # {^_^} or { } is text, not a command
        else:
            try:
                check_shortcut(part)
            except ValueError as exc:
                raise MacroError(f"Unbekannter Befehl {{{part.strip()}}}: {exc}. Nichts getippt.") from None
            steps.append(("keys", part.strip()))
    return steps
