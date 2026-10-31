"""Macros: output texts with {commands} in braces.

    "Hallo{Enter}"                       text, then a key
    "{Win+R}{WaitWindow Run}notepad"     wait until the Run window is in front
    "{Loop 3}Hi{Wait 500}{EndLoop}"      repeat, may be nested
    "{Clipboard}"                        type what is on the clipboard (e.g. a KeePass copy)
    "{File C:\\Users\\me\\doc.pdf}"        paste the file or folder (several: path|path)

The whole macro is checked before anything happens, so a typo like {Crtl+C} stops it
with a message instead of typing half a macro into some window. German command names
work too ({Warte 500}, {Schleife 3}…{SchleifenEnde}, {Zwischenablage}, {Datei …}).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from . import jsontext

_I = re.IGNORECASE
_TOKEN = re.compile(r"\{([^{}]+)\}")
_WAIT = re.compile(r"\s*(?:wait|warte|warten)\s*(\d{1,5})\s*", _I)
_WAIT_WINDOW = re.compile(r"\s*(?:waitwindow|wartefenster)\s+(.+?)\s*", _I)
_CLIPBOARD = re.compile(r"\s*(?:clipboard|zwischenablage)\s*", _I)
_FILES = re.compile(r"\s*(?:file|files|datei|dateien|folder|ordner)\s+(.+?)\s*", _I)
_LOOP = r"\{\s*(?:loop|schleife)\s+(\d{1,5})\s*\}"
_END_LOOP = r"\{\s*(?:endloop|schleifenende)\s*\}"
_ANY_LOOP = re.compile(rf"{_LOOP}|{_END_LOOP}", _I)
# an innermost loop: no other loop token between start and end
_INNER_LOOP = re.compile(rf"{_LOOP}((?:(?!{_LOOP}|{_END_LOOP}).)*?){_END_LOOP}", _I | re.DOTALL)

MAX_LOOP_COUNT = 1000
# nested loops multiply, so cap the result before it eats the memory
MAX_EXPANDED_LENGTH = 1_000_000

# ("text", str) | ("keys", combo) | ("wait", seconds) | ("window", title part)
# | ("clipboard", None) | ("files", [path, ...])
Step = tuple[str, object]


class MacroError(RuntimeError):
    """The macro is broken or was stopped; the message is shown to the user."""


class MacroStopped(MacroError):
    """The user switched Morse input off on purpose; nothing to report."""


def resolve_path(raw: str) -> str:
    """Absolute local path; ``%USERPROFILE%`` and ``~`` work. MacroError for a network path.

    Even looking whether ``\\\\server\\share`` exists makes Windows log in at that server and hand
    over the user's NTLM hash, so a macro from a stranger must never get that far.
    """
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if path.startswith(("\\\\", "//")):
        raise MacroError(f"Netzwerkpfade sind nicht erlaubt: {raw}. Nichts getippt.")
    return path


def commands_in_file(path: Path) -> list[str]:
    """Commands in the texts of an alphabet or smiley file that DO something when keyed.

    Key combinations (``Win+R``), ``File`` and ``Clipboard``. Waiting and loops are harmless, plain
    text too. An unreadable file gives [] because importing it reports that anyway.
    """
    try:
        data = jsontext.loads(Path(path).read_text(encoding="utf-8-sig"), dict)
        values = [v for v in data["codes"].values() if isinstance(v, str)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    found: dict[str, None] = {}  # a dict keeps the order of first use
    for text in values:
        try:
            steps = parse(text, lambda _combo: None)  # every combo counts here, typos too
        except MacroError:
            continue  # a broken macro stops by itself before it does anything
        for kind, value in steps:
            if kind == "keys":
                found[f"{{{value}}}"] = None
            elif kind in ("files", "clipboard"):
                found["{File …}" if kind == "files" else "{Clipboard}"] = None
    return list(found)


def _split_paths(spec: str) -> list[str]:
    """``a|b`` gives two paths ('|' and '"' cannot occur in Windows paths); quotes around a path are dropped."""
    paths = [p.strip().strip('"').strip() for p in spec.split("|")]
    if not all(paths):
        raise MacroError(f"Leerer Pfad in {{File {spec}}}. Nichts getippt.")
    return paths


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
        elif _CLIPBOARD.fullmatch(part):
            steps.append(("clipboard", None))
        elif files := _FILES.fullmatch(part):
            steps.append(("files", _split_paths(files[1])))
        elif not any(c.isalnum() for c in part):
            steps.append(("text", f"{{{part}}}"))  # {^_^} or { } is text, not a command
        else:
            try:
                check_shortcut(part)
            except ValueError as exc:
                raise MacroError(f"Unbekannter Befehl {{{part.strip()}}}: {exc}. Nichts getippt.") from None
            steps.append(("keys", part.strip()))
    return steps
