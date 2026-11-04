"""Lenient JSON for hand-written alphabet and smiley files with pasted texts.

Pasted texts bring raw line breaks and straight quotes ("NIV") that strict JSON rejects.
Files are always saved back as strict JSON, so the leniency only applies to input.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

# a quote only closes a string if a comma, colon, bracket or the end follows
_STRING_END = re.compile(r"\s*(?:[,:}\]]|$)")


def escape_stray_quotes(text: str) -> str:
    """Escape quotes inside strings that cannot be the string's end."""
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string and c == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        if c == '"':
            if not in_string:
                in_string = True
            elif _STRING_END.match(text, i + 1):
                in_string = False
            else:
                c = '\\"'
        out.append(c)
        i += 1
    return "".join(out)


def loads(text: str, object_pairs_hook: Callable[[list[tuple[str, Any]]], Any]) -> Any:
    """json.loads that accepts raw line breaks and stray quotes inside strings.

    Raises the error of the original text if the repair doesn't help either.
    """
    try:
        return json.loads(text, object_pairs_hook=object_pairs_hook, strict=False)
    except json.JSONDecodeError as exc:
        try:
            return json.loads(escape_stray_quotes(text), object_pairs_hook=object_pairs_hook, strict=False)
        except json.JSONDecodeError:
            raise exc from None


_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\Z")
_WORD = re.compile(r"[^\s{}\[\],:\"'/]+")
_PY_LITERALS = {"True": "true", "False": "false", "None": "null"}


def _tokens(text: str) -> list[tuple[str, str]]:
    """Split into (kind, text) with kinds str, p (punctuation), w (bare word), ws.

    Comments are dropped, single-quoted strings become double-quoted. Fixes
    found on the way are recorded as kind "fix" tokens (their text is the fix name).
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(("ws", text[i:j]))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            out.append(("fix", "Kommentare entfernt"))
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(("fix", "Kommentare entfernt"))
        elif c in "\"'":
            j, body = i + 1, []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    body.append(text[j : j + 2])
                    j += 2
                else:
                    body.append(text[j])
                    j += 1
            raw = "".join(body)
            if c == "'":
                raw = raw.replace("\\'", "'")
                raw = re.sub(r'(?<!\\)((?:\\\\)*)"', r'\1\\"', raw)
                out.append(("fix", "einfache Anführungszeichen ersetzt"))
            out.append(("str", f'"{raw}"'))
            i = j + 1
        elif c in "{}[],:":
            out.append(("p", c))
            i += 1
        else:
            m = _WORD.match(text, i)
            if not m:  # lone "/" and the like
                out.append(("w", c))
                i += 1
            else:
                out.append(("w", m.group()))
                i = m.end()
    return out


def repair(text: str) -> tuple[str, list[str]]:
    """Fix JSON problems that have exactly one reading; returns (text, fixes).

    Handled: byte order mark, // and /* */ comments, single-quoted strings,
    unquoted keys, True/False/None, trailing commas, missing commas between
    items, missing closing brackets at the end. Raises json.JSONDecodeError of
    the original text if the result still doesn't parse, so nothing is guessed.
    """
    try:
        loads(text, dict)
    except json.JSONDecodeError as original:
        error = original
    else:
        return text, []

    fixes: list[str] = []

    def note(what: str) -> None:
        if what not in fixes:
            fixes.append(what)

    stripped = text.lstrip("\ufeff")
    if stripped != text:
        note("Byte-Order-Mark entfernt")
    raw = _tokens(stripped)
    for kind, value in raw:
        if kind == "fix":
            note(value)
    toks = [t for t in raw if t[0] != "fix"]
    sig = [i for i, t in enumerate(toks) if t[0] != "ws"]

    drop: set[int] = set()
    insert_after: dict[int, str] = {}
    stack: list[str] = []
    for pos, i in enumerate(sig):
        kind, value = toks[i]
        nxt = toks[sig[pos + 1]] if pos + 1 < len(sig) else None
        prev = toks[sig[pos - 1]] if pos else None
        if kind == "w":
            if value in _PY_LITERALS:
                toks[i] = ("w", _PY_LITERALS[value])
                note("True/False/None zu true/false/null")
            elif value not in ("true", "false", "null") and not _NUMBER.match(value):
                if nxt == ("p", ":") and prev in (None, ("p", "{"), ("p", ",")):
                    toks[i] = ("str", f'"{value}"')
                    note("Schlüssel in Anführungszeichen gesetzt")
        elif kind == "p":
            if value in "{[":
                stack.append("}" if value == "{" else "]")
            elif value in "}]" and stack:
                stack.pop()
            elif value == "," and nxt is not None and nxt[0] == "p" and nxt[1] in "}]":
                drop.add(i)
                note("überzählige Kommas entfernt")
        ends_value = kind in ("str", "w") or (kind == "p" and value in "}]")
        starts_value = nxt is not None and (nxt[0] in ("str", "w") or (nxt[0] == "p" and nxt[1] in "{["))
        if ends_value and starts_value:
            insert_after[i] = ","
            note("fehlende Kommas ergänzt")

    parts: list[str] = []
    for i, (_kind, value) in enumerate(toks):
        if i not in drop:
            parts.append(value)
        if i in insert_after:
            parts.append(insert_after[i])
    if stack:
        parts.append("".join(reversed(stack)))
        note("fehlende schließende Klammern ergänzt")
    fixed = "".join(parts)
    try:
        loads(fixed, dict)
    except json.JSONDecodeError:
        raise error from None
    return fixed, fixes


def write_atomic(path: Path, data: str | bytes) -> None:
    """Write to ``<name>.tmp`` and rename, so a crash cannot leave a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


MAX_BACKUPS = 5


def backup_file(path: Path, move: bool) -> None:
    """Keep ``path`` as ``<name>.bak`` (copy, or move if ``move``).

    An existing backup that differs is not overwritten: the next free ``.bak2`` .. ``.bak5`` is
    used, and the last one is replaced when all are taken, so the oldest copy always survives.
    A backup with the same content is reused. Errors are ignored.
    """
    try:
        for number in range(1, MAX_BACKUPS + 1):
            target = path.with_name(f"{path.name}.bak{number if number > 1 else ''}")
            if not target.exists() or number == MAX_BACKUPS:
                break
            if filecmp.cmp(path, target, shallow=False):
                if move:
                    path.unlink()
                return
        if move:
            os.replace(path, target)
        else:
            shutil.copy2(path, target)
    except OSError:
        pass
