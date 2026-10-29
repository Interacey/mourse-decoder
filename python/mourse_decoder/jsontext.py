"""Lenient JSON for hand-written alphabet and smiley files with pasted texts.

Pasted texts bring raw line breaks and straight quotes ("NIV") that strict JSON rejects.
Files are always saved back as strict JSON, so the leniency only applies to input.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
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
